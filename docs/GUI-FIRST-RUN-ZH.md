# GeoPublisher 首次连接与合成数据闭环

用户只需配置 GeoServer 网页后台连接，**不需要 Linux root / SSH / 数据库账号**。

## 每个输入框要填什么

| 控件 | 含义 | 填写 |
|---|---|---|
| 配置名称（自定义） | 本机区分不同服务器环境 | 例如 my-site-test，不用于网络登录 |
| GeoServer 后台地址 | 打开 GeoServer 网页管理端的 HTTP(S) 服务根地址 | 如 https://host/geoserver；远程环境不要用 127.0.0.1 |
| 工作空间（Workspace） | GeoServer 管理后台已有的 Workspace 名称 | 例如 geopub_auto_smoke_20261117；不是数据库名称 |
| 本机 GDAL 目录 | 工作台自带的 GDAL 3.13.3 二进制路径 | 便携版本默认自动填写，通常不用改 |
| GeoServer 登录用户名 | 具有发布权限的 GeoServer 网页后台账号 | 如 admin；不是 Linux root，也不是数据库用户 |
| GeoServer 登录密码 | 上述 GeoServer 网页账号的密码 | 仅本次会话输入，不保存入配置文件 |

非本机 HTTP 未经过 TLS 加密，存在管理账号和图件泄露风险。优先配置 HTTPS。如仅在已获授权的隔离内网使用 HTTP，需要主动进入高级设置勾选“允许已授权的隔离内网 HTTP”，并在发布前再次阅读确认窗口。不要把密码发到聊天、上传 GitHub 或写入明文配置文件。

## 高级设置（平时只可能需要内网 HTTP）

- 受控 GDAL 版本：保持工作台预装版本，避免运行时错配。
- HTTPS CA 证书：仅私有 CA / 自签名证书才选择 CA PEM；系统可信证书保持空。
- 隔离内网 HTTP 例外：只有已确认服务归属、网络隔离和许可后人工勾选。
- 网络请求超时：默认 120 秒，大数据在连通性正常时可加大。
- 自动化用户名变量、自动化密码变量：保留默认，日常登录使用上面的直接输入框即可。

## 独立测试工作空间怎么建立

将 Workspace 填为 geopub_auto_smoke_20261117 这类全新的测试名称。
点击“仅用于新测试：创建独立 GeoServer 测试工作空间”，程序会校验专用测试命名、确认服务器地址，弹出二次确认并检查是否重名。若工作空间已存在，不要重复创建，直接只读连接测试。

成功后界面出现“连接成功，前往发布任务”。点击进入发布页；不必手工 Xshell/REST 创建工作空间。该按钮不会创建生产业务目录或写 PostgreSQL。

## 测试数据和发布流程

源码仓库提供完全离线、可重复生成的脚本：

    scripts/create_synthetic_gis_test_data.py

在装有受控 GDAL 的 Windows 环境使用：

    python scripts/create_synthetic_gis_test_data.py --output "D:\Java-p\geopublisher-synthetic-test" --gdal-bin "D:\Java-p\GeoPublisher-Workbench-Portable\Current\runtime\Library\bin"

生成：
- shp：3 个 SHP，8 个点、4 条线、4 个面；16 个虚构要素，EPSG:4326。
- gdb：1 个包含 3 个要素类的 OpenFileGDB。
- raster：1 份合成 GeoTIFF。
- styles：3 份 SLD，点/线/面符号。
- START-HERE.txt 与 MANIFEST.json：使用说明和每个文件的 SHA256。

第一次先选择 shp 中的 3 个 .shp，点击“下一步：检查图幅”，确认 3 图层坐标/名称/目标工作空间，点击“确认并自动发布”。工作台负责 ZIP 副本、GeoServer REST 上传、注册图层、绑定样式和请求 WMS/WFS；用户不需要手工压缩、Xftp 或 Xshell。成功后在 GeoServer 验证三个图层，并从另一个浏览器会话读取 WMS/WFS；关闭工作台后数据应由服务器提供。

第二轮再测试整个 GDB 转换和 TIFF 上传；必须使用新 Workspace 或明确创建新版本，不能盲目在首次测试空间重复发布同名图层。

GeoServer 数据目录仍需运维确认位于持久磁盘并纳入备份；不在生产服务器上为测试而重启服务。数据库父子节点、真正的 MXD 符号保真依旧是独立验收项。
