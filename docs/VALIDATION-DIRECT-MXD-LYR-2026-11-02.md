

# Windows 本地直接 MXD / LYR 转换验收（2026-11-02）

## 目标与安全边界

将原先“导出 ArcMap/QGIS 辅助脚本，再手动执行”的流程改为用户可在桌面转换窗口直接发起：选择 MXD 或包含同名 SHP/LYR 的目录 → 核对本机 Python 和输出根目录 → 后台转换 → 检查报告 → 人工确认是否加入发布清单或启用 SLD。

仅在用户本机执行。禁止把真实地质 MXD、GDB、SHP、LYR、配置、凭据或日志提交 GitHub/发送至服务器。本次测试使用先前由 ArcMap 10.8 创建的纯数学合成点、线、面数据；没有连接生产 GeoServer 或业务数据库。

## 已实现

- 新增 offline_conversion.py：只允许明确的本机 ArcGIS 10.x Python / QGIS Python 运行时，使用参数数组及 subprocess.Popen(shell=False)，不通过 cmd、bat、PowerShell 解释执行。
- 子进程环境从最小 Windows 系统变量构造，不继承 GeoServer 发布凭据、代理、云端凭据、任意 PATH。QGIS 使用显式 QGIS_PREFIX_PATH、PYTHONHOME、PYTHONPATH、Qt、GDAL/PROJ 路径，PROJ_NETWORK=OFF。
- 新建 offline-mxd-* 或 offline-lyr-* 目录，脚本、输出和 conversion.log 全部留在本地任务目录；所有来源只读，禁止符号链接、UNC 输入与 QGIS 输入输出目录重叠。
- 状态栏更新本地转换进度；增设“请求安全取消本地转换”和“打开本地转换报告”按钮。日志以有界方式读取，不把第三方完整输出直接送入 UI。
- 用户发起取消时，仅创建本地 cancel.request 标记；ArcPy 与 PyQGIS 在下一图层检查点识别标记并不再开始该图层。不会强行杀掉处理中的进程；失败与取消均不得显示为成功。
- MXD 完成后可选择将导出的 SHP 加入发布任务；LYR 完成后可选择将来源 SHP 加入任务，并将本次新 SLD 输出目录自动填入高级选项。以上仅是本机选择，仍需单独执行发布预检与发布确认。

## 本机真实执行证据

- 受控 ArcMap Python 2.7（ArcGIS 10.8，ArcInfo 授权），直接后台运行合成 MXD：3 个图层全部 EXPORTED，状态 SUCCEEDED，用时 10.58 秒。
- 受控 QGIS 4.2.1 Python 3.12.13 + SLYR Community / QGIS Style Assistant 0.4.0，后台运行同名 SHP/LYR：3 个图层全部 EXPORTED，状态 SUCCEEDED，用时 3.20 秒。
- ArcMap/QGIS 各自使用预置取消标记实际运行，均得到 3 个 CANCELLED，返回码 1，且没有产生可作为新发布图层的 SHP / SLD。
- 首轮新增/相关测试：83 passed；后续完整 pytest 和 CI 以最终提交与 CI 检查为准。
- 本机真实输入、输出和测试日志：D:\Java-p\GeoPublisher-Workbench-validation\arcgis-real-20261102，严禁推送仓库。

## 仍未验收

- 复杂地质 MXD/LYR、中文注记、混合字体、比例尺规则、复杂符号的保真与 GeoServer 实际 WMS 图片像素级一致性。
- 1 GB TIFF、真实 GIS 文件的长期压力与性能指标、完整断网干净机软件安装/许可/签名验收。
- Windows 便携预览当前不包含或再分发 ArcMap、QGIS、SLYR 等第三方许可运行时，运行这些直接转换功能仍依赖用户本机已授权的软件。


## 最终 Windows 回归与真实合成链路复测

- 改动完成后的 Windows 原生 Ruff：通过。
- 完整 pytest 连续两次：第一次 **546 passed、5 skipped，20.91 秒**；第二次 **546 passed、5 skipped，19.93 秒**，退出码均为 0。5 项跳过为 Windows 本机符号链接权限限制。
- Python sdist 和 wheel 通过本机锁定依赖构建。
- 最新受控执行器再次调用本机 ArcMap 10.8 与 QGIS 4.2.1：MXD 3/3 成功，用时 7.26 秒；LYR 3/3 成功，用时 1.69 秒。报告和输出仅在本地 D 盘。
- 本轮 GitHub PR 与合并提交 CI 结果需在 PR 提交后另行核实。该预览仍不是完成许可证和干净断网验收的正式发行包。
