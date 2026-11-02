"""Small Chinese desktop UI sharing the CLI's immutable publishing plans."""

from __future__ import annotations

import os
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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

_STRATEGY_LABELS = {
    "fail": "同名时停止（推荐）",
    "skip": "跳过同名图层（不更新）",
    "new-version": "新建独立图层版本",
}
_KIND_LABELS = {
    "shapefile": "矢量 SHP",
    "geotiff": "栅格 TIFF",
}


def gdb_preview_summary(info: dict, *, include_annotations: bool = False) -> str:
    """Explain local GDB analysis without declaring SHP attributes lossless."""
    layers = info.get("layers", [])
    annotation = sum(bool(layer.get("annotation")) for layer in layers)
    empty = sum(not layer.get("feature_count") for layer in layers)
    rename = sum(
        field.get("renamed", False)
        for layer in layers
        for field in layer.get("field_map", [])
    )
    omitted = sum(
        field.get("omitted", False)
        for layer in layers
        for field in layer.get("field_map", [])
    )
    planned = sum(
        bool(layer.get("feature_count"))
        and bool(layer.get("crs"))
        and (include_annotations or not layer.get("annotation"))
        for layer in layers
    )
    return (
        f"已找到 {len(layers)} 个要素类，共 {info.get('feature_count', 0)} 条要素。\n"
        f"默认计划导出 {planned} 个，注记相关 {annotation} 个，空图层 {empty} 个。\n"
        f"源字段可能需要改短名 {rename} 处，可能省略 {omitted} 处。\n"
        "这是只读分析；确认后才在本地创建 SHP 副本，原始 GDB 不变。"
    )


def result_next_action(status: str) -> str:
    """A short, actionable explanation without implying business registration."""
    if status == "SUCCEEDED":
        return "服务已发布并通过基本 WMS 出图检查。请对照源数据核对位置、颜色和透明区域；业务目录尚未登记，需要另行确认 SQL。"
    if status == "REVIEW_REQUIRED":
        return "地图已返回，但自动检查无法确认内容正确。请检查下方预览与任务报告，人工核对后再用于业务系统。"
    if status == "MANUAL_ACTION_REQUIRED":
        return "服务器写入结果可能不确定。不要重新上传或直接删除，请打开报告，先核对服务器中的实际对象。"
    if status == "PARTIAL_SUCCESS":
        return "部分图层已完成。请逐项查看失败原因，保留已有成功结果，核对后再恢复任务。"
    if status == "SKIPPED":
        return "本次不会更新同名图层。旧图层仍在服务器上，不能把跳过视作新数据发布成功。"
    if status in {"FAILED", "BLOCKED"}:
        return "发布未完成。请查看运行详情和报告，修正数据或环境后再重新检查。"
    if status == "CANCELLED":
        return "已在安全检查点取消，已创建的资源可能仍保留。再次执行前请核对任务报告。"
    return "请根据下方任务报告确认服务状态；业务目录不会自动登记。"


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


