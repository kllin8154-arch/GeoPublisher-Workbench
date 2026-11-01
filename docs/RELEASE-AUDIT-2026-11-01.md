# GeoPublisher 受控清理恢复与第三方许可证审计（2026-11-01）

## 范围及隔离

本次以 `main` 的 `f1bd81b` 为基线。只修改错误后的审计输出和只读许可证证据收集，不扩展任何服务器写入、删除、自动重试、生产数据库操作或现场数据访问。

本机离线生成的许可证报告保留在：
`D:\a系统测试\GeoPublisher-Workbench\validation\license-audit-20261101.json`。
报告来自现有、已验证的 Windows 便携预览，不上传生产数据、凭据或真实 GIS 文件。

## 1. 清理失败后的残留清单

`rollback_plan()` 仍先核对所有已记录的拥有权、服务端指纹以及未知写入，再开始删除；删除只影响本任务拥有的服务配置，原始上传文件继续保留。

这次为 `cleanup-report.json` 新增 `recovery_inventory`，来源为 **LOCAL_JOURNAL_ONLY**，包括：

- `objects[]`：resource_id、kind、name、state；状态可为 `CONFIRMED_ABSENT_AT_LAST_CHECK`、`DELETE_OUTCOME_UNKNOWN`、`NOT_CONFIRMED_REMOVED`；
- `unresolved_operations[]`：尚处于 `UNKNOWN/INTENT` 的任务操作，只有标识、动作和状态，没有凭据或 HTTP 请求正文；
- `requires_live_reconciliation`：是否存在尚未确认的对象或操作；
- 中文人工处理提示，说明**本地记录并不等于实时服务器状态**。

`CONFIRMED_ABSENT_AT_LAST_CHECK` 只说明最后一次客户端读取得到对象已不存在，不证明后来没有其他人恢复；任何未知结果均保留人工复核路径。报告本身不授权新的 DELETE，不可直接作为自动执行命令。

新增测试覆盖：成功清理全对象的记录、第二资源先删 Layer 后在 Resource DELETE 丢失响应、多资源残留与未知结果清单、外部样式变更导致全部删除前阻断、再次清理不触发任何新增远程写入。经本机验证，`tests/test_engine.py` **33 passed**。

## 2. 离线许可证证据审计

脚本：`scripts/audit_portable_licenses.py`。读取随包 `runtime/conda-meta/*.json` 的 **name、version、license、files**，以及明确指定的本地 Python `*.dist-info/METADATA` 及许可证文件。输出候选匹配、相对路径、SHA256，不导出许可证正文；不联网，不扫描用户 GIS 数据，不读取或输出凭据。

样例：

```powershell
Set-Location D:\Java-p\GeoPublisher-Workbench
.\.venv\Scripts\python.exe scripts\audit_portable_licenses.py --bundle D:\Java-p\GeoPublisher-Workbench-Portable\Current --site-packages D:\Java-p\GeoPublisher-Workbench\.venv\Lib\site-packages --output D:\a系统测试\GeoPublisher-Workbench\validation\license-audit-20261101-new.json
```

以上命令在 PowerShell 中按单行执行；输出路径必须不存在，脚本不会覆盖旧报告。

2026-11-01 本机审计结果：

| 核查项 | 结果 |
|---|---:|
| Conda 组件 | 67 |
| 通过元数据及文件检查定位到随包许可文本 | 9 |
| 未能按对应组件定位到许可文本/未索引 | 58 |
| 本地 Python wheel 源环境元数据 | 31 |
| 当前对外分发结论 | **NOT_CLEARED** |

“58 未能定位”不等于 58 个许可证违法或缺失：部分可能由合并依赖、上游发行包的共享版权文件、只在源包存在或未被 `files` 列表声明导致。应逐包查证，不能以 `license=MIT` 等元数据字段代替许可证正文。

官方资料核对：
- [GDAL License](https://gdal.org/en/stable/license.html)：GDAL 核心通常为 MIT，但二进制依赖的许可证可能更加严格。
- [PROJ About / License](https://proj.org/en/stable/about.html)：PROJ 使用 MIT。
- [Qt 6 licensing](https://doc.qt.io/qt-6/licensing.html) 和 [Qt LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)：PySide6/Qt 的 LGPL、GPL、商业许可及再分发义务需依据实际组件与分发模式核对。

特别关注当前 `geos` 组件声明 `LGPL-2.1-only`，以及 PySide6 Essentials、Qt DLL 的许可证文本、动态链接可替换性、用户权利及所需通知。审计结果不是法律意见，也不证明这些义务已经履行。

### 后续分发门槛

1. 将实际随包组件和必要许可证全文、NOTICE、归属/修改说明逐项核对齐全，生成经过人工确认的 SBOM / notice 列表；
2. 与代码签名、安全扫描、干净 Windows 断网安装/启动和隔离 GeoServer 五资源发布验收分开记录；
3. 仍以生产环境完全隔离、失败恢复与 WMS 多缩放级别/NoData 验收作为进入现场试用的独立门槛。

本轮新增能力可在受控开发环境使用；正式包仍不得因审计脚本成功就自动变为合规发行版。
