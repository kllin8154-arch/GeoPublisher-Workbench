"""Small Chinese desktop UI sharing the CLI's immutable publishing plans."""

from __future__ import annotations

import os
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import ConfigError, Environment
from .plans import build_plan, load_plan, save_json

_STATUS_NAMES = {
    "DRAFT": "草稿",
    "READY": "检查通过",
    "RUNNING": "发布中",
    "SUCCEEDED": "执行成功",
    "PARTIAL_SUCCESS": "部分完成",
    "FAILED": "执行未完成",
    "CANCELLED": "已安全取消",
    "MANUAL_ACTION_REQUIRED": "需要人工核对",
    "BLOCKED": "预检未通过",
    "SKIPPED": "已跳过",
    "ROLLED_BACK": "已清理本次新增资源",
    "REVIEW_REQUIRED": "需要核对地图",
}


def default_work_dir() -> Path:
    if os.name == "nt" and Path("D:/").is_dir():
        return Path("D:/a系统测试/GeoPublisher-Workbench")
    return Path.cwd() / "data"


def _status_label(status: object) -> str:
    value = str(status)
    return _STATUS_NAMES.get(value, value)


def _needs_raster_confirmation(plan: dict) -> bool:
    return not isinstance(plan.get("import_intent"), dict) and sum(
        resource.get("kind") == "geotiff" for resource in plan.get("resources", [])
    ) > 1


def _raster_summary(plan: dict) -> str:
    if _needs_raster_confirmation(plan):
        return "旧计划未记录多个 TIFF 分别发布的确认，请重新选择数据并生成计划。"
    intent = plan.get("import_intent", {})
    if intent.get("raster_mode") == "independent":
        count = intent["raster_count"]
        return f"影像发布方式：将 {count} 个 TIFF 分别发布为 {count} 个独立图层。"
    return ""


class SourceList(QListWidget):
    """Accept explicit local file/folder drops only; no automatic discovery."""

    paths_dropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(96)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragEnterEvent) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls):
            self.paths_dropped.emit([url.toLocalFile() for url in urls])
            event.acceptProposedAction()
        else:
            event.ignore()


