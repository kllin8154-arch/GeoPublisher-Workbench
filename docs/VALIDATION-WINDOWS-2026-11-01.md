# Windows 本地验证报告（2026-11-01）

阅读范围：第 1–6 节保留提交 `1e3e324` 的原始验证结论，其中 `310 passed, 4 skipped` 不代表后续补丁。PR #1 的独立 Windows 增量验证及门槛状态见 [第 7 节](#7-pr-1-凭据隔离补丁的-windows-增量验证)。

## 1. 结论

本次在 Windows 10 原生环境中，使用一次性回环 GeoServer 2.24.4、受控 GDAL 3.13.3 和纯合成数据，完成了代码质量、Python 构建、原生桌面启动、离线预检以及真实 GeoServer 发布集成验证。

核心受控链路已经取得可复现证据：5 个资源全部通过 WMS，4 个矢量资源全部通过 WFS；首次运行产生 20 条写操作记录，同一计划重复执行新增操作记录为 0；配置清理删除 20 个本任务对象，且未改变无关图层。上传响应丢失场景按设计进入 `MANUAL_ACTION_REQUIRED`，传输层计数确认重试没有再次写入。

**发布判断：当前版本可视为 Windows 受控开发预览，不应作为生产离线安装包或原始完整 PRD 的验收版本发布。** 在进入现场试运行前，至少应修复 GDAL 子进程继承 GeoServer 凭据的问题、确认是否接受多 TIFF 独立发布对原始 PRD 的偏离，并完成包含 GDAL/PROJ 的 Windows 与 Linux 离线发行物验收。

本次没有访问生产 GeoServer、业务数据库或真实空间数据，也没有执行目录 SQL。所有服务端写操作都发生在本机回环地址的一次性测试服务中。

## 2. 验证边界与环境

| 项目 | 本次环境 |
|---|---|
| 被测提交 | `1e3e324` |
| 操作系统 | Windows 10.0.19045 x64 |
| Python | 3.12.12 |
| uv | 0.13.4，官方发布件；GitHub artifact attestation 校验通过 |
| Pixi | 0.83.0 |
| GDAL | 3.13.3，仓库 `pixi.lock` 的 `win-64` 环境 |
| GeoServer | 2.24.4 官方二进制分发，回环地址、一次性目录 |
| Java | 17 |
| Qt | PySide6 Essentials 6.12.0，Windows 原生平台插件 |
| 数据 | 脚本生成的 4 个 SHP 和 1 个 RGB GeoTIFF |

本地验证材料和一次性工具保存在仓库之外的临时目录中。这些目录包含可再生成的合成数据、测试输出和测试运行时，不属于产品源代码，不应提交到仓库。

验证期间未将密码写入环境档案、命令行参数、报告或 Git。一次性 GeoServer 的认证信息只通过测试进程的会话环境变量提供。

## 3. 执行结果

### 3.1 依赖、静态检查、单元测试与构建

| 检查 | 结果 |
|---|---|
| `uv sync --frozen --extra desktop --extra dev --extra packaging` | 通过 |
| `uv lock --check --offline` | 通过 |
| `uv pip check` | 31 个已安装包兼容 |
| `uv run --frozen --no-sync ruff check .` | 通过，无告警 |
| `uv run --frozen --no-sync pytest -q` | `310 passed, 4 skipped` |
| `uv run --frozen --no-sync python -m build --no-isolation` | sdist 与 wheel 构建成功 |
| `uv run --frozen --no-sync geopub --version` | `GeoPublisher Workbench 0.1.0` |
| Windows PyInstaller 预览 | 构建成功；不包含 GDAL/PROJ，不能作为完整离线包 |

4 个跳过项均为符号链接安全测试。当前 Windows 会话不允许创建测试所需的符号链接，因此以下拒绝边界没有在本机实际运行：目录 SQL 输出符号链接、计划源文件/父目录符号链接和数据源符号链接。它们不能被记为 Windows 已通过。

仓库没有启用覆盖率工具或覆盖率阈值，因此本报告不声明行覆盖率或分支覆盖率。CLI 目前只有版本入口烟测，没有独立的自动化命令行用例。

### 3.2 受控 GDAL 与离线预检

`uv run --frozen --no-sync geopub doctor --environment <本地测试环境档案> --offline` 返回：