def controlled_gdal_bin() -> str:
    """Locate only this application's locked runtime, never system PATH or QGIS.

    Frozen releases keep the versioned GIS runtime next to the application,
    under runtime/. Source checkouts use their own Pixi environment. Failure to
    find a *complete* layout leaves the field empty for explicit configuration.
    """
    if getattr(sys, "frozen", False):
        prefix = Path(sys.executable).resolve().parent / "runtime"
    else:
        prefix = Path(__file__).resolve().parents[2] / ".pixi" / "envs" / "default"
    windows = os.name == "nt"
    library = prefix / "Library" if windows else prefix
    bin_dir = library / "bin"
    binaries = ("gdalinfo.exe", "ogrinfo.exe") if windows else ("gdalinfo", "ogrinfo")
    if (
        all((bin_dir / name).is_file() for name in binaries)
        and (library / "share" / "gdal").is_dir()
        and (library / "share" / "proj" / "proj.db").is_file()
    ):
        return str(bin_dir)
    return ""


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
        self._connection_testing = False
        self._pending_gdb_conversion = None
        self._close_when_idle = False
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
        subtitle = QLabel("按 3 步完成：选择数据 → 本地检查 → 确认发布。首次使用请先完成「环境设置」。")
        subtitle.setObjectName("muted")
        box.addWidget(subtitle)
        toolbar = QHBoxLayout()
        self.new_button = QPushButton("新建任务")
        self.restore_button = QPushButton("恢复任务")
        self.history_button = QPushButton("任务历史")
        self.folder_button = QPushButton("打开任务目录")
        self.help_button = QPushButton("使用帮助")
        self.folder_button.setEnabled(False)
        for widget in (self.new_button, self.restore_button, self.history_button):
            toolbar.addWidget(widget)
            self._editable.append(widget)
        toolbar.addStretch()
        toolbar.addWidget(self.help_button)
        toolbar.addWidget(self.folder_button)
        box.addLayout(toolbar)
        self.new_button.clicked.connect(self._new_task)
        self.restore_button.clicked.connect(self._choose_plan)
        self.history_button.clicked.connect(self._toggle_history)
        self.folder_button.clicked.connect(self._open_output)
        self.help_button.clicked.connect(self._show_quick_help)
        self._editable.append(self.help_button)
        self.history = QListWidget()
        self.history.setMaximumHeight(130)
        self.history.setVisible(False)
        self.history.itemDoubleClicked.connect(self._open_history)
        box.addWidget(self.history)
        self.step_label = QLabel("第 1 步 / 共 3 步 · 选择数据")
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
        hint = QLabel("第 1 步：把 SHP、TIFF 文件或文件夹拖入下方，也可以点「添加文件」。本步只处理本地数据，不会立即上传。")
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
            ("GDB / MXD / LYR 转换", self._arcgis_conversion),
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
        self.source_hint = QLabel("支持 SHP（同目录包含 DBF/SHX/PRJ）和单个 GeoTIFF。多个 TIFF 需明确勾选独立发布；Mosaic/Pyramid 暂不支持。")
        self.source_hint.setObjectName("muted")
        self.source_hint.setWordWrap(True)
        box.addWidget(self.source_hint)
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
        basic_form = QFormLayout()
        self.strategy = QComboBox()
        self.strategy.addItem("同名时停止（推荐）", "fail")
        self.strategy.addItem("跳过同名图层", "skip")
        self.strategy.addItem("创建独立新版本", "new-version")
        self.strategy.setToolTip("推荐保留「同名时停止」。跳过表示不会更新该图层；新版本会使用独立名称。")
        basic_form.addRow("遇到同名图层", self.strategy)
        self.output_root = QLineEdit(str(self._work_dir))
        basic_form.addRow("任务记录保存到", self._path_row(self.output_root, self._choose_output))
        box.addLayout(basic_form)
        self.advanced_import_button = QPushButton("展开高级选项（样式、编码、坐标系、图层名称）")
        self.advanced_import_button.setCheckable(True)
        box.addWidget(self.advanced_import_button)
        self.advanced_import_options = QWidget()
        advanced_form = QFormLayout(self.advanced_import_options)
        self.style_path = QLineEdit()
        self.style_path.setPlaceholderText("留空时自动生成简单样式")
        advanced_form.addRow("单图层 SLD 样式", self._path_row(self.style_path, self._choose_style))
        self.sld_directory = QLineEdit()
        self.sld_directory.setPlaceholderText("QGIS 插件批量导出的 SLD 所在文件夹；需与 SHP/TIFF 同名")
        advanced_form.addRow("批量 SLD 目录", self._path_row(self.sld_directory, self._choose_sld_directory))
        self.remap_sld_fields = QCheckBox("我已核对字段对照：修复 SLD 中的原字段名（仅 GDB 导出的 SHP）")
        self.remap_sld_fields.setToolTip(
            "必须在 SHP 同目录提供 conversion-report.json。逐一校验 SHP、DBF、SHX、PRJ、CPG 的 SHA256；"
            "只改本次计划的 SLD 副本，不修改 QGIS 导出的原 SLD。复杂表达式会阻断。"
        )
        advanced_form.addRow("", self.remap_sld_fields)
        self.encoding = QComboBox()
        self.encoding.addItem("自动读取 .cpg", None)
        self.encoding.addItem("UTF-8", "UTF-8")
        self.encoding.addItem("GBK", "GBK")
        self.crs = QLineEdit()
        self.crs.setPlaceholderText("仅在已确认原数据坐标系时填写，如 EPSG:4326")
        advanced_form.addRow("SHP 文字编码", self.encoding)
        advanced_form.addRow("缺失时声明坐标系", self.crs)
        self.target_name = QLineEdit()
        self.target_name.setPlaceholderText("默认使用文件名；单个文件可指定")
        advanced_form.addRow("自定义图层名称", self.target_name)
        self.wfs = QCheckBox("额外检查 WFS（仅矢量，默认关闭）")
        advanced_form.addRow("", self.wfs)
        self.advanced_import_options.hide()
        self.advanced_import_button.toggled.connect(self.advanced_import_options.setVisible)
        self.advanced_import_button.toggled.connect(
            lambda expanded: self.advanced_import_button.setText(
                "收起高级选项" if expanded else "展开高级选项（样式、编码、坐标系、图层名称）"
            )
        )
        self._editable.append(self.advanced_import_button)
        box.addWidget(self.advanced_import_options)
        note = QLabel("下一步只做本地预检并创建副本，不连接 GeoServer。源文件不变；声明坐标系也不会转换数据。")
        note.setObjectName("muted")
        box.addWidget(note)
        box.addStretch()
        self.inspect_button = QPushButton("下一步：检查数据")
        self.inspect_button.setObjectName("primary")
        self.inspect_button.clicked.connect(self._start_plan)
        box.addWidget(self.inspect_button, 0, Qt.AlignmentFlag.AlignRight)
        self._editable.extend(
            (
                self.encoding,
                self.remap_sld_fields,
                self.crs,
                self.target_name,
                self.strategy,
                self.wfs,
                self.independent_tiffs,
                self.inspect_button,
            )
        )
        for edit in (self.style_path, self.sld_directory, self.crs, self.target_name, self.output_root):
            edit.textChanged.connect(self._invalidate_plan)
        self.encoding.currentIndexChanged.connect(self._invalidate_plan)
        self.strategy.currentIndexChanged.connect(self._invalidate_plan)
        self.wfs.toggled.connect(self._invalidate_plan)
        self.remap_sld_fields.toggled.connect(self._invalidate_plan)
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
        self.review_guidance = QLabel("请确认图层名称、坐标系和检查结果。只有预检通过才能进入发布。")
        self.review_guidance.setObjectName("muted")
        self.review_guidance.setTextFormat(Qt.TextFormat.PlainText)
        self.review_guidance.setWordWrap(True)
        box.addWidget(self.review_guidance)
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
        self.execute_button = QPushButton("确认并发布")
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
        self.result_guidance = QLabel("完成后在这里显示下一步操作建议。")
        self.result_guidance.setTextFormat(Qt.TextFormat.PlainText)
        self.result_guidance.setWordWrap(True)
        box.addWidget(self.result_guidance)
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
        hint = QLabel("首次使用只需填写服务器地址、工作空间和本次登录信息，再点「测试连接」。下方高级设置平时无需修改。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.env_key = QLineEdit("local-test")
        self.base_url = QLineEdit("http://127.0.0.1:8080/geoserver")
        self.workspace = QLineEdit("geopub")
        self.gdal_bin = QLineEdit(controlled_gdal_bin())
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
        self.base_url.setToolTip("例如 https://your-geoserver.example/geoserver。默认的 127.0.0.1 地址仅是示例，并非已连接。")
        self.workspace.setToolTip("GeoServer 中已创建的工作空间名称，不是数据库名称；工具不会自动创建。")
        self.gdal_bin.setToolTip("便携版自动寻找随包的受控 GDAL；找不到时才需要手动选择目录。")
        self.session_username.setToolTip("GeoServer 登录用户名，仅在本次会话内使用。")
        self.session_password.setToolTip("GeoServer 登录密码，不会写入环境档案、任务计划或报告。")
        for label, widget in (
            ("环境名称", self.env_key),
            ("GeoServer 服务地址", self.base_url),
            ("已有工作空间", self.workspace),
            ("GDAL 目录", self._path_row(self.gdal_bin, self._choose_gdal)),
            ("本次用户名", self.session_username),
            ("本次密码", self.session_password),
        ):
            form.addRow(label, widget)
        inner_layout.addLayout(form)
        tip = QLabel("提示：这里的默认服务器地址只是示例。先点「测试连接（只读）」，成功后再回到发布任务。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        inner_layout.addWidget(tip)
        self.advanced_settings_button = QPushButton("展开高级设置（证书、超时、凭据变量等）")
        self.advanced_settings_button.setCheckable(True)
        inner_layout.addWidget(self.advanced_settings_button)
        self.advanced_settings_options = QWidget()
        advanced = QFormLayout(self.advanced_settings_options)
        for label, widget in (
            ("预期 GDAL 版本", self.gdal_version),
            ("CA 证书", self._path_row(self.ca_bundle, self._choose_ca)),
            ("", self.allow_http),
            ("请求超时", self.timeout),
            ("用户名环境变量", self.username_env),
            ("密码环境变量", self.password_env),
        ):
            advanced.addRow(label, widget)
        self.advanced_settings_options.hide()
        self.advanced_settings_button.toggled.connect(self.advanced_settings_options.setVisible)
        self.advanced_settings_button.toggled.connect(
            lambda expanded: self.advanced_settings_button.setText(
                "收起高级设置" if expanded else "展开高级设置（证书、超时、凭据变量等）"
            )
        )
        self._editable.append(self.advanced_settings_button)
        inner_layout.addWidget(self.advanced_settings_options)
        inner_layout.addStretch()
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
        load = QPushButton("载入环境")
        save = QPushButton("保存环境")
        clear = QPushButton("清除本次凭据")
        self.connection_test_button = QPushButton("测试连接（只读）")
        self.connection_test_button.setObjectName("primary")
        load.clicked.connect(self._choose_environment)
        save.clicked.connect(self._save_environment)
        clear.clicked.connect(self._clear_credentials)
        self.connection_test_button.clicked.connect(self._check_connection)
        for button in (load, save, clear, self.connection_test_button):
            buttons.addWidget(button)
            self._editable.append(button)
        outer.addLayout(buttons)
        self.connection_status = QLabel("尚未测试连接。测试只读取服务器版本和工作空间，不会上传或修改数据。")
        self.connection_status.setObjectName("muted")
        self.connection_status.setTextFormat(Qt.TextFormat.PlainText)
        self.connection_status.setWordWrap(True)
        outer.addWidget(self.connection_status)
        for edit in (
            self.env_key, self.base_url, self.workspace, self.ca_bundle,
            self.username_env, self.password_env, self.session_username, self.session_password,
        ):
            edit.textChanged.connect(self._connection_invalidated)
        self.allow_http.toggled.connect(self._connection_invalidated)
        self.timeout.valueChanged.connect(self._connection_invalidated)
        return container

    def _connection_invalidated(self, *_: object) -> None:
        if not self._building_forms:
            self.connection_status.setText("连接信息已改变，请重新点「测试连接（只读）」。")

    def _check_connection(self) -> None:
        """Only GET /rest/about/version and GET the explicitly selected workspace."""
        if self._thread is not None:
            return
        try:
            env = self.environment(connection_only=True)
        except ConfigError as exc:
            self.connection_status.setText(f"设置尚未完整：{exc}。")
            return
        username, password = self.session_username.text(), self.session_password.text()
        if bool(username) != bool(password):
            self.connection_status.setText("请同时填写本次用户名和密码，或都留空并使用配置的环境变量。")
            return

        def probe(_on_event: Callable) -> dict:
            from .geoserver import GeoServerClient

            user, secret = (username, password) if username and password else env.credentials()
            with GeoServerClient(
                env.base_url, user, secret,
                timeout=env.timeout, verify=env.ca_bundle or True,
            ) as client:
                info = client.probe()
                workspace_exists = client.get_workspace(env.workspace) is not None
            return {
                "version": info.get("geoserver_version") or "未识别",
                "workspace_exists": workspace_exists,
                "workspace": env.workspace,
            }

        self._connection_testing = True
        self.connection_status.setText("正在只读检测服务器版本和工作空间，不会上传或修改任何文件。")
        self._start_worker(probe, self._show_connection_result)

    def _show_connection_result(self, result: dict) -> None:
        self._connection_testing = False
        version = result.get("version", "未识别")
        if result.get("workspace_exists"):
            message = (
                f"连接成功 · GeoServer {version} · 工作空间 {result['workspace']} 已存在。"
                + (
                    "发布前还需选择完整的受控 GDAL 目录。"
                    if not self.gdal_bin.text().strip()
                    else "可以返回「发布任务」选择数据。"
                )
            )
        else:
            message = (
                f"服务器已连接（GeoServer {version}），但工作空间 {result['workspace']} 不存在。"
                "请管理员先在 GeoServer 中创建工作空间，再发布。"
            )
        self.connection_status.setText(message)
        self.activity.setText(message)

    def environment(self, *, connection_only: bool = False) -> Environment:
        version = self.gdal_version.text().strip()
        if not version and not connection_only:
            raise ConfigError("GDAL_VERSION_REQUIRED", "请填写已验证的 GDAL 版本，避免无提示切换运行时。")
        return Environment(
            environment_key=self.env_key.text().strip(),
            base_url=self.base_url.text().strip(),
            workspace=self.workspace.text().strip(),
            gdal_bin=self.gdal_bin.text().strip() or ("READ_ONLY_CONNECTION_CHECK" if connection_only else ""),
            gdal_version=version or None,
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
        self.connection_status.setText("已切换环境设置，请重新测试服务器连接（只读）。")

    def _navigate(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.tasks_nav.setChecked(index == 0)
        self.settings_nav.setChecked(index == 1)

    def _set_step(self, index: int) -> None:
        self.steps.setCurrentIndex(index)
        self.step_label.setText(("第 1 步 / 共 3 步 · 选择数据", "第 2 步 / 共 3 步 · 检查与确认", "第 3 步 / 共 3 步 · 发布与结果")[index])

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

    def _choose_sld_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择 QGIS 导出的批量 SLD 目录")
        if directory:
            self.sld_directory.setText(directory)
            self.advanced_import_button.setChecked(True)

    def _export_arcmap_helper(self) -> None:
        from .arcgis_bridge import save_arcmap_helper

        output, _filter = QFileDialog.getSaveFileName(
            self, "保存 ArcMap MXD→SHP/LYR 离线脚本",
            str(self._work_dir.parent / "arcmap_mxd_to_shp.py"),
            "ArcMap Python 脚本 (*.py)",
        )
        if not output:
            return
        try:
            result = save_arcmap_helper(output)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "无法导出 ArcMap 脚本", str(exc))
            return
        QMessageBox.information(
            self, "离线脚本已导出",
            f"已保存：{result}\n"
            "请在装有 ArcMap 10.x/ArcPy 且能访问 MXD 原始数据的离线电脑上执行。\n"
            '用法：python arcmap_mxd_to_shp.py "地图.mxd" "新的输出目录"\n'
            "不会在本程序中自动执行 ArcPy，也不会把数据上传服务器。",
        )

    def _arcgis_conversion(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("GIS 数据与样式离线转换")
        dialog.resize(650, 470)
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "① GDB → SHP：使用受控 GDAL 在本机批量导出要素类，原始 GDB 不变，附字段映射报告。"
            "\n② MXD → SHP + LYR：在装有 ArcMap 10.x 的离线电脑运行辅助脚本；MXD 不能单独还原数据。"
            "\n③ LYR → SLD：导出 QGIS 批量离线脚本，或通过 Style Assistant 手动导出，再按 SHP 名称批量匹配。"
            "\n\n注记层、长字段、中文字段和复杂符号都需要核对；转换成功不等于样式无损。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        annotation = QCheckBox("也导出注记图层的几何边界（不保留真实文字排版）")
        layout.addWidget(annotation)
        gdb_button = QPushButton("GDB 批量转换为 SHP（本地离线）")
        gdb_button.clicked.connect(
            lambda: (dialog.accept(), self._choose_gdb_and_export(annotation.isChecked()))
        )
        layout.addWidget(gdb_button)
        save_button = QPushButton("导出 ArcMap MXD→SHP 辅助脚本")
        save_button.clicked.connect(self._export_arcmap_helper)
        layout.addWidget(save_button)
        demo_button = QPushButton("导出 ArcMap 合成 MXD 测试样本生成脚本")
        demo_button.clicked.connect(self._export_arcmap_demo_helper)
        layout.addWidget(demo_button)
        qgis_button = QPushButton("导出 QGIS LYR→SLD 批量离线转换脚本")
        qgis_button.clicked.connect(self._export_qgis_helper)
        layout.addWidget(qgis_button)
        folder_button = QPushButton("选择 QGIS 导出的 SLD 文件夹")
        folder_button.clicked.connect(lambda: (self._choose_sld_directory(), dialog.accept()))
        layout.addWidget(folder_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.exec()

    def _export_arcmap_demo_helper(self) -> None:
        """Export code which needs REAL ArcMap 10.x to create a valid MXD."""
        from .arcgis_bridge import save_arcmap_helper

        target, _filter = QFileDialog.getSaveFileName(
            self, "导出 ArcMap 合成 MXD 测试脚本",
            str(self._work_dir.parent / "arcmap_create_synthetic_mxd.py"),
            "ArcMap Python 脚本 (*.py)",
        )
        if not target:
            return
        try:
            path = save_arcmap_helper(target, demo=True)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "脚本未导出", str(exc))
            return
        QMessageBox.information(
            self, "合成 MXD 脚本已导出",
            f"脚本：{path}\n"
            "普通 Python 无法伪造有效的 MXD 二进制文件，需使用真正的 ArcMap 10.x / ArcPy。\n"
            "需在真正安装 ArcMap 10.x 的离线电脑准备空白 MXD 模板后执行：\n"
            'python arcmap_create_synthetic_mxd.py "空白模板.mxd" "全新输出目录"\n'
            "脚本只创建数学点、线、面和真实 MXD 副本，不访问地质业务数据。",
        )

    def _export_qgis_helper(self) -> None:
        """Export a QGIS-native, fully offline LYR to SLD batch converter."""
        from .arcgis_bridge import save_qgis_helper

        target, _filter = QFileDialog.getSaveFileName(
            self,
            "导出 QGIS LYR→SLD 离线批量脚本",
            str(self._work_dir.parent / "qgis_lyr_to_sld.py"),
            "Python 脚本 (*.py)",
        )
        if not target:
            return
        try:
            path = save_qgis_helper(target)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "QGIS 脚本未导出", str(exc))
            return
        QMessageBox.information(
            self,
            "QGIS 离线脚本已导出",
            f"脚本已保存：{path}\n"
            "请用 QGIS 自带的 python-qgis.bat 执行，而不是普通 Python。\n"
            '用法：python-qgis.bat qgis_lyr_to_sld.py "同名 SHP/LYR 目录" "全新样式输出目录"\n'
            "需要先在本机 QGIS 安装 Style Assistant 0.4.0 和 SLYR Community。\n"
            "所有转换、报告仅保存在本地，不覆盖原文件，也不自动发布到 GeoServer。",
        )

    def _choose_gdb_and_export(self, include_annotations: bool = False) -> None:
        """Explicit local batch conversion, never a GeoServer upload."""
        if self._thread is not None:
            return
        gdb_path = QFileDialog.getExistingDirectory(
            self, "选择本地 .gdb 文件夹（原始数据保持不变）",
            str(self._work_dir),
        )
        if not gdb_path:
            return
        source = Path(gdb_path).absolute()
        if source.suffix.casefold() != ".gdb":
            QMessageBox.warning(self, "没有选择 GDB", "请进入数据所在目录并选择以 .gdb 结尾的文件夹。")
            return
        try:
            from .datasets import DatasetError, GdalRuntime

            runtime = GdalRuntime(
                self.gdal_bin.text().strip(),
                expected_version=self.gdal_version.text().strip() or "3.13.3",
                credential_env_names=(self.username_env.text(), self.password_env.text()),
            )
        except (DatasetError, ValueError) as exc:
            QMessageBox.warning(self, "请先配置 GDAL", str(exc))
            return
        from .gdb_export import inspect_gdb

        self.activity.setText("正在只读分析 GDB 图层与字段，不会生成 SHP 或上传服务器。")
        self._start_worker(
            lambda _event: inspect_gdb(runtime, source),
            lambda info: self._show_gdb_preview(info, runtime, source, include_annotations),
        )

    def _select_gdb_layers(self, info: dict, include_annotations: bool) -> list[str] | None:
        """One checkable list makes skipped/annotation layers clear before work."""
        dialog = QDialog(self)
        dialog.setWindowTitle("GDB 图层检查 · 选择要导出的图层")
        dialog.resize(740, 560)
        layout = QVBoxLayout(dialog)
        summary = QLabel(gdb_preview_summary(info, include_annotations=include_annotations))
        summary.setWordWrap(True)
        summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(summary)
        list_widget = QListWidget()
        layout.addWidget(list_widget)
        for layer in info.get("layers", []):
            count = int(layer.get("feature_count") or 0)
            reason = (
                "空图层，默认跳过" if not count else
                "注记边界，非文字样式" if layer.get("annotation") else
                "需人工核对字段/样式" if layer.get("warnings") else "普通图层"
            )
            item = QListWidgetItem(
                f"{layer['name']} · {count} 条 · {layer.get('geometry', '未知类型')} · {reason}"
            )
            item.setData(Qt.ItemDataRole.UserRole, layer["name"])
            allowed = count > 0 and bool(layer.get("crs")) and (
                include_annotations or not layer.get("annotation")
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if allowed else Qt.CheckState.Unchecked)
            if not allowed:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            list_widget.addItem(item)
        note = QLabel(
            "只有被勾选的图层会转换。字段短名、中文属性和注记布局都需要检查；"
            "失败的部分文件会隔离，不会进入发布列表。"
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        actions.button(QDialogButtonBox.StandardButton.Ok).setText("确认转换选中的图层")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return [
            list_widget.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(list_widget.count())
            if list_widget.item(index).checkState() == Qt.CheckState.Checked
        ]

    def _show_gdb_preview(self, info, runtime, source, include_annotations: bool) -> None:
        chosen = self._select_gdb_layers(info, include_annotations)
        if chosen is None:
            self.activity.setText("已取消本地 GDB 转换；未写出 SHP。")
            return
        if not chosen:
            self.activity.setText("没有勾选可导出的图层；未创建输出目录。")
            QMessageBox.information(self, "尚未选择图层", "请至少勾选一个可导出的图层。")
            return
        self._pending_gdb_conversion = (runtime, source, include_annotations, chosen)
        if self._thread is None:
            # Unit tests may use a synchronous worker; normal Qt work moves on
            # from _thread_finished after the inspect worker has stopped.
            pending, self._pending_gdb_conversion = self._pending_gdb_conversion, None
            self._begin_gdb_conversion(*pending)

    def _begin_gdb_conversion(
        self, runtime, source: Path, include_annotations: bool, names: list[str]
    ) -> None:
        from .gdb_export import convert_gdb

        parent = Path(self.output_root.text()).expanduser().absolute()
        output = parent / (
            datetime.now().strftime("gdb-export-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        )
        self.activity.setText(
            f"正在本地转换所选 {len(names)} 个图层；原始 GDB 不变，也不会上传服务器。"
        )
        self._start_worker(
            lambda _event: convert_gdb(
                runtime, source, output,
                include_annotations=include_annotations, only_layers=names,
            ),
            self._show_gdb_export_result,
        )

    def _show_gdb_export_result(self, report: dict) -> None:
        counts = report.get("summary", {})
        converted = sum(count for status, count in counts.items() if status.startswith("EXPORTED"))
        failed = counts.get("FAILED", 0)
        skipped = sum(count for status, count in counts.items() if status.startswith("SKIPPED"))
        report_file = Path(report["destination"]) / "conversion-report.json"
        message = (
            f"本地转换完成：{converted} 个图层已生成 SHP，{skipped} 个已跳过，{failed} 个失败。\n"
            f"详细报告：{report_file}\n"
            "数据不会自动发布；字段改名、类型差异、注记与 LYR 样式须核对。"
        )
        self.activity.setText(message)
        if not converted:
            QMessageBox.warning(self, "未生成可用 SHP", message)
            return
        choice = QMessageBox.question(
            self, "GDB 转换结果", message + "\n\n将成功导出的 SHP 目录加入当前发布任务吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.add_sources([report["destination"]])

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
            "sld_directory": self.sld_directory.text().strip() or None,
            "remap_sld_fields": self.remap_sld_fields.isChecked(),
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
            f"准备发布到：{env['base_url']}\n"
            f"工作空间：{env['workspace']}  ·  {len(resources)} 个图层  ·  "
            f"同名处理：{_STRATEGY_LABELS.get(plan['strategy'], plan['strategy'])}"
        )
        raster_summary = _raster_summary(plan)
        if raster_summary:
            summary += "\n" + raster_summary
        self.plan_summary.setText(summary)
        self.resources_table.setRowCount(len(resources))
        details = []
        blocked_count = sum(len(item.get("blocking_errors", [])) for item in resources)
        warning_count = sum(len(item.get("warnings", [])) for item in resources)
        if not plan.get("ready") or blocked_count:
            advice = f"有 {blocked_count} 项阻断问题，需要调整数据后重新检查；暂时不能发布。"
        elif _needs_raster_confirmation(plan):
            advice = "多个 TIFF 的独立发布确认尚未记录。返回上一步明确勾选，再重新检查。"
        else:
            advice = f"检查通过。共有 {warning_count} 条提醒，请核对下方信息，再按「确认并发布」。"
        self.review_guidance.setText(advice)
        for row, resource in enumerate(resources):
            errors, warnings = resource.get("blocking_errors", []), resource.get("warnings", [])
            inspection = resource.get("inspection", {})
            state = "阻断" if errors else ("通过，有提示" if warnings else "通过")
            values = (
                resource["target_name"],
                _KIND_LABELS.get(resource.get("kind", ""), resource.get("kind", "")),
                inspection.get("crs") or "未确认",
                state,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.resources_table.setItem(row, column, item)
            details.append(f"{resource['target_name']}：{state}")
            rewritten = resource.get("sld_field_rewrite")
            if isinstance(rewritten, dict):
                changes = rewritten.get("changes", [])
                details.append(f"  字段对照已明确启用：{len(changes)} 个 SLD 字段引用被修正")
                for change in changes:
                    details.append(f"    {change['original']} → {change['shp_field']}")
                details.append("  请用真实 WMS 对照分类、标注及图例；字段匹配成功不等于样式还原。")
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
            f"准备发布到：{env['base_url']}\n工作空间：{env['workspace']}\n"
            f"本次图层：{len(self._plan['resources'])} 个\n"
            f"同名处理：{_STRATEGY_LABELS.get(self._plan['strategy'], self._plan['strategy'])}\n"
            + (raster_summary + "\n" if raster_summary else "")
            + "\n确认后才会向 GeoServer 上传副本、创建图层和样式，并请求 WMS 验证。\n"
            "源文件不会被修改；已有数据不会原位覆盖；业务目录/数据库不会自动写入。\n"
            "如果发布中断，请先检查任务报告，不要直接重复上传。"
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
        self.result_summary.setText(f"工作空间：{env['workspace']}  ·  {len(plan['resources'])} 个图层")
        self.result_guidance.setText("正在执行已确认的任务。下方将显示地图预览；需要排查时可展开「运行详情」。")
        self.detail_button.setChecked(False)
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
        # Queue work only after QThread's event loop is processing events.
        # Running directly from 'started' can complete before exec() begins;
        # a fast worker's queued quit may then race with thread startup.
        self._thread.started.connect(self._worker.run, Qt.ConnectionType.QueuedConnection)
        self._worker.completed.connect(self._worker_completed)
        self._worker.failed.connect(self._worker_failed)
        self._worker.event.connect(self._receive_event)
        # QThread.quit() is documented thread-safe. Call it directly from the
        # worker's finished signal, so exiting the worker loop never depends
        # on a queued callback being serviced by the GUI event loop.
        self._worker.finished.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
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
        if self._close_when_idle:
            return  # Do not open a follow-up dialog after the user requested closing.
        if self._worker_success:
            self._worker_success(value)

    @Slot(object)
    def _worker_failed(self, error: dict) -> None:
        self._pending_gdb_conversion = None
        if self._close_when_idle:
            return
        message = f"{error['code']}：{error['message']}"
        self.activity.setText(message)
        if self._connection_testing:
            self.connection_status.setText("连接未通过：" + message + "。请检查服务器地址、工作空间和凭据。")
            self._connection_testing = False
            return
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
        if self._close_when_idle:
            self._pending_gdb_conversion = None
            self._close_when_idle = False
            QTimer.singleShot(0, self.close)
            return
        self._refresh_history()
        if self._pending_gdb_conversion is not None:
            pending, self._pending_gdb_conversion = self._pending_gdb_conversion, None
            self._begin_gdb_conversion(*pending)

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
        self.result_guidance.setText(result_next_action(str(status)))
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
        self.result_guidance.setText("完成后在这里显示下一步操作建议。")

    def _show_quick_help(self) -> None:
        QMessageBox.information(
            self,
            "GeoPublisher 使用帮助",
            "首次使用（建议先完成环境设置）：\n"
            "1. 填写 GeoServer 地址、工作空间和本次账号密码，点「测试连接（只读）」。\n"
            "2. 返回发布任务，拖入 SHP 或 TIFF；保持默认设置，点「下一步：检查数据」。\n"
            "3. 确认图层、坐标系和提示，点「确认并发布」。完成后查看地图预览。\n\n"
            "遇到复杂样式、编码或缺失坐标系，再展开高级选项。\n"
            "多个 TIFF 必须明确按独立图层发布；Mosaic/Pyramid 拼接暂不支持。\n"
            "遇到失败或未知结果，请先阅读任务报告，不要盲目重试。\n"
            "数据源保持不变，业务数据库不会自动写入。",
        )

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
            if self._close_when_idle:
                event.ignore()
                return
            if self.steps.currentIndex() != 2:
                answer = QMessageBox.question(
                    self,
                    "本地操作仍在运行",
                    "是否在当前检查或转换结束后自动关闭窗口？\n不会强制终止正在处理的文件。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self._close_when_idle = True
                    self._pending_gdb_conversion = None
                    self.activity.setText("已安排操作结束后自动关闭，当前操作继续在本机执行。")
                event.ignore()
                return
            answer = QMessageBox.question(
                self,
                "发布任务仍在运行",
                "是否请求安全取消，并在任务停止后自动关闭？\n当前服务端操作不会被强制中断。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_when_idle = True
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
