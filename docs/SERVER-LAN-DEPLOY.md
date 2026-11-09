# 局域网 Linux 服务器发布链路｜GeoPublisher Workbench

适用：Windows/macOS/Linux 电脑与 Linux GeoServer 服务器处于经授权的同一局域网；或工作台直接运行在 Linux 服务器上。

## 设计：分段发布，避免“文件上传成功=图层已经入库”

    MXD/GDB/SHP/TIFF
       ↓ 本机受控 ArcMap/GDAL/QGIS 转换、SHP 字段核验/SLD
    受控 SHP/单 GeoTIFF immutable plan.json   或   服务器可用的 TIFF/ImageMosaic .tar
       ↓
    [选择目标服务器、验证 SSH host key / Linux 本机权限]
       ↓
    [文件传输] SFTP tar → 同名冲突检查 → SHA256 → 新目录解包 → TIFF SHA256
       ↓
    [GeoServer] 经 SSH loopback 隧道 / 本机 localhost 调用官方 REST
       ├─ SHP / 单 GeoTIFF：原计划上传引擎，含 SLD 与 SQLite journal
       └─ 服务端 GeoTIFF / ImageMosaic：REST external.geotiff / external.imagemosaic
       ↓
    [服务验收] store/layer 实际存在 → WMS PNG / mosaic granules / 必要时 WFS
       ↓
    [业务目录] 根据 206 实际只读父节点及字段，专题 → 比例尺 → 图幅
       ↓
    [生成 SQL] 业务服务及节点事务 SQL → 现场授权/备份后人工执行 → 查询复核

数据库操作**暂不在 SSH 交付模块自动执行**。原有 catalog-draft / catalog-tree 负责对业务目录生成经审查的 SQL；缺失真实业务表结构、父节点或 WMS 人工审查记录时保持 BLOCKED。

## 支持环境

| 所在机器 | 文件交付 | GeoServer REST | 备注 |
|---|---|---|---|
| Windows 10/11 | Paramiko SSH/SFTP → Linux | SSH 加密转发至 Linux 的 127.0.0.1:8080 | 开发机以 D 盘为默认工作目录 |
| macOS | Paramiko SSH/SFTP → Linux | 同上 | Python 核心、CLI、Qt UI 可运行；macOS 原生安装包另行构建验证 |
| Linux 客户端 | Paramiko SSH/SFTP → Linux | 同上 | 需 Python 3.11+ |
| Linux GeoServer 本机 | 直接本机复制到独立目录 | 127.0.0.1:8080/geoserver | Linux 用户需具备目标目录写权限；无需开通 SSH |

正式现场的具体 GeoServer/JDK/插件版本必须在目标服务器上只读核实；本模块的通用发布清单要求精确 GeoServer 版本，拒绝未核实版本，不能用本机测试实例替代。

## SSH 与凭据规则

- 服务器地址必须是明确的 RFC1918 私网 IP 或本机 loopback；拒绝公网目标、DNS 域名以及 UNC/网络共享文件源。
- SSH 使用已独立核验并保存在指定 **known_hosts** 的服务端主机公钥；未知主机密钥直接拒绝，不自动信任。
- 用户可选 SSH 密码、私钥或 SSH Agent。SSH 登录密码、私钥口令、GeoServer REST 密码都只在本次操作内存中保留，不写入环境配置和 SQLite journal。
- GeoServer REST 的管理请求走 SSH loopback 转发，不要求向 LAN 开放 REST 管理端口；默认 SSH 目标转发到远端 127.0.0.1:8080。
- Linux 根目录仅允许已授权的数据路径（例如 /srv/geopublisher/releases），不允许系统根目录、符号链接或路径穿越。
- 任何重名 tar、store、layer 均停止而不覆盖；不能以删除旧版代替确认所有权。
- 上传/写入超时一律视为 **UNKNOWN**。先比对哈希、目标 store/layer 和审计记录，再由人工决定是否恢复；没有自动盲重试或生产 DELETE。

## 在 Windows/macOS/Linux 电脑操作

准备一个只包含连接参数的本机 JSON（没有密码，值请按现场填写）：

    {
      "mode": "ssh",
      "host": "192.168.1.20",
      "port": 22,
      "username": "gisdeploy",
      "auth": "key",
      "known_hosts": "/absolute/local/path/known_hosts",
      "key_file": "/absolute/local/path/id_ed25519",
      "root": "/srv/geopublisher/releases",
      "geoserver_port": 8080,
      "postgres_port": 5432,
      "geoserver_context": "/geoserver"
    }

Windows 上 JSON 中的反斜杠必须写为双反斜杠，或使用正斜杠路径。请使用服务器控制台/运维渠道确认 SHA256 host key 指纹，**单独执行 ssh-keyscan 并不能完成身份核验**。如果首次创建 known_hosts，需要由运维确认指纹后再保存。

桌面进入 **发布任务 → Linux 服务器部署**。页面选择连接方式、凭据、Linux 发布目录、需要操作的发布包及当前阶段。选择“只读预检”不会传输数据；选择上传、解包、注册分别经过确认。

同一功能的 CLI：

    geopub server-doctor --profile <local-ssh-profile.json>
    geopub server-stage --profile <local-ssh-profile.json> --archive <verified-release.tar>
    geopub server-extract --profile <local-ssh-profile.json> --archive <verified-release.tar>
    geopub server-geoserver-doctor --profile <local-ssh-profile.json> --archive <verified-release.tar> --manifest <reviewed-raster.json> --geoserver-user <REST-user>
    geopub server-publish-raster --profile <local-ssh-profile.json> --archive <verified-release.tar> --manifest <reviewed-raster.json> --geoserver-user <REST-user> --output <new-local-audit-dir>