```text
status AVAILABLE
version 3.13.3
drivers ESRI Shapefile=true, GTiff=true, COG=true, VRT=true
network OFF
```

运行时包含 `gdalinfo`、`ogrinfo`、`ogr2ogr`、`gdalwarp`、`gdal_translate`、`gdaladdo` 和 `gdalbuildvrt`。本次实际产品路径只调用当前实现允许的检查命令；工具存在不代表产品已经实现重投影、修复、COG 转换或金字塔构建。

合成样本包含：

- UTF-8 点、线、面 SHP；
- GBK 面 SHP；
- EPSG:4326 RGB 渐变 GeoTIFF。

离线计划结果为 `ready: true`，共 5 个资源。4 个 SHP 均带有 `GEOMETRY_VALIDATION_NOT_RUN` 告警，表示当前版本没有完成全量几何有效性或拓扑检查；GeoTIFF 没有预检告警。

### 3.3 Windows 原生桌面烟测

桌面程序使用 Qt `windows` 平台插件成功启动，窗口标题为 `GeoPublisher · GeoServer 发布工作台`。系统检测到 368 个字体族，包含 Microsoft YaHei，中文界面原生渲染正常。

无显示器的 `offscreen` 平台没有加载系统字体，中文会显示为方框。该现象只说明 offscreen 截图不能作为 Windows 视觉验收证据，不是原生桌面已发现的字体故障。

### 3.4 一次性 GeoServer 真实集成

执行 `scripts/integration_check.py`，目标仅为本机回环 GeoServer。结果如下：

| 验证项 | 结果 |
|---|---|
| GeoServer / GDAL | 2.24.4 / 3.13.3 |
| 计划资源数 | 5 |
| WMS PNG | 5 / 5 成功 |
| 矢量 WFS 抽样 | 4 / 4 成功 |
| 首次写操作记录 | 20 |
| 同计划重跑新增操作记录 | 0 |
| 配置清理状态 | `ROLLED_BACK` |
| 清理移除对象 | 20 |
| 无关图层 | 清理前后不变 |
| 上传响应丢失 | `MANUAL_ACTION_REQUIRED` |
| 未知结果重试新增写操作 | 0 |
| 服务器实际数据文件 | 按设计保留 |

这证明当前样本范围内的预检、不可变计划、REST 发布、样式绑定、WMS/WFS 验收、幂等恢复和精确配置清理可以在 Windows 上协同工作。它不证明代理、TLS、现场权限、真实大文件、真实样式或业务目录接入已经通过。

## 4. 与产品范围的符合度

仓库的 [v0.2 开发基线](PRD-v0.2.md) 和 [实现合同](IMPLEMENTATION_CONTRACT.md) 已主动缩小早期产品设想。下表同时说明当前实现证据与尚未满足的完整目标，不能用开发基线的“完成”替代原始 PRD 验收。

| 能力 | 当前判断 | 说明 |
|---|---|---|
| SHP 预检与发布 | 部分通过 | 组件、编码、CRS、字段、范围、要素数及上传链路通过；没有全量无效几何、空几何、拓扑、自动修复和重投影 |
| 单 GeoTIFF 发布 | 部分通过 | 元数据检查、独立上传和 WMS 通过；没有容量估算、压缩优化、COG 转换、内部概览构建或大文件续传 |
| SLD | 默认 SLD 集成通过，外部 SLD 仅有代码级证据 | 真实集成使用程序生成的默认 SLD 并完成绑定；已有 SLD 的 XML、字段引用和外部资源检查由单元测试覆盖，但未做真实集成；不支持 QML/LYRX/CIM 转换，也未证明复杂样式视觉等价 |
| GeoServer REST | 核心链路通过 | 已有工作空间内的创建、绑定、读取、幂等恢复和精确删除通过；不支持原位替换和任意历史版本恢复 |
| WMS/WFS 验收 | 基础链路通过 | 实际 PNG 与矢量抽样通过；尚未覆盖比例尺区间、空白基准、栅格有效数据窗口和多缩放级别，本次真实 GeoTIFF/WMS 集成样本也未覆盖 NoData/Alpha 场景 |
| 任务审计与清理 | 部分通过 | SQLite 写意图、结果、所有权快照和锁有效；没有预定义完整 rollback plan 或失败自动补偿，另行授权的清理只移除服务配置并保留上传数据，不满足原 PRD 的完整补偿要求 |
| 目录 SQL | 生成器通过单元测试 | 只生成通用 PostgreSQL SQL，不连接数据库；仓库没有原 PRD 所需的现场服务表/图层表适配模板，`rollback.sql` 只用于只读核验且不含 DELETE，现场仍须人工核验 |
| Windows | 核心开发链路通过 | 本报告提供原生桌面和真实集成证据；完整离线安装包尚未通过 |
| Linux | 既有开发链路证据 | 见 [验证汇总](VALIDATION.md)；目标 Linux 发行物仍需重新验收 |
| Mosaic / Pyramid | 未实现 | 开发合同明确排除；不能将多个 TIFF 的独立发布等同于 Mosaic/Pyramid |
| 跨系统任务迁移 | 未满足 | 计划保存绝对本机路径，同一个任务工程不能直接在 Windows 与 Linux 间搬移执行 |

