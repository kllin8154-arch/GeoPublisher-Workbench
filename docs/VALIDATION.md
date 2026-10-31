# 0.1.0 开发预览验证记录

本记录区分本地实测、模拟故障测试和尚待现场验收的部分。所有 GIS 数据均由数学规则生成，没有使用真实项目数据或业务数据库。

## 多 TIFF 改动的 Linux 增量验证

在 `bb83e77` 基线上实现导入意图和已知结构阻断后，全量 **414 passed**，原生 GDAL 用例实际执行；Ruff、离线锁检查和构建通过。新增可复跑的离线脚本使用中文/空格路径、两份合成 TIFF 和真实 Qt 后台：28 次原生调用完成，未确认/输入变化/结构标记场景均停止，HTTP 客户端创建及 Python socket 连接尝试为 0，源哈希不变。Windows 本次结果尚待验收，详见 [多 TIFF 增量记录](VALIDATION-RASTER-IMPORTS-2026-11-01.md)。下述历史基线保留原数值。

## 最新 Windows 本地验证

PR #1（`d8bb643`）已在保留原文档的独立分支完成增量验证：**332 passed, 4 skipped**，新增原生 GDAL 用例实际执行；三类凭据配置的中文路径五资源预检通过，原生桌面后台生成计划通过，默认/自定义凭据不进入 GDAL，父进程凭据读取正常。该补丁关闭凭据继承问题，详见 [Windows 报告第 7 节](VALIDATION-WINDOWS-2026-11-01.md#7-pr-1-凭据隔离补丁的-windows-增量验证)。

2026-11-01 已在 Windows 10 原生环境完成 Python 构建、受控 GDAL 3.13.3、原生 Qt 桌面启动与字体烟测、离线计划及一次性 GeoServer 2.24.4 的真实集成验证：`310 passed, 4 skipped`，5 个资源 WMS 全部成功，4 个矢量资源 WFS 全部成功，同计划重跑新增操作记录为 0，精确清理移除 20 个本任务配置对象。

该结果证明 Windows 核心受控链路可运行，但不代表完整离线发行物或生产验收通过。详细环境、结果矩阵、PRD 差距以及 P0/P1 阻断项见 [Windows 本地验证报告（2026-11-01）](VALIDATION-WINDOWS-2026-11-01.md)。

## 本地基线

| 项目 | 实测环境 |
|---|---|
| 操作系统 | Linux x86-64 |
| Python | 3.12.14 |
| GDAL | conda-forge 3.13.3，使用仓库 Pixi 锁文件 |
| GeoServer | 官方 2.24.4 二进制分发，独立临时目录，回环地址 |
| Java | 17 |
| 桌面 | PySide6 Essentials 6.12.0，offscreen |

选择 2.24.4 是为了验证该兼容基线，不表示推荐将旧版本直接用于新建生产环境。其他 GeoServer 版本仍需独立验收。

## 自动测试与构建

```text
ruff check .                       All checks passed
QT_QPA_PLATFORM=offscreen pytest -q 314 passed
python -m build --no-isolation      sdist + wheel built successfully
```

测试覆盖 REST 认证和错误状态、写结果未知、对象所有权、SLD 正文变化、外部修改拒绝清理、SQLite 跨进程锁与崩溃恢复、CRS/编码/组件、透明图像、XML 异常、目录 SQL、桌面计划失效和后台执行约束。

输入边界测试包含非对象 JSON、错误类型、UUID、计划摘要、符号链接、已有任务目录和目录创建竞争，以及 SLD 体积变化后的有界读取。

## 真实 GeoServer 集成

脚本：`scripts/integration_check.py`。使用 5 个合成数据集：UTF-8 点、线、面 SHP，GBK 面 SHP，以及 RGB 渐变 GeoTIFF。

