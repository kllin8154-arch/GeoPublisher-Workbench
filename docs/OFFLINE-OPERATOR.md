# GeoPublisher Workbench：离线主机 / 内网服务器部署

适用范围：最终部署在与互联网隔离的 Windows 10/11 或 Windows Server（带桌面环境）的主机；或者部署在有受控 Python/GDAL 运行环境的 Linux 内网服务器。构建机可联网准备依赖，**目标主机运行时不能依赖公网**。地质业务数据不得上传互联网或公共构建平台。

## Windows 离线桌面（目前可直接使用的交付方式）

1. 在联网开发机完成构建与测试，取完整的 `Current` 文件夹，通过经批准的离线介质拷贝到目标主机本地英文路径（例如 `D:\\GeoPublisher\\Current`）。不要只复制 exe，也不要从网络共享直接运行。
2. 目标机不必安装 Python、pip、uv、PySide6 或 GDAL；它们和 GDAL/PROJ 数据随便携包提供。不得在目标机执行在线安装命令。
3. 首次双击 `Install-Offline-Shortcut.cmd`，程序使用系统自带 PowerShell 和 Windows COM 将快捷方式创建在当前用户桌面；不需要管理员、外网或修改执行策略。也可以直接双击 `GeoPublisher-Workbench.exe`。如果是无桌面的 Windows Server Core，跳过快捷方式功能。
4. 在“环境设置”填写**已授权**内网 GeoServer 地址、已有 Workspace 和本次登录凭据。无公网网络不影响使用，但必须能访问需发布的 GeoServer；若 GeoServer 在本机，则用回环地址。程序启动不主动连接网络，只有检测连接及确认发布才访问指定环境。
5. 在“发布任务”第一步直接选择整个 GDB、MXD 或 SHP/TIFF。GDB 会在本机逐图层转换，合格成果自动加入列表；MXD 只能在具备 ArcMap 10.x/ArcPy、原始数据源可访问的 Windows 离线主机转换，不能将 MXD 当作包含完整几何数据的容器。复杂 LYR 样式转换需本机另有合法 QGIS/相关工具。
6. 点击“下一步：检查图幅”，核对每个图层的 CRS、字段、名称、样式和目标地址，再点击“确认并发布”。发布后人工检查真实 WMS，生成业务数据库目录父子节点草稿；数据库最终登记前必须核实现场表结构/父节点/权限并单独审核 SQL。

目标主机的任务数据和源文件保持在本机指定目录。不要将真实 GDB、MXD、SHP、转换产物、数据库密码写进公开仓库或共享给联网构建服务。

### Windows 目标机自检

在复制后的 `Current` 目录中核对：

- `GeoPublisher-Workbench.exe`、`_internal`、`runtime\\Library\\bin\\gdalinfo.exe`、`runtime\\Library\\share\\proj\\proj.db`、`build-manifest.json`、`Install-Offline-Shortcut.cmd` 都存在。
- 运行 `runtime\\Library\\bin\\gdalinfo.exe --version`，核实 GDAL 版本；坐标系校验需使用随包的 GDAL_DATA 与 PROJ_DATA。
- 在不连外网的情况下启动桌面，加入**数学合成** SHP 进行本地预检，再在**专用隔离 GeoServer** 上执行发布和 WMS 验收。主业务 GeoServer/数据库不作为自动化测试目标。
- 核对构建清单中 exe、GDAL、proj.db 及快捷方式脚本的哈希；在目标机扫描恶意软件、核查许可证/NOTICE，再决定是否正式交付。当前包是内部开发预览，尚未完成干净离线机器和正式分发验收。

## Linux 内网服务器

Windows 的 `Current` 包不能在 Linux 直接运行，亦不能用 Windows 的 ArcMap 10.x/ArcPy 进行 MXD 转换。无图形桌面的 Linux 服务器以 **CLI 发布引擎** 为主；如确实需要 GUI，需提供受支持的 Linux 图形会话并单独打包和验收。

Linux 离线交付须在**相同 CPU 架构与 Linux ABI** 的构建环境中准备冻结的 Python/GDAL/PROJ 依赖、校验哈希、提供经许可的离线安装包和启动脚本。目标机不运行 `pip install`、`uv sync` 等在线命令。本阶段**没有生成或验收 Linux 离线成品包**，不能将 Windows 便携版当作 Linux 交付物。

如果 GeoServer 在内网服务器而程序部署在隔离 Windows 管理主机上，使用明确授权的内网服务地址即可；业务数据库目录登记仍采用单独审核流程，不能因为地图发布成功就自动写生产数据库。