## 5. 发现的问题与发布阻断项

### P0：进入生产前必须处理

1. **GDAL 子进程继承 GeoServer 凭据环境变量。** 当前 GDAL 运行时环境从 `os.environ.copy()` 派生，实测子环境中仍包含 `GEOPUB_PASSWORD`。虽然密码没有写入日志或计划，但无须认证的 GIS 子进程不应获得该凭据。应建立子进程环境白名单，显式移除 `GEOPUB_USERNAME`、`GEOPUB_PASSWORD` 及未来的其他密钥变量，并补充回归测试。
2. **完整离线发行物未形成。** Windows PyInstaller 预览可以构建，但没有包含 GDAL、PROJ、驱动、数据目录、许可证和 NOTICE。必须在干净的目标 Windows/Linux 机器完成断网安装、启动、预检、发布和卸载/升级验证。
3. **多 TIFF 产品语义与早期 PRD 不一致。** 当前开发合同、实现、桌面提示和自动化测试均已固定为“多个独立图层”；早期目标则要求识别 Mosaic/Pyramid 候选并阻断或转专用流程。发布前必须由产品负责人明确接受这一范围变更；如不接受，应改为阻断或进入专用流程，避免用户把分幅数据误当成一个镶嵌图层。
4. **失败补偿与 rollback plan 不满足原 PRD。** 当前只有另行确认的显式配置清理，失败时不会依据预定义计划自动补偿，也不会恢复或删除服务器实际数据。若继续以原 PRD 为验收基线，必须补齐授权边界、补偿计划、执行回执和失败恢复测试。
5. **WMS 完整判空算法未满足原 PRD。** 当前真实 PNG 主链已通过，但尚未覆盖比例尺区间、空白基准、栅格有效数据窗口和多缩放级别，本次真实集成样本也未覆盖 NoData/Alpha；这属于原 PRD Gate D 的验收阻断。
6. **任务工程不能跨 Windows/Linux 迁移。** 计划保存绝对本机路径，不能把同一任务工程直接移到另一系统继续执行，不满足原 PRD 的跨平台任务要求。
7. **现场目录适配未交付。** 仓库有意不包含真实业务 schema，但原 PRD 验收仍需要经授权、脱敏的服务表/图层表映射模板、测试库执行证据和可核验的回退方案；通用 SQL 生成器不能替代该项。

### P1：现场试运行前建议处理

1. 本次 PowerShell 输出捕获使用的代码页与 Python UTF-8 输出不一致时，CLI 中文 JSON 出现乱码；设置 `PYTHONUTF8=1` 后正常，应用生成的 JSON/报告文件始终是正确 UTF-8。应在入口检测输出编码，或在安装与使用说明中明确配置。
2. 本机 4 个符号链接安全测试被跳过；应在允许创建符号链接的 Windows CI/测试机补跑。
3. 远程 GitHub Actions 因账户侧 runner 资源限制未实际执行；当前没有可替代本地结果的远程双平台 CI 证据。
4. 没有覆盖率阈值，也没有 CLI 命令级自动化测试。
5. 大文件上传同步且不可续传；取消需等待当前请求或超时，尚未验证 1 GB TIFF、磁盘不足和长时间传输。

## 6. 建议的下一轮验收

按以下顺序推进，避免重复执行已经证明的检查：