| 验证项 | 结果 |
|---|---|
| 预检、打包、独立上传和图层/样式绑定 | 5 / 5 成功 |
| WMS PNG 真实验收 | 5 / 5 成功 |
| 矢量 WFS 抽样 | 4 / 4 成功；栅格不请求 WFS |
| 首次任务写操作记录 | 20 条 |
| 同任务再次执行新增写操作 | 0 条 |
| 配置清理 | `ROLLED_BACK`，移除 20 个本任务配置对象 |
| 清理后无关示例图层 | 配置快照保持不变 |
| 服务器完成上传后人为丢弃响应 | `MANUAL_ACTION_REQUIRED` |
| 对未知上传任务再次执行 | 仍要求核对，新增写请求 0 次 |

清理调用不递归、不要求 GeoServer 清除实际数据。测试服务内的合成源数据文件仍保留；清理服务配置和回收磁盘空间是两个不同操作。

集成验证中曾发现 GeoServer 在首次 WMS/WFS 后补充默认 `advertised` 属性，导致过严的快照比较误报外部修改。当前比较会规范化已知默认值；实际用户更改和 SLD 正文变更仍会阻止自动恢复/清理。

## SQL 与桌面证据

目录模块完成 34 项单元测试；另外使用临时 pglast 8.7 对 18 份创建/复用、中文、引号、换行和美元定界符冲突脚本进行 SQL 与 PL/pgSQL 语法解析。全部通过。没有连接数据库，因此这不构成真实 schema 或触发器行为的验收。

`docs/images/desktop-preview.png` 是实际 Qt 界面截图，展示真实任务结果及 GeoServer 返回的地图，并显示目录尚未登记。截图没有用静态假结果冒充执行成功。

## Windows 与发行物

已配置 `windows-latest` 与 `ubuntu-24.04` CI，以及 Windows PyInstaller 桌面预览构建。具体远程结果以对应 GitHub Actions 运行记录为准。

首次远程运行 [Actions #1](https://github.com/kllin8154-arch/GeoPublisher-Workbench/actions/runs/41222715209) 已触发，但两个 job 均未启动。GitHub 返回账户侧 runner 资源限制。该结果是运行资源阻塞，不包含任何远程测试或 Windows 构建执行证据；不能据此宣称 Windows 已通过，也没有可下载的 Windows 构建产物。

本地 Windows 10 已于 2026-11-01 完成同一 Python 测试、wheel/sdist、PyInstaller 预览、原生桌面启动、受控 GDAL 和一次性 GeoServer 集成检查。PyInstaller 预览构建成功，但其中仍不包含 GDAL/PROJ；详细结果见 [Windows 本地验证报告](VALIDATION-WINDOWS-2026-11-01.md)。以下命令可用于 Windows 本地复核；远程 CI 仍应在 runner 资源恢复后从 Actions 单独重跑：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
uv run --frozen --no-sync pytest -q
Remove-Item Env:QT_QPA_PLATFORM
uv run --frozen --no-sync pyinstaller --noconfirm --clean --windowed --onedir --name GeoPublisher-Workbench --paths src scripts/desktop_entry.py
```

Windows 10 本机已完成原生桌面启动与中文字体烟测，并验证 Pixi GDAL DLL 布局；这不包含布局、键盘、焦点和完整交互验收。Windows 11、目标 Linux、完整离线安装包及内网断网启动仍待目标机器验收。PyInstaller 预览不包含 GDAL，因此不能当作完整可离线分发的安装包。

## 尚未完成的验收

GDAL 凭据继承已由 PR #1 修复并完成 Windows 增量验证；旧提交的失败证据保留在 Windows 报告第 1–6 节。以下项目仍待验收：

- 本次多 TIFF 意图与已知结构阻断的 Windows 原生增量验收；无标记结构的检测边界及实际组合意图仍需人工核对。
- Windows/Linux 完整离线包、断网安装、许可证清单及干净目标机验收。
- 现场反向代理、证书链、账号权限及目标 GeoServer 版本。
- 真实业务目录字段、默认值、唯一键、外键、触发器与页面可见性。
- 1 GB TIFF、长时间上传、磁盘不足等容量与性能基准。
- 全量无效几何、空间拓扑、完整 SLD XSD 和复杂样式视觉等价。
- Mosaic/Pyramid、已有数据原位更新、缓存一致性和任意历史版本恢复。

这些边界在产品状态和路线中保留，当前成功结果只代表上面列出的样本与故障场景。
