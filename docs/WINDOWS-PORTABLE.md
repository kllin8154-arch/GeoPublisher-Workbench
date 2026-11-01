# Windows 便携预览包与桌面快捷方式

本页只记录内部开发预览；不是签名安装包，也未通过完整的纯离线干净机器验收。构建/发布时不要接触真实项目地质数据、生产 GeoServer 或业务数据库。

## 本机交付路径（2026-11-01）

- 仓库：`D:\Java-p\GeoPublisher-Workbench`
- 便携程序：`D:\Java-p\GeoPublisher-Workbench-Portable\Current\GeoPublisher-Workbench.exe`
- Windows 实际桌面快捷方式：`D:\Desktop\GeoPublisher Workbench.lnk`（程序通过 Windows 已重定向的桌面目录取得）
- 默认任务数据：`D:\a系统测试\GeoPublisher-Workbench`（与应用程序及运行时分开）
- 源码构建产物、预览 manifest、运行时和构建缓存均留在 D 盘，不依赖 C 盘里的开发工具目录。

**重要路径限制：** 已在本机实测，固定的 Windows GDAL/PROJ 3.13.3 运行时搬到含中文字符的安装目录时，`proj.db` 虽然存在，`gdalsrsinfo EPSG:4326` 仍返回 `Cannot find proj.db`；保持完全相同的 runtime 内容，移动到英文路径 `D:\Java-p\GeoPublisher-Workbench-Portable` 后立即通过。因此当前构建脚本强制使用 ASCII 安装路径，不要把 **runtime 安装目录** 移到中文路径。GIS **输入/任务数据目录** 可按现有中文/空格路径测试结果继续使用。此限制针对当前固定 Windows 原生运行时，尚未被认定是所有 GDAL 版本的通用问题。

## 一键启动

在 Windows 桌面双击 **GeoPublisher Workbench**。快捷方式直接指向便携版 `.exe`，不调用 PowerShell、虚拟环境、`pythonw.exe` 或网络下载。程序首次打开时，仅在应用自身的受控运行时完整（`gdalinfo`、`ogrinfo`、GDAL 数据及 `proj.db` 均存在）时，自动填写“GDAL 工具目录”，否则保留为空，由用户手动配置。此自动发现不搜索系统 PATH、QGIS 或 ArcGIS。

已经导入的环境档案仍优先使用档案指定路径；如果档案来自旧位置，需要重新选择 **当前便携包** 中的 `runtime\Library\bin` 并保存新版档案。程序不会保存明文 GeoServer 凭据。

## 重新构建

要求：Windows 10/11、Python 项目虚拟环境、固定的 Pixi GDAL/PROJ、PyInstaller、Pillow；所有开发工具和代码已经在 D 盘。

```powershell
Set-Location D:\Java-p\GeoPublisher-Workbench
.\.venv\Scripts\python.exe scripts\make_windows_icon.py
.\scripts\build_windows_portable.ps1
.\scripts\install_windows_shortcut.ps1
```

图标也可以通过 `.\.venv\Scripts\python.exe scripts\make_windows_icon.py` 生成。标准构建会拒绝覆盖已有 `Current`；升级前应先把已经验证的 `Current` 移动为独立备份目录，再执行新构建。若 PyInstaller 和所有 runtime 复制均已完成，只是最终验证失败且已经处理根因，可用 `build_windows_portable.ps1 -Resume` 继续已经存在、且经过明确核对的 staging 目录，不会重用未知来源的旧包。快捷方式安装脚本会备份先前的快捷方式。

**不要只复制 EXE。** `Current\_internal`、`Current\runtime\Library\bin`、`share`、`lib\gdalplugins`（如有）和 `runtime\conda-meta` 均属于该预览包。删除或迁移其中一个目录会损坏运行时。

## 当前实际验收

- Windows 新增代码后的 Ruff 通过，pytest **410 passed、5 skipped**；5 项均因本机会话符号链接权限限制跳过。
- Windows 原生 Qt 桌面 EXE 已在无发布操作条件下启动并获得非空窗口句柄；未使用假静态页面。
- 完全从便携包的 `runtime\Library\bin` 调用 GDAL 3.13.3；`gdalinfo --formats` 含 GTiff、COG、VRT；`ogrinfo --formats` 含 ESRI Shapefile；`gdalsrsinfo EPSG:4326` 返回 0。
- `build-manifest.json` 记录目标可执行文件、GDAL 工具和 `proj.db` 的 SHA256；`THIRD-PARTY-METADATA.csv` 收集受控 conda 环境的包名、版本和声明的许可证类型。
- 应用当前目标路径为纯英文 D 盘目录；数据目录仍保留原有中文 D 盘位置。没有调用任何生产 REST 发布接口，也没有执行数据库 SQL。

## 正式发布前继续关闭的门槛

1. 在没有 Python、Pixi、QGIS、ArcGIS 的干净 Windows 10/11 机器 **断网** 验证启动和 5 个合成资源离线预检，并用一次性 GeoServer 完成受控发布、重复执行与精确配置清理。
2. 逐包核对实际第三方许可证文本、NOTICE、版本来源和再分发义务；现有 CSV **不等同于许可证文本**。补软件签名、杀毒安全扫描和升级/卸载验证。不建议为了启动而关闭杀毒保护。
3. 根据发布门槛继续完成 WMS 不同缩放级别、NoData/Alpha 及错误 BBOX 验收、明确授权的失败补偿、任务跨系统迁移与现场业务目录适配。

本机便携包是 **内部开发预览**，不能标记为完整生产离线交付。