发布 SHP、SLD 或单幅 GeoTIFF 的原有任务：

    geopub server-publish-plan --profile <local-ssh-profile.json> --plan <immutable-task/plan.json> --geoserver-user <REST-user>

注意：该模式必须先使用**实际 SSH 服务器地址**创建不可变 plan.json，程序要求 plan.environment.base_url 精确为 http://<目标内网 IP>:<GeoServer 服务器端口>/geoserver；Linux 本机模式则需 http://127.0.0.1:<端口>/geoserver。拒绝把某环境的 plan.json 默默转发到另一台服务器。

## Linux 服务器本机运行

同样的 Python CLI 可以使用：

    {
      "mode": "local",
      "root": "/srv/geopublisher/releases",
      "geoserver_port": 8080,
      "geoserver_context": "/geoserver"
    }

CLI 的 --profile 指向本机 JSON；tar 文件路径也指向 Linux 上的本地文件。SHP/GDB 等业务数据使用本地 GDAL，MXD 只能依赖 Windows ArcMap 的真实 ArcPy 或先在客户端转换为 SHP。无需在服务器装 ArcMap，也不能把 MXD 当作 GeoServer 可识别的数据存储类型。

## GeoServer raster 清单（仅供说明，非生产配置）

    {
      "schema_version": 1,
      "release": "synthetic_release",
      "workspace": "demo",
      "geoserver_version": "2.24.4",
      "layers": [
        {
          "store": "synthetic_overview",
          "layer": "synthetic_overview",
          "kind": "geotiff",
          "source": "data/overview/preview.tif",
          "bbox": [-180, -90, 180, 90],
          "granules": null
        },
        {
          "store": "synthetic_mosaic",
          "layer": "synthetic_mosaic",
          "kind": "imagemosaic",
          "source": "data/mosaic",
          "bbox": [-180, -90, 180, 90],
          "granules": 8
        }
      ]
    }

只接受已通过 SHA256 文件校验的 tar / TIFF。ImageMosaic 必须在 GeoServer 服务进程可读、可索引的本机目录，且其插件和 GeoServer 版本相互匹配。**一个 GeoTIFF 不能冒充 ImageMosaic；八张 TIFF 也不应当被误认为 ImagePyramid。**

即使 WMS 自动判为 PASSED，也只是图片尺寸/有效像素初筛。用户仍需审查地理位置、接缝、裁剪、比例尺、透明背景、注记/SLD 样式，然后才能生成业务目录登记 SQL。

## 失败恢复

| 阶段 | 中断或冲突处理 |
|---|---|
| SSH 主机校验 | 立即停止；不得自动接受未知 host key |
| SFTP 2GB+ 上传 | 保留 .partial 状态；可以取消但不可自动重传/覆盖 |
| SHA256 校验 | 源/目标不一致即阻断；不解包 |
| 解包 | 只写全新目录；异常保留目录供人工核查 |
| REST 写入 | 先持久化 REST_CREATE_INTENT；遇到 UNKNOWN 停止，不盲重试/清理 |
| WMS/Granules | 保留真实 PNG 和只读核验结果，未通过不能声明成功 |
| SQL 入库 | 必须核对 206 真实字段、父节点和备份；当前生成器只生成待审核 SQL，不自动执行 |

## 开发/验证状态

- Windows 上 SSH/SFTP 参数、主机指纹拒绝、公网目标拒绝、重名阻断、路径安全、SHA256、审计日志、既有发布任务复用和桌面 worker 均有合成测试。
- 本机独立 GeoServer 2.24.4 + JDK17 合成 GeoTIFF 与 8 分幅 ImageMosaic 的 REST/WMS 真实验证通过。
- 仍需在实际目标 Linux 上只读预检 SSH、Java 运行进程/权限、ImageMosaic 配套 JAR、GeoServer REST 版本、父节点与数据库结构。正式 208/206 均未在此开发过程中修改。
- 生产 SQL 自动执行、跨平台原生安装器、ImagePyramid 和断点续传属于后续独立安全验收阶段；不得宣称已经实现。


## PostgreSQL 16 只读现场核验与自动化回归

GeoServer 发布后，业务目录登记须先单独运行“7. 业务数据库表与父节点只读核验”。该阶段使用 206 独立 SSH 会话，PostgreSQL 端口经 SSH 转发到远端 localhost。数据库会话使用 BEGIN READ ONLY、限时只读查询、ROLLBACK，仅比较业务目录 JSON 指明的业务表、必要字段、父节点和服务绑定，不自动修改 schema_verified。

CLI 命令：

    geopub server-catalog-doctor --profile <206-ssh.json> --catalog-json <business_tree_draft.json> --database <DB> --database-user <SELECT-role> --output <new-local-folder>

密码通过交互式提示输入，不写入 JSON、审计报告或命令行参数。推荐专用 SELECT 最小权限账号。

为避免模拟数据测试掩盖兼容问题，CI 新增独立的 postgres16-readonly-integration Ubuntu 作业，在一次性 postgres:16-alpine 服务上创建 demo_catalog/demo_nodes/demo_services 合成数据，验证正常核验、父节点错误阻断、真实事务只读状态，以及 SELECT 账号无法 INSERT。专用测试用例必须显式设置 GEOPUB_SYNTHETIC_PG16_TEST=1，否则跳过；CI 数据与现网 206 完全无关。

任何无阻断的只读报告都只是证据，不构成可执行生产 SQL 的授权。真实 206 的字段、现有父节点和账号仍需现场核验，备份与数据库登记操作须单独批准。
