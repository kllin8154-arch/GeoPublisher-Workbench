# 多 TIFF 导入意图：Linux、Windows 与 CI 增量验证

日期：2026-11-01（Asia/Shanghai）。基线：PR #1 已合入的 `bb83e77`。本阶段仍为受控开发预览。

## 改动范围与证据归属

新增本地导入检查、CLI/桌面明确选择、schema 2 来源记录及执行前复核。未修改 GDAL 环境隔离策略、REST 请求实现、远程写入顺序或配置清理算法。

本记录先保留 Linux 原始结果，再追加 `6dff92e` 的 Windows 原生验收与远程 CI。Windows 原版 `310 passed, 4 skipped` 与 PR #1 的 `332 passed, 4 skipped` 继续保留在原报告，不能作为本阶段新增功能的 Windows 通过证据。本次没有启动 GeoServer，没有连接生产环境或数据库，没有执行 REST 写操作。

## Linux 自动检查

| 检查 | 本次结果 |
|---|---|
| 全量 pytest | **414 passed**，无跳过 |
| 原生 GDAL 用例 | JUnit 中存在，0.225 秒，无 skipped/failure/error 节点 |
| Ruff | 通过 |
| `uv lock --check --offline` | 通过，依赖锁未修改 |
| sdist / wheel | 构建成功 |
| 格式差异检查 | `git diff --check` 通过 |

测试覆盖：未确认的文件/目录输入在 GDAL 前停止；任意主配置名、BOM、Java properties 续行及转义键；Indexer XML；损坏、超限、不可读或符号链接保留标记；Pyramid 上层目录/层级/单文件与索引 SHP 旁路；无关兄弟目录不受影响；模式、数量、来源和源摘要关联；同数重命名后拒绝旧计划；旧版多 TIFF 在构造客户端前停止；原源文件删除后仍能按所有权清理。

桌面回归覆盖默认未确认、后台参数传递、更改选择清除确认、重复添加不改变确认、预览及执行框展示冻结数量、恢复不覆盖意图，以及旧版多 TIFF 的执行按钮持续禁用。

## 真实 GDAL 与 Qt 后台验证

脚本：`scripts/validate_raster_imports.py`。它在新目录中用数学规则生成数据，再将两份合成 TIFF 放入中文及空格路径。所有预检使用锁定的 GDAL 3.13.3；Qt 使用 PySide6 Essentials 6.12.0 的 `offscreen` 平台，不是模拟 `build_plan()` 返回值。

| 场景 | 结果 |
|---|---|
| 两 TIFF 未明确选择 | `RASTER_INTENT_REQUIRED`；没有调用 GDAL，也没有创建任务目录 |
| 选择 `independent` | ready，2 个资源，均为 EPSG:4326 |
| 保存并恢复 | 完整计划一致，模式 independent，资源仍为 2 |
| 原目录新增 TIFF | `IMPORT_SELECTION_CHANGED`，没有构造 HTTP 客户端 |
| 原目录父层新增 Pyramid 标记 | `UNSUPPORTED_PYRAMID`，没有构造 HTTP 客户端 |
| 显式选择已知 Pyramid 下单 TIFF | 阻断，independent 不覆盖 |
| 仅选择 Mosaic 索引 SHP | `UNSUPPORTED_MOSAIC`，未进入 GDAL |
| 桌面默认不勾选，点击生成按钮 | 后台返回实际数量提示，执行按钮不可用 |
| 点击分别发布复选框后生成 | 真实后台完成，显示 2 TIFF → 2 独立图层 |
| 再增加一个输入 | 清除确认及旧计划，执行按钮不可用 |

实际原生调用 **28 次**：生成样本 12 次、核心两资源预检 8 次、桌面两资源预检 8 次。Python socket 连接尝试 **0**，HTTP 客户端创建尝试 **0**；源 TIFF 哈希前后一致。没有点击执行发布。脚本拦截 Python 网络连接作为离线边界验证；GDAL 继续使用项目原有的最小子环境及 `PROJ_NETWORK=OFF`。

已查看真实 Qt 选择页和检查页截图，载入现有本地 Noto Sans CJK 字体后中文显示正常；980×720 与 1160×830 两种布局的控件和生成按钮可见。这些结果不代替 Windows 平台字体、窗口或运行时验收。截图及生成数据保留在本地忽略目录，仓库只包含复验脚本。