1. 修复 GDAL 子进程凭据继承，并只回归相关单元测试、完整 pytest 和一次集成检查。
2. 由产品负责人确认是否接受当前“多个独立图层”对原始 PRD 的偏离；若不接受，再实现 Mosaic/Pyramid 候选阻断并更新合同、界面文案和测试。
3. 确定原 PRD 是否仍为验收基线；若是，补齐失败补偿、完整 WMS 判空和跨系统任务迁移。
4. 生成包含受控 GDAL 的 Windows 与 Linux 离线包；在干净、断网的目标系统执行安装和五资源冒烟验证。
5. 在授权的测试环境验证 HTTPS/反向代理/权限、目标 GeoServer 版本和真实体量数据。
6. 由现场负责人提供脱敏后的目录字段映射，在测试库人工执行生成的 SQL，并分别核验唯一键、外键、触发器和页面可见性。

只有 P0 全部关闭，并取得目标 Windows/Linux 离线包与现场测试环境的证据后，才建议将版本从“开发预览”提升为“可试运行候选”。

## 7. PR #1 凭据隔离补丁的 Windows 增量验证

### 7.1 版本与提交隔离

2026-11-01，先将 README、验证汇总和本报告的原始版本独立提交为 `a1605db`，再创建 `validation/pr-1-windows-20261101`，合入 [PR #1](https://github.com/kllin8154-arch/GeoPublisher-Workbench/pull/1) 的 `d8bb6438c8aa27905480f67be47a6db72d89f638`。被测合并上下文为 `0318ad0`；其中 `src/`、`tests/` 和 `scripts/` 与该 PR 头提交一致，没有附加代码修改。

使用原 Windows 10.0.19045、Python 3.12.12、uv 0.13.4、锁定的 GDAL 3.13.3 和 PySide6 6.12.0 环境。没有启动 GeoServer，没有执行 REST 写入或目录 SQL。旧版第 3.4 节的发布集成结果仍只归属于旧提交，本次按补丁影响范围验证本地子进程、预检与桌面计划生成。

### 7.2 全量测试与原生用例执行证据

| 检查 | 结果 |
|---|---|
| 全量 pytest | **332 passed, 4 skipped**，9.46 秒，共收集 336 项 |
| 相对原版新增测试 | 22 项全部通过；原版 `310 passed, 4 skipped` 保留不变 |
| Ruff | 通过 |
| sdist / wheel | 构建成功 |
| `uv lock --check --offline` | 通过 |
| 真实子进程环境观察 | 默认、自定义、白名单冲突凭据均未透传；父环境未改变 |
| 原生 GDAL 用例 | **实际执行，通过，未跳过**；8 次原生调用，JUnit 记录 1.040 秒 |

原生用例为 `tests/test_datasets.py::test_selected_native_gdal_still_probes_and_resolves_crs_with_minimal_environment`。已解析 JUnit XML，确认该 testcase 存在且没有 `skipped`、`failure` 或 `error` 节点。它直接调用选定的 Windows `.exe`，验证版本、驱动、EPSG 解析与坐标变换；没有用 mock 返回值替代原生执行。

4 个跳过项仍全部是 Windows 符号链接权限所致：

- `tests/test_catalog.py:271`：1 项；
- `tests/test_config_plans.py:309`：2 项；
- `tests/test_datasets.py:447`：1 项。

### 7.3 驱动、CRS、中文路径和凭据边界

另行在含中文及空格的目录生成 5 份新合成数据，并通过 CLI 离线 doctor 和 plan 入口验证三种凭据引用：

| 配置 | 离线 doctor | 中文路径计划 | 父进程凭据可读 | GDAL 子环境 |
|---|---|---|---|---|
| 默认 `GEOPUB_USERNAME` / `GEOPUB_PASSWORD` | AVAILABLE | ready，5 个资源 | 是 | 无默认或自定义凭据名/值 |
| 自定义引用，配置使用混合大小写 | AVAILABLE | ready，5 个资源 | 是 | 无默认或自定义凭据名/值 |
| 凭据引用与 `LANG` / `LC_ALL` 重名 | AVAILABLE | ready，5 个资源 | 是 | 重名凭据排除优先 |

所有凭据值均为本机测试哨兵；结果只记录布尔值，不保存凭据值或完整环境转储。每次实际 `subprocess.run` 调用前检查子环境，同时仍执行真实 GDAL。父进程通过 `Environment.credentials()` 读取值，前后结果一致，完整父环境也保持不变。

该独立验证共观察到 **71 次原生调用**：合成样本生成 12 次、版本/驱动/CRS/变换 8 次、三组 doctor 各 6 次、三组五资源预检各 11 次。全部成功，均没有携带测试凭据、代理或云凭据哨兵。PATH 仅为选定运行时的 `Library/bin`；GDAL_DATA 和 PROJ_DATA 指向该运行时，`proj.db` 存在，`PROJ_NETWORK=OFF`。

ESRI Shapefile、GTiff、COG、VRT 四个驱动均可用；`gdalsrsinfo` 解析 EPSG:4326，`gdaltransform` 将 `(10, 20)` 转为 EPSG:3857 的约 `(1113194.90793, 2273030.92699)`，与预期一致。全部资源 CRS 为 EPSG:4326，源文件哈希前后相同。Python HTTP 请求拦截计数为 0；没有执行发布命令。

### 7.4 原生桌面生成计划烟测

使用 Qt `windows` 平台插件启动真实桌面，在含中文及空格的输入/输出路径中，通过 QTest 点击实际“检查并生成计划”按钮，等待真实后台 `build_plan()` 完成。

| 检查 | 结果 |
|---|---|
| 原生字体 | 368 个字体族，Microsoft YaHei 可用，截图中文字正常 |
| 后台计划生成 | 实际启动并完成，`ready: true` |
| 资源 | 5 个，全部 EPSG:4326 |
| 页面状态 | 进入检查预览，执行按钮可用 |
| 原生 GDAL | 11 次，全部退出码 0，默认和自定义凭据名/值均未透传 |
| 父进程凭据 | `Environment.credentials()` 读取正常 |
| 输入文件 | 哈希不变 |
| 网络与错误 | socket 连接尝试 0，无错误弹窗 |

没有点击执行发布。该烟测覆盖桌面按钮到后台预检再到检查预览的路径，不替代完整桌面交互、可访问性或发布验收。

### 7.5 证据与复验方法

本机验证材料位于仓库外 `GeoPublisher-Workbench-validation/pr1-windows-20261101`，含离线验证脚本、合成样本、计划、JUnit、汇总 JSON 和桌面截图。未将本机环境档案、GIS 数据或截图中的绝对任务路径写入 Git。

| 证据 | SHA256 |
|---|---|
| `pytest.xml` | `1d6c774745031b2dc3af0e0bc3fba49ffd0891fe6148c66b1f68304e655374bc` |
| `native-results.json` | `76d80b8b98f027b23c31f0e04f7e4e31e4b58fd3f7757a18d3dcc376698d0bad` |
| `desktop/desktop-plan-results.json` | `439b680019346e5450f0bcd8b153d2e235f3701e12deec18d07fd2cbd8eba759` |
| `desktop/desktop-plan-native.png` | `07680030a0fefb78b0f022894f5d06c4ddde5a1bc6bb07edd91cf3ca15b7325e` |

使用锁定运行时和项目虚拟环境复验；先准备依赖并执行 `pixi install --locked`，随后运行：

```powershell
$env:PYTHONUTF8 = '1'
$env:QT_QPA_PLATFORM = 'offscreen'
uv run --frozen --no-sync pytest -q -ra --junitxml=data/validation/pytest.xml
uv run --frozen --no-sync pytest -v tests/test_datasets.py::test_selected_native_gdal_still_probes_and_resolves_crs_with_minimal_environment
uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync python -m build --no-isolation
Remove-Item Env:QT_QPA_PLATFORM
```

若原生用例显示跳过，应先补齐 `.pixi/envs/default/Library/bin` 下受控 GDAL，再验收；不能计为通过。中文路径与桌面烟测使用新任务目录，保留源哈希、实际子进程观察和计划结果，并按第 7.3–7.4 节逐项核验。

### 7.6 门槛结论

**PR #1 的 Windows 增量验证通过，可以合入主分支；第 5 节 P0 第 1 项“GDAL 子进程凭据继承”在该补丁中关闭。** 同时覆盖默认、自定义、大小写及白名单冲突引用；父进程凭据读取没有被破坏。

完整离线包、多 TIFF 导入意图、WMS 内容验收、完整失败补偿、任务迁移和现场目录适配仍按 [后续验收门槛](RELEASE-GATES.md) 单独推进。远程 CI 资源限制未解除，本次通过依据是本机执行证据。
