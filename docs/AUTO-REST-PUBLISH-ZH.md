# GeoPublisher 自动上传 GeoServer：不使用 Xftp/Xshell 的常规流程

## 目的

正常发布只需 **选择整个 GDB/MXD（或 SHP/GeoTIFF）→ 本地预检 → 确认并自动发布**。工作台内部已有的 GeoServer REST file.shp 接口用于上传压缩 SHP 副本。文件由 GeoServer 服务器自己的数据目录管理，不引用本机 D: 或 E: 磁盘和 Xshell 会话。客户端停止运行后仍可由 GeoServer 提供 WMS/WFS，前提是 GeoServer 数据目录持久化。

此流程不需要用户将图层压缩为 tar、通过 Xftp 上传，或在 Linux 手动执行发布脚本。那些操作仅作为必须使用指定 /zbcx 路径的高级备用模式。

## 新的默认三步

1. **环境设置（首次 / 换服务器时）**：填写 GeoServer REST 服务根地址、已存在的 Workspace、本次 GeoServer 管理账号和密码；点击「测试连接（只读）」。密码不会保存到环境 JSON、日志或任务报告。不需要 Linux root 账号或 SSH 端口转发。
2. **选择数据**：使用「选择整个 GDB」「选择 MXD 地图」「添加 SHP/TIFF」。GDB/MXD 转换需要本机受控 GDAL / ArcMap Runtime 和可访问的原始关联数据，不是把 MXD 本身作为 SHP。成功导出的 SHP 自动加入待发布列表。
3. **下一步：检查图幅 → 确认并自动发布**：工作台为每个 SHP 生成包含 .shp/.shx/.dbf/.prj/.cpg 等组件的 ZIP 副本，按完整计划哈希复核后，通过 GeoServer REST PUT .../file.shp?configure=none 上传到服务端的数据目录，再创建指定 nativeName 的图层、上传/绑定 SLD、请求实际 WMS/WFS。过程记录操作意图、任务状态、缩放图片与报告。

结果页增加 data_transfer=AUTOMATIC_GEOSERVER_REST_UPLOAD、server_storage=GEOSERVER_MANAGED_DATA_DIR、单图层状态、WMS 预览及恢复记录。业务 PostgreSQL 图幅父子节点不会因此自动登记，须经独立授权和真实图幅核对。

## 安全与授权

- 建议为内网 GeoServer 配置 HTTPS + 正确可信的服务证书。REST Basic Auth 会随请求传输用户名/密码，使用 HTTP 时没有 TLS 加密。
- 已授权的纯内网 HTTP 仅在环境高级设置中主动勾选“允许此已授权内网环境使用 HTTP”后允许；确认执行时仍提示凭据/图件在 HTTP 内网中传输。不要在公网 HTTP 上使用管理员账号。
- 接口必须明确且唯一：通过已经审核的服务器与 Workspace 发布，禁止自动推断服务器或绕过证书校验。
- 只创建全新 Store/Layer/Style，不覆盖已有图层、目录或服务器文件；全部资源的同名检查在首个上传前完成（fail/new-version 的多图层新批次）。
- 检查不是数据库级远端锁；如果其他人同时发布或编辑 GeoServer，应暂停并人工核对。超时或结果未知时绝不盲目重试。
- 地质数据仅发送到用户明确许可的内网 GeoServer，不上传 GitHub、公开云端、聊天或模型工具。
- **服务器数据目录持久性需要运维确认**：在 208 上检查 GEOSERVER_DATA_DIR 实际路径及磁盘挂载/备份。不能因为 REST 返回成功就断言重启后必然存在。
- 默认 SHA256 保护的是本机源、副本及任务操作记录；实际 REST 上传后还需要 WMS/WFS 和 GeoServer Store 元数据核实，未实现远端按文件级哈希回读。

## 与历史发布任务的关系

若过去已经通过 external.shp 将 SHP 发布到独立测试 Workspace，其现有 Store 不得使用新的 REST 上传方式覆盖或重播。

自动上传模式用于下一批全新任务，指向经过确认的 Workspace。现有历史验收包不迁移、不删除。应在受控测试环境执行一次真正的 REST 自动上传授权演练，确认服务的数据目录路径、持久磁盘和重启后可用性后，才能对外宣称完整生产验收。

## 高级备用

如甲方要求图层物理数据必须存放特定目录，例如 /zbcx/geopublisher/releases，可继续通过「更多工具」选择 SSH/SFTP/服务器本地部署方式；这不是一般用户必须走的步骤。

官方技术依据：https://docs.geoserver.org/latest/en/user/rest/api/datastores/