class _Worker(QObject):
    completed = Signal(object)
    failed = Signal(object)
    event = Signal(object)
    finished = Signal()

    def __init__(self, operation: Callable[[Callable], dict]) -> None:
        super().__init__()
        self.operation = operation

    @Slot()
    def run(self) -> None:
        try:
            self.completed.emit(self.operation(self.event.emit))
        except Exception as exc:
            code = getattr(exc, "code", "UNEXPECTED_ERROR")
            message = (
                str(exc)
                if hasattr(exc, "code")
                else "操作未完成，请检查环境与任务产物；未知的服务端结果需要先核对。"
            )
            self.failed.emit({"code": code, "message": message})
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self, *, work_dir: Path | str | None = None) -> None:
        super().__init__()
        self.setWindowTitle("GeoPublisher · GeoServer 发布工作台")
        self.resize(1160, 830)
        self.setMinimumSize(980, 720)
        self._work_dir = Path(work_dir) if work_dir is not None else default_work_dir()
        self._plan: dict | None = None
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._worker_success: Callable | None = None
        self._cancel = threading.Event()
        self._building_forms = True
        self._preview_paths: list[Path] = []
        self._preview_pixmap: QPixmap | None = None
        self._report_path: Path | None = None
        self._editable: list[QWidget] = []
        self._build_ui()
        self._building_forms = False
        self._refresh_history()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(196)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 28, 20, 24)
        brand = QLabel("GeoPublisher")
        brand.setObjectName("brand")
        side.addWidget(brand)
        side.addWidget(QLabel("GeoServer 发布工作台"))
        side.addSpacing(28)
        self.tasks_nav = QPushButton("发布任务")
        self.settings_nav = QPushButton("环境设置")
        for button in (self.tasks_nav, self.settings_nav):
            button.setCheckable(True)
            side.addWidget(button)
        side.addStretch()
        footer = QLabel("本地处理 · 内网发布\n凭据仅用于本次会话")
        footer.setObjectName("muted")
        side.addWidget(footer)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_tasks_page())
        self.pages.addWidget(self._build_settings_page())
        self.tasks_nav.clicked.connect(lambda: self._navigate(0))
        self.settings_nav.clicked.connect(lambda: self._navigate(1))
        self.tasks_nav.setChecked(True)
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.setStyleSheet("""
            QWidget { font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; font-size: 13px; color: #222936; background: #ffffff; }
            QFrame#sidebar { background: #f6f7f8; border-right: 1px solid #e5e7eb; }
            QFrame#sidebar QLabel { background: transparent; }
            QLabel#brand { font-size: 19px; font-weight: 700; }
            QLabel#title { font-size: 25px; font-weight: 650; }
            QLabel#muted { color: #6b7280; line-height: 1.5; }
            QLabel#step { font-size: 16px; font-weight: 600; padding: 8px 0; }
            QPushButton { border: 1px solid #d8dde3; border-radius: 6px; padding: 8px 14px; background: #ffffff; }
            QPushButton:hover { background: #f0f3f5; }
            QPushButton:checked { background: #e9eef0; color: #274e4b; border-color: #bccfcb; }
            QPushButton#primary { background: #315f59; color: #ffffff; border-color: #315f59; }
            QPushButton#primary:hover { background: #264c47; }
            QPushButton:disabled { color: #9ca3af; background: #f3f4f6; border-color: #e5e7eb; }
            QLineEdit, QComboBox, QSpinBox { padding: 7px; border: 1px solid #d8dde3; border-radius: 5px; min-height: 21px; }
            QListWidget, QPlainTextEdit, QTableWidget { border: 1px solid #e1e5e9; border-radius: 5px; background: #ffffff; }
            QHeaderView::section { padding: 8px; background: #ffffff; border: none; border-bottom: 1px solid #e1e5e9; }
            QProgressBar { border: none; background: #edf1f0; border-radius: 3px; height: 5px; }
            QProgressBar::chunk { background: #557b73; }
        """)

    def _build_tasks_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(30, 25, 30, 22)
        title = QLabel("发布任务")
        title.setObjectName("title")
        box.addWidget(title)
        subtitle = QLabel("检查数据、确认计划，再将图层发布到指定 GeoServer。")
        subtitle.setObjectName("muted")
        box.addWidget(subtitle)
        toolbar = QHBoxLayout()
        self.new_button = QPushButton("新建任务")
        self.restore_button = QPushButton("恢复任务")
        self.history_button = QPushButton("任务历史")
        self.folder_button = QPushButton("打开任务目录")
        self.folder_button.setEnabled(False)
        for widget in (self.new_button, self.restore_button, self.history_button):
            toolbar.addWidget(widget)
            self._editable.append(widget)
        toolbar.addStretch()
        toolbar.addWidget(self.folder_button)
        box.addLayout(toolbar)
        self.new_button.clicked.connect(self._new_task)
        self.restore_button.clicked.connect(self._choose_plan)
        self.history_button.clicked.connect(self._toggle_history)
        self.folder_button.clicked.connect(self._open_output)
        self.history = QListWidget()
        self.history.setMaximumHeight(130)
        self.history.setVisible(False)
        self.history.itemDoubleClicked.connect(self._open_history)
        box.addWidget(self.history)
        self.step_label = QLabel("1  选择数据")
        self.step_label.setObjectName("step")
        box.addWidget(self.step_label)
        self.steps = QStackedWidget()
        self.steps.addWidget(self._build_import())
        self.steps.addWidget(self._build_review())
        self.steps.addWidget(self._build_result())
        box.addWidget(self.steps, 1)
        self.activity = QLabel("请先在环境设置中配置 GeoServer 和受控 GDAL 工具目录。")
        self.activity.setTextFormat(Qt.TextFormat.PlainText)
        self.activity.setWordWrap(True)
        self.activity.setObjectName("muted")
        box.addWidget(self.activity)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        box.addWidget(self.progress)
        return page

    def _path_row(self, edit: QLineEdit, callback: Callable, label: str = "浏览") -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        self._editable.extend((edit, button))
        return row

    def _build_import(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("拖入 SHP、GeoTIFF 或目录。Mosaic / Pyramid 拼接或金字塔数据暂不支持。")
        hint.setWordWrap(True)
        box.addWidget(hint)
        self.sources = SourceList()
        self.sources.paths_dropped.connect(self.add_sources)
        self._editable.append(self.sources)
        box.addWidget(self.sources)
        row = QHBoxLayout()
        for label, action in (
            ("添加文件", self._choose_sources),
            ("添加目录", self._choose_source_folder),
            ("清空", self._clear_sources),
        ):
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
            self._editable.append(button)
        row.addStretch()
        self.source_count = QLabel("尚未添加数据")
        row.addWidget(self.source_count)
        box.addLayout(row)
        self.independent_tiffs = QCheckBox("将所选 TIFF 分别发布为独立图层")
        self.independent_tiffs.setToolTip(
            "多个 TIFF 需确认分别发布；更改数据后需要重新确认。发现 Mosaic / Pyramid 结构时会停止检查。"
        )
        box.addWidget(self.independent_tiffs)
        source_model = self.sources.model()
        for signal in (
            source_model.rowsInserted,
            source_model.rowsRemoved,
            source_model.rowsMoved,
            source_model.dataChanged,
            source_model.modelReset,
        ):
            signal.connect(self._sources_changed)
        form = QFormLayout()
        self.style_path = QLineEdit()
        self.style_path.setPlaceholderText("留空时使用按数据类型生成的简单样式")
        form.addRow("SLD 样式", self._path_row(self.style_path, self._choose_style))
        self.encoding = QComboBox()
        self.encoding.addItem("使用 .cpg 声明", None)
        self.encoding.addItem("UTF-8", "UTF-8")
        self.encoding.addItem("GBK", "GBK")
        self.crs = QLineEdit()
        self.crs.setPlaceholderText("仅当已确认缺失 CRS 时填写，例如 EPSG:4326")
        form.addRow("SHP 编码", self.encoding)
        form.addRow("声明缺失 CRS", self.crs)
        self.target_name = QLineEdit()
        self.target_name.setPlaceholderText("留空时从文件名生成；单数据集可手工指定")
        form.addRow("图层名称", self.target_name)
        self.strategy = QComboBox()
        self.strategy.addItem("同名时停止（推荐）", "fail")
        self.strategy.addItem("跳过同名图层", "skip")
        self.strategy.addItem("创建独立新版本", "new-version")
        form.addRow("同名处理", self.strategy)
        self.wfs = QCheckBox("同时验收 WFS（仅在环境允许时启用）")
        form.addRow("", self.wfs)
        self.output_root = QLineEdit(str(self._work_dir))
        form.addRow("任务产物目录", self._path_row(self.output_root, self._choose_output))
        box.addLayout(form)
        note = QLabel("检查会生成独立发布副本，保留源文件。坐标声明不会重投影数据。")
        note.setObjectName("muted")
        box.addWidget(note)
        box.addStretch()
        self.inspect_button = QPushButton("检查并生成计划")
        self.inspect_button.setObjectName("primary")
        self.inspect_button.clicked.connect(self._start_plan)
        box.addWidget(self.inspect_button, 0, Qt.AlignmentFlag.AlignRight)
        self._editable.extend(
            (
                self.encoding,
                self.crs,
                self.target_name,
                self.strategy,
                self.wfs,
                self.independent_tiffs,
                self.inspect_button,
            )
        )
        for edit in (self.style_path, self.crs, self.target_name, self.output_root):
            edit.textChanged.connect(self._invalidate_plan)
        self.encoding.currentIndexChanged.connect(self._invalidate_plan)
        self.strategy.currentIndexChanged.connect(self._invalidate_plan)
        self.wfs.toggled.connect(self._invalidate_plan)
        self.independent_tiffs.toggled.connect(self._invalidate_plan)
        return page

    def _build_review(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        self.plan_summary = QLabel()
        self.plan_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.plan_summary.setWordWrap(True)
        box.addWidget(self.plan_summary)
        self.resources_table = QTableWidget(0, 4)
        self.resources_table.setHorizontalHeaderLabels(["目标图层", "类型", "坐标系", "检查结果"])
        self.resources_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.resources_table.verticalHeader().hide()
        self.resources_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.resources_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        box.addWidget(self.resources_table, 2)
        self.check_details = QPlainTextEdit()
        self.check_details.setReadOnly(True)
        self.check_details.setPlaceholderText("预检问题与发布计划说明")
        box.addWidget(self.check_details, 1)
        row = QHBoxLayout()
        self.back_button = QPushButton("调整数据")
        self.back_button.clicked.connect(lambda: self._set_step(0))
        self.execute_button = QPushButton("确认并执行计划")
        self.execute_button.setObjectName("primary")
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self._confirm_run)
        row.addWidget(self.back_button)
        row.addStretch()
        row.addWidget(self.execute_button)
        self._editable.append(self.back_button)
        box.addLayout(row)
        return page

    def _build_result(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        self.result_title = QLabel("等待执行")
        self.result_title.setTextFormat(Qt.TextFormat.PlainText)
        self.result_title.setObjectName("step")
        box.addWidget(self.result_title)
        self.result_summary = QLabel()
        self.result_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.result_summary.setWordWrap(True)
        self.result_summary.setObjectName("muted")
        box.addWidget(self.result_summary)
        self.preview_selector = QComboBox()
        self.preview_selector.currentIndexChanged.connect(self._select_preview)
        box.addWidget(self.preview_selector)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.preview = QLabel("发布后在这里显示 GeoServer 实际返回的 WMS 地图。")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(240)
        self.preview.setStyleSheet("border: 1px solid #e1e5e9; border-radius: 6px; color: #6b7280;")
        self.preview.setWordWrap(True)
        splitter.addWidget(self.preview)
        self.run_log = QPlainTextEdit()
        self.run_log.setReadOnly(True)
        splitter.addWidget(self.run_log)
        splitter.setSizes([360, 160])
        box.addWidget(splitter, 1)
        controls = QHBoxLayout()
        self.cancel_button = QPushButton("安全取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._request_cancel)
        self.report_button = QPushButton("打开任务报告")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self._open_report)
        self.detail_button = QPushButton("运行详情")
        self.detail_button.setCheckable(True)
        self.detail_button.setChecked(True)
        self.detail_button.toggled.connect(self.run_log.setVisible)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.detail_button)
        controls.addStretch()
        controls.addWidget(self.report_button)
        box.addLayout(controls)
        return page

    def _build_settings_page(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(30, 25, 30, 22)
        title = QLabel("环境设置")
        title.setObjectName("title")
        outer.addWidget(title)
        hint = QLabel("首次保存环境档案，后续可重复使用。凭据值不写入档案或任务。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setVerticalSpacing(12)
        self.env_key = QLineEdit("local-test")
        self.base_url = QLineEdit("http://127.0.0.1:8080/geoserver")
        self.workspace = QLineEdit("geopub")
        self.gdal_bin = QLineEdit()
        self.gdal_bin.setPlaceholderText("选择包含 gdalinfo、ogrinfo 等命令的受控目录")
        self.gdal_version = QLineEdit("3.13.3")
        self.ca_bundle = QLineEdit()
        self.ca_bundle.setPlaceholderText("自签名证书环境可选择 CA 文件")
        self.allow_http = QCheckBox("允许此已授权内网环境使用 HTTP")
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 3600)
        self.timeout.setValue(120)
        self.timeout.setSuffix(" 秒")
        self.username_env = QLineEdit("GEOPUB_USERNAME")
        self.password_env = QLineEdit("GEOPUB_PASSWORD")
        self.session_username = QLineEdit()
        self.session_username.setPlaceholderText("可留空，执行时从指定环境变量读取")
        self.session_password = QLineEdit()
        self.session_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.session_password.setPlaceholderText("仅保存在本次进程内存")
        for label, widget in (
            ("环境标识", self.env_key),
            ("GeoServer 地址", self.base_url),
            ("既有工作空间", self.workspace),
            ("GDAL 工具目录", self._path_row(self.gdal_bin, self._choose_gdal)),
            ("预期 GDAL 版本", self.gdal_version),
            ("CA 证书", self._path_row(self.ca_bundle, self._choose_ca)),
            ("", self.allow_http),
            ("请求超时", self.timeout),
            ("用户名变量名", self.username_env),
            ("密码变量名", self.password_env),
            ("本次用户名", self.session_username),
            ("本次密码", self.session_password),
        ):
            form.addRow(label, widget)
        for edit in (
            self.env_key,
            self.base_url,
            self.workspace,
            self.gdal_bin,
            self.gdal_version,
            self.ca_bundle,
            self.username_env,
            self.password_env,
        ):
            edit.textChanged.connect(self._invalidate_plan)
            self._editable.append(edit)
        self.allow_http.toggled.connect(self._invalidate_plan)
        self.timeout.valueChanged.connect(self._invalidate_plan)
        self._editable.extend((self.allow_http, self.timeout, self.session_username, self.session_password))
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        load = QPushButton("载入环境档案")
        save = QPushButton("保存环境档案")
        clear = QPushButton("清除本次凭据")
        load.clicked.connect(self._choose_environment)
        save.clicked.connect(self._save_environment)
        clear.clicked.connect(self._clear_credentials)
        for button in (load, save, clear):
            buttons.addWidget(button)
            self._editable.append(button)
        buttons.addStretch()
        outer.addLayout(buttons)
        return container

    def environment(self) -> Environment:
        version = self.gdal_version.text().strip()
        if not version:
            raise ConfigError("GDAL_VERSION_REQUIRED", "请填写已验证的 GDAL 版本，避免无提示切换运行时。")
        return Environment(
            environment_key=self.env_key.text().strip(),
            base_url=self.base_url.text().strip(),
            workspace=self.workspace.text().strip(),
            gdal_bin=self.gdal_bin.text().strip(),
            gdal_version=version,
            username_env=self.username_env.text().strip(),
            password_env=self.password_env.text().strip(),
            ca_bundle=self.ca_bundle.text().strip() or None,
            allow_http=self.allow_http.isChecked(),
            timeout=float(self.timeout.value()),
        )

    def apply_environment(self, environment: Environment) -> None:
        self._building_forms = True
        for widget, value in (
            (self.env_key, environment.environment_key),
            (self.base_url, environment.base_url),
            (self.workspace, environment.workspace),
            (self.gdal_bin, environment.gdal_bin),
            (self.gdal_version, environment.gdal_version or ""),
            (self.ca_bundle, environment.ca_bundle or ""),
            (self.username_env, environment.username_env),
            (self.password_env, environment.password_env),
        ):
            widget.setText(value)
        self.allow_http.setChecked(environment.allow_http)
        self.timeout.setValue(int(environment.timeout))
        self._clear_credentials()
        self._building_forms = False
        self._invalidate_plan()

    def _navigate(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.tasks_nav.setChecked(index == 0)
        self.settings_nav.setChecked(index == 1)

    def _set_step(self, index: int) -> None:
        self.steps.setCurrentIndex(index)
        self.step_label.setText(("1  选择数据", "2  检查与预览", "3  发布与结果")[index])

    def _invalidate_plan(self, *_: object) -> None:
        if self._building_forms:
            return
        self._plan = None
        self.execute_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.activity.setText("输入已改变，请重新检查并生成计划。")

    def add_sources(self, paths: list[str]) -> None:
        known = {self.sources.item(i).text() for i in range(self.sources.count())}
        for raw in paths:
            path = Path(raw).expanduser().absolute()
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                continue
            if path.is_file() and path.suffix.lower() not in {".shp", ".tif", ".tiff"}:
                continue
            value = str(path)
            if value not in known:
                self.sources.addItem(value)
                known.add(value)

    def _clear_sources(self) -> None:
        self.sources.clear()
        self._sources_changed()

    def _sources_changed(self, *_: object) -> None:
        if self._building_forms:
            return
        count = self.sources.count()
        self.source_count.setText(f"已选择 {count} 个文件或目录" if count else "尚未添加数据")
        self.independent_tiffs.setChecked(False)
        self._invalidate_plan()

    def _can_execute_plan(self) -> bool:
        return bool(
            self._plan and self._plan.get("ready") and not _needs_raster_confirmation(self._plan)
        )

    def _choose_sources(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择数据", "", "空间数据 (*.shp *.tif *.tiff)")
        self.add_sources(paths)

    def _choose_source_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择数据目录")
        if selected:
            self.add_sources([selected])

    def _choose_style(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 SLD 样式", "", "SLD 样式 (*.sld)")
        if path:
            self.style_path.setText(path)

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择任务产物目录", self.output_root.text())
        if path:
            self.output_root.setText(path)
            self._work_dir = Path(path)
            self._refresh_history()

    def _choose_gdal(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择受控 GDAL 命令目录", self.gdal_bin.text())
        if path:
            self.gdal_bin.setText(path)

    def _choose_ca(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 CA 证书", "", "证书 (*.pem *.crt);;全部文件 (*)")
        if path:
            self.ca_bundle.setText(path)

    def _clear_credentials(self) -> None:
        self.session_username.clear()
        self.session_password.clear()

    def _choose_environment(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "载入环境档案", str(self._work_dir), "JSON 环境档案 (*.json)"
        )
        if path:
            try:
                self.apply_environment(Environment.load(path))
                self.activity.setText("已载入环境档案。")
            except (ConfigError, ValueError) as exc:
                QMessageBox.warning(self, "环境档案无效", str(exc))

    def save_environment(self, path: Path | str) -> None:
        save_json(path, self.environment().to_dict())

    def _save_environment(self) -> None:
        try:
            environment = self.environment()
        except ConfigError as exc:
            QMessageBox.warning(self, "请完善环境设置", str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存环境档案",
            str(self._work_dir / f"{environment.environment_key}.json"),
            "JSON 环境档案 (*.json)",
        )
        if path:
            try:
                self.save_environment(path)
            except OSError:
                QMessageBox.warning(self, "无法保存", "请检查文件路径和目录写入权限。")

    def _start_plan(self) -> None:
        if self._thread is not None:
            return
        try:
            env = self.environment()
            if not self.sources.count():
                raise ConfigError("NO_DATASETS", "请添加 SHP、GeoTIFF 或数据目录。")
            if not self.output_root.text().strip():
                raise ConfigError("OUTPUT_REQUIRED", "请选择任务产物目录。")
        except ConfigError as exc:
            QMessageBox.warning(self, "尚不能检查", str(exc))
            return
        source_paths = [Path(self.sources.item(i).text()) for i in range(self.sources.count())]
        self._work_dir = Path(self.output_root.text()).expanduser().absolute()
        output = self._work_dir / (datetime.now().strftime("task-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        options = {
            "strategy": self.strategy.currentData(),
            "encoding": self.encoding.currentData(),
            "declared_crs": self.crs.text().strip() or None,
            "sld": self.style_path.text().strip() or None,
            "target_name": self.target_name.text().strip() or None,
            "enable_wfs": self.wfs.isChecked(),
            "raster_mode": "independent" if self.independent_tiffs.isChecked() else None,
        }
        self.activity.setText("正在检查数据并生成独立发布副本……")
        self._start_worker(lambda _: build_plan(env, source_paths, output, **options), self.review_plan)

    def review_plan(self, plan: dict) -> None:
        self._plan = plan
        self.folder_button.setEnabled(True)
        resources = plan.get("resources", [])
        env = plan["environment"]
        summary = (
            f"目标：{env['base_url']}\n工作空间：{env['workspace']}  ·  {len(resources)} 个资源  ·  策略：{plan['strategy']}"
        )
        raster_summary = _raster_summary(plan)
        if raster_summary:
            summary += "\n" + raster_summary
        self.plan_summary.setText(summary)
        self.resources_table.setRowCount(len(resources))
        details = []
        for row, resource in enumerate(resources):
            errors, warnings = resource.get("blocking_errors", []), resource.get("warnings", [])
            inspection = resource.get("inspection", {})
            state = "阻断" if errors else ("通过，有提示" if warnings else "通过")
            values = (
                resource["target_name"],
                resource.get("kind", ""),
                inspection.get("crs") or "未确认",
                state,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.resources_table.setItem(row, column, item)
            details.append(f"{resource['target_name']}：{state}")
            for label, issues in (("阻断", errors), ("提示", warnings)):
                for issue in issues:
                    message = (
                        issue.get("message", issue.get("code", "")) if isinstance(issue, dict) else str(issue)
                    )
                    details.append(f"  {label}：{message}")
        details.append("\n发布后将使用实际 WMS 地图验收。目录 SQL 如有生成，仍需另行登记。")
        self.check_details.setPlainText("\n".join(details))
        self.execute_button.setEnabled(self._can_execute_plan() and self._thread is None)
        self.activity.setText(
            raster_summary
            if _needs_raster_confirmation(plan)
            else (
                "检查通过，执行前请确认目标环境。"
                if plan.get("ready")
                else "存在阻断问题。请调整数据或配置，再生成新计划。"
            )
        )
        self._navigate(0)
        self._set_step(1)

    def restore_plan(self, path: Path | str) -> None:
        plan = load_plan(path)
        self.apply_environment(Environment(**plan["environment"]))
        self._work_dir = Path(plan["output_dir"]).parent
        self.output_root.setText(str(self._work_dir))
        self.review_plan(plan)

    def _choose_plan(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择已有任务的 plan.json", str(self._work_dir), "JSON 计划 (*.json)"
        )
        if path:
            try:
                self.restore_plan(path)
            except (ConfigError, ValueError) as exc:
                QMessageBox.warning(self, "无法恢复计划", str(exc))

    def _confirm_run(self) -> None:
        if self._thread is not None or not self._can_execute_plan():
            return
        env = self._plan["environment"]
        user, password = self.session_username.text(), self.session_password.text()
        if bool(user) != bool(password):
            QMessageBox.warning(
                self, "凭据未完整填写", "请同时填写本次用户名和密码，或同时留空使用环境变量。"
            )
            return
        raster_summary = _raster_summary(self._plan)
        message = (
            f"目标：{env['base_url']}\n工作空间：{env['workspace']}\n"
            f"计划资源：{len(self._plan['resources'])} 个\n同名策略：{self._plan['strategy']}\n"
            + (raster_summary + "\n" if raster_summary else "")
            + "\n"
            "将执行此计划中的数据上传、图层创建和样式绑定，并保存验收报告。\n"
            "本版本不会原位覆盖已有数据，也不会执行数据库 SQL。"
        )
        answer = QMessageBox.question(
            self,
            "确认执行发布计划",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._cancel = threading.Event()
        credentials = (user, password) if user and password else None
        plan = self._plan
        journal_path = Path(plan["output_dir"]).parent / "geopublisher.sqlite"
        self._set_step(2)
        self.run_log.clear()
        self.result_title.setText("正在发布")
        self.result_summary.setText(f"工作空间：{env['workspace']}  ·  {len(plan['resources'])} 个资源")
        self.detail_button.setChecked(True)
        self.preview.clear()
        self.preview.setText("等待 GeoServer 实际 WMS 地图。")
        self.preview_selector.clear()
        self._preview_paths.clear()
        self._preview_pixmap = None
        self._report_path = None
        self.report_button.setEnabled(False)
        self.cancel_button.setEnabled(True)

        def execute(on_event: Callable) -> dict:
            from .engine import run_plan

            return run_plan(
                plan,
                journal_path=journal_path,
                credentials=credentials,
                on_event=on_event,
                cancel_event=self._cancel,
            )

        self._start_worker(execute, self.show_result)

    def _start_worker(self, operation: Callable, success: Callable) -> None:
        self._worker_success = success
        self._thread = QThread(self)
        self._worker = _Worker(operation)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.completed.connect(self._worker_completed)
        self._worker.failed.connect(self._worker_failed)
        self._worker.event.connect(self._receive_event)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._set_busy(True)
        self._thread.start()

    def _set_busy(self, busy: bool) -> None:
        for widget in self._editable:
            widget.setEnabled(not busy)
        self.tasks_nav.setEnabled(not busy)
        self.settings_nav.setEnabled(not busy)
        self.history.setEnabled(not busy)
        self.execute_button.setEnabled(not busy and self._can_execute_plan())
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0 if busy else 1)

    @Slot(object)
    def _worker_completed(self, value: dict) -> None:
        if self._worker_success:
            self._worker_success(value)

    @Slot(object)
    def _worker_failed(self, error: dict) -> None:
        message = f"{error['code']}：{error['message']}"
        self.activity.setText(message)
        if self.steps.currentIndex() == 2:
            self.result_title.setText("执行未完成，请核对任务状态")
            self.run_log.appendPlainText(message)
        else:
            self._plan = None
            self.execute_button.setEnabled(False)
            QMessageBox.warning(self, "操作未完成", message)

    @Slot()
    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._worker_success = None
        self._set_busy(False)
        self.cancel_button.setEnabled(False)
        self._refresh_history()

    @Slot(object)
    def _receive_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        text = event.get("text") or event.get("message") or event.get("status") or event.get("stage")
        if text:
            prefix = event.get("resource") or event.get("resource_id")
            rendered = f"{prefix} · {text}" if prefix else str(text)
            self.activity.setText(rendered)
            self.run_log.appendPlainText(rendered)

    def _request_cancel(self) -> None:
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self.activity.setText("已请求安全取消；当前操作结束后将在安全检查点停止。")

    def show_result(self, result: dict) -> None:
        status = result.get("status", "MANUAL_ACTION_REQUIRED")
        self._set_step(2)
        self.result_title.setText(_status_label(status))
        self.activity.setText(
            f"任务状态：{_status_label(status)}。服务发布状态与业务目录登记状态请分别查看。"
        )
        self.run_log.appendPlainText(f"\n任务状态：{status}")
        if self._plan is None:
            return
        output = Path(self._plan["output_dir"]).resolve()
        self.run_log.appendPlainText(f"任务目录：{output}")
        candidate = result.get("report_path")
        possible_reports = [Path(candidate)] if isinstance(candidate, str) else []
        possible_reports.extend((output / "report.md", output / "REPORT.md"))
        self._report_path = next(
            (p.resolve() for p in possible_reports if p.is_file() and p.resolve().is_relative_to(output)),
            None,
        )
        self.report_button.setEnabled(self._report_path is not None)
        preview_paths: list[tuple[str, Path]] = []
        resources = result.get("resources", [])
        if isinstance(resources, dict):
            resources = list(resources.values())
        succeeded = sum(isinstance(r, dict) and r.get("status") == "SUCCEEDED" for r in resources)
        skipped = sum(isinstance(r, dict) and r.get("status") == "SKIPPED" for r in resources)
        catalog = result.get("catalog_message") or "业务目录状态请查看任务报告。"
        self.result_summary.setText(
            f"{len(resources)} 个资源  ·  {succeeded} 个执行成功  ·  {skipped} 个跳过\n{catalog}"
        )
        self.detail_button.setChecked(status != "SUCCEEDED")
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            name = resource.get("target_name") or resource.get("resource_id") or "地图"
            self.run_log.appendPlainText(f"{name}：{_status_label(resource.get('status', '待核对'))}")
            raw = resource.get("preview_path")
            if isinstance(raw, str):
                preview_paths.append((str(name), Path(raw)))
        # Only inspect this task's known artifact subfolders, never source trees.
        for resource in self._plan.get("resources", []):
            folder = output / resource["resource_id"]
            for name in ("preview.png", "wms.png", "wms-preview.png"):
                preview_paths.append((resource["target_name"], folder / name))
        self.preview_selector.clear()
        self._preview_paths = []
        seen = set()
        for name, path in preview_paths:
            resolved = path.resolve()
            if resolved in seen or not resolved.is_relative_to(output) or not resolved.is_file():
                continue
            if resolved.suffix.lower() != ".png":
                continue
            seen.add(resolved)
            self._preview_paths.append(resolved)
            self.preview_selector.addItem(name)
        if self._preview_paths:
            self._select_preview(0)
        else:
            self.preview.clear()
            self.preview.setText("没有可展示的 WMS 验收图。请查看任务状态和报告。")

    def _select_preview(self, index: int) -> None:
        if not 0 <= index < len(self._preview_paths):
            return
        pixmap = QPixmap(str(self._preview_paths[index]))
        if pixmap.isNull():
            self.preview.setText("验收图无法解码，请查看任务报告。")
            self._preview_pixmap = None
            return
        self._preview_pixmap = pixmap
        self._scale_preview()

    def _scale_preview(self) -> None:
        if self._preview_pixmap is not None:
            self.preview.setPixmap(
                self._preview_pixmap.scaled(
                    self.preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._scale_preview()

    def _new_task(self) -> None:
        self._plan = None
        self._clear_sources()
        self._set_step(0)
        self.execute_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.run_log.clear()
        self._report_path = None
        self.report_button.setEnabled(False)

    def _toggle_history(self) -> None:
        self._refresh_history()
        self.history.setVisible(not self.history.isVisible())

    def _refresh_history(self) -> None:
        self.history.clear()
        journal_path = self._work_dir / "geopublisher.sqlite"
        if not journal_path.is_file():
            return
        try:
            from .journal import Journal

            with Journal(journal_path) as journal:
                for task in journal.list_tasks():
                    plan = task["plan"]
                    item = QListWidgetItem(
                        f"{_status_label(task['status'])}  ·  {task['created_at'][:19]}  ·  {plan.get('environment', {}).get('workspace', '')}"
                    )
                    item.setData(Qt.ItemDataRole.UserRole, str(Path(plan["output_dir"]) / "plan.json"))
                    self.history.addItem(item)
        except Exception:
            self.activity.setText("任务历史暂时无法读取；可通过恢复任务选择已有 plan.json。")

    def _open_history(self, item: QListWidgetItem) -> None:
        try:
            self.restore_plan(item.data(Qt.ItemDataRole.UserRole))
            self.history.hide()
        except (ConfigError, ValueError) as exc:
            QMessageBox.warning(self, "无法恢复任务", str(exc))

    def _open_output(self) -> None:
        if self._plan:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._plan["output_dir"]).resolve())))

    def _open_report(self) -> None:
        if self._report_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._report_path)))

    def closeEvent(self, event: object) -> None:
        if self._thread is not None:
            if self.steps.currentIndex() != 2:
                QMessageBox.information(
                    self, "数据检查仍在运行", "本次数据检查结束后可关闭窗口。请保留窗口等待检查完成。"
                )
                event.ignore()
                return
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "是否请求安全取消？窗口将在任务停止后允许关闭。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._request_cancel()
            event.ignore()
            return
        self._clear_credentials()
        event.accept()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("GeoPublisher Workbench")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