最终 `results.json` 的 SHA256：`e566ea7bf51bc37cb5abad8accbe487fe182363c670241b734ef6b9c192ec3e1`。该 JSON 不包含凭据或现场数据。

## Windows 原生与远程 CI 结果

2026-11-01 已在用户 Windows 10 本机完成 PR #2 增量验收；最终代码提交为 `6dff92e`。全量 **409 passed, 5 skipped**，跳过项均为符号链接权限测试，原生 GDAL 用例实际执行（0.321 秒）。Ruff、sdist/wheel 与指定 uv 0.13.4 的离线锁检查通过。首次 Unix 绝对路径测试夹具问题在 `8b263f8` 修正；CI 默认编码与 Linux Qt 系统库问题在 `6dff92e` 修正，未改变发布业务逻辑。

Qt 使用真实 `windows` 平台。未确认阻断、实际勾选、后台两资源计划、输入变化和已知结构阻断全部通过；28 次 GDAL 调用，0 Python socket 连接尝试、0 HTTP 客户端创建，源哈希不变。`restored_mode` 验证保存文件经 `load_plan()` 读取的结果，不表示烟测点击了完整桌面恢复流程。两张 1740×1245 原生截图已检查中文和控件；截图脚本等待原生绘制过渡后再捕获，并检查保存成功。

[Actions #8](https://github.com/kllin8154-arch/GeoPublisher-Workbench/actions/runs/41286109109) 两平台均 **413 passed, 1 skipped**，唯一跳过项为未安装原生 GDAL；符号链接测试全部执行。Linux/Windows 的 Ruff、sdist/wheel、Windows PyInstaller 预览及产物上传全部通过。CI 预览仍不包含完整 GDAL/PROJ，原生运行时证据来自本机独立验证。最终 Linux 本地复验为 **414 passed**（3.63 秒），原生用例实际执行（0.334 秒）。

Windows 最终证据、完整跳过清单、首次失败及修正过程见 [Windows 报告第 8 节](VALIDATION-WINDOWS-2026-11-01.md#8-pr-2-多-tiff-导入的-windows-增量验证)。Linux 最终 JUnit 的 SHA256 为 `57543330e0d89e2021a8f72e3763396f0201087f42544418603c3040443aec1e`。

## Windows 复验步骤

在已准备的 Windows 项目虚拟环境和 Pixi 运行时中执行。以下 `uv` 应使用已验证的 0.13.4 可执行文件；本次实际锁检查显式使用独立工具目录，测试与构建直接调用项目 `.venv`。先运行原有检查，并保存本次独立 JUnit；符号链接用例可能因权限跳过，需要记录实际项目及数量，不能预填 Linux 通过数。新增 marker 符号链接测试也可能跳过。原生 GDAL 用例仍必须实际执行。

```powershell
$env:PYTHONUTF8 = '1'
$env:QT_QPA_PLATFORM = 'offscreen'
uv run --frozen --no-sync pytest -q -ra --junitxml=data/validation/raster-imports-pytest.xml
uv run --frozen --no-sync ruff check .
uv lock --check --offline
uv run --frozen --no-sync python -m build --no-isolation
Remove-Item Env:QT_QPA_PLATFORM
```

再用原生 `windows` Qt 平台运行合成数据烟测；输出目录必须是新的，不会复用已有任务：

```powershell
uv run --frozen --no-sync python scripts/validate_raster_imports.py --gdal-bin .pixi/envs/default/Library/bin --output data/validation/raster-imports-windows-001 --desktop
```

检查 `results.json` 中的 `desktop.platform` 为 `windows`，并人工查看两个截图的中文与控件布局。脚本会检查未确认阻断、两资源实际计划、来源变化、结构标记与确认清除；预期网络和客户端创建计数均为 0。脚本失败时不要把部分产物计为通过，应使用新输出目录复验。

本次无需启动 GeoServer 或重做全部历史发布链路；REST 写入和清理的历史集成结果仍属于原提交。后续服务端功能变更或完整发行验收再运行对应集成范围。

## 保留的边界

本次实现的是已知结构标记识别和显式意图，不会自动证明一组 TIFF 必然独立。无配置的原始金字塔、无索引的拼接数据，以及不可读或强特征位于读取上限之外的任意命名配置，仍有识别边界。具体规则见 [导入说明](RASTER-IMPORTS.md)。

完整离线包、WMS 地理/样式内容验收、完整失败补偿、任务迁移和现场目录适配仍未由本次测试覆盖。
