# SHP 持久化到 Linux 服务器后发布（GeoPublisher）

## 必须澄清的两种发布方式

**方式 A：GeoServer REST 上传（适合小/中批次）**

- 工作台已有功能：本机先将 SHP 的 SHP/SHX/DBF/PRJ/CPG 制作为 ZIP。
- 点击“确认并发布”后，工作台用 GeoServer REST \`PUT /rest/workspaces/{ws}/datastores/{store}/file.shp?configure=none\` 将**文件内容**发送到 GeoServer；GeoServer 在**服务器**创建并持久化数据存储，再创建图层、上传 SLD、绑定样式并核验 WMS。
- 这**不是引用 Windows D:/E:/ 绝对路径**。上传成功且服务器持久存储健康时，关闭 Windows 程序不会导致图层数据消失。GeoServer 的实际数据目录必须放在持久磁盘上；如果服务部署在容器内，不能仅放容器临时层。
- 官方说明：https://docs.geoserver.org/stable/en/user/rest/api/datastores/ （file.shp 与 external.shp）。

**方式 B：SFTP/Xftp 8 先上传到 Linux 数据区（大批次、独立备份管理建议）**

- 本机：GDB/MXD → 可信 SHP/SLD → 不可变 \`plan.json\` → “准备服务器 SHP 数据包”。
- 本机只生成 \`gpv_<任务标识>.tar\` 与 \`vector-release.json\`，不上传。
- 通过 Xftp 8、工作台 Paramiko SFTP 或内部 SSH 通道将 tar 传至**事先授权、持久存储**的 Linux 发布目录（例如 \`/srv/geopublisher/releases\`）。
- 服务器上的 tar 先与本地 SHA256 匹配，再解到一个全新目录，执行所有 SHP/SHX/DBF/PRJ/CPG 的 SHA256SUMS 校验。
- 通过已确认 SSH host key 的内网隧道调用 REST **external.shp**，注册 \`file:///srv/geopublisher/releases/<release>/data/r0001/<native>.shp\`，这里只传**服务器本地路径**，不再次上传数据字节。
- 之后仍复用原工作台已实现的逐层日志与冲突检查 → featureType → SLD → WMS/WFS → 业务节点草稿/人工验收。遇到未知结果不盲目重试，决不自动删除原始数据。
- **两种方式只能选择其一**，同一已执行计划/日志不能悄悄从 REST 上传切到服务器预置文件，或者反过来。

## 关键前提（先在 Xshell 8 只读核实）

必须向运维确认以下信息，不可根据文件名猜测：

1. 实际提供 GeoServer 服务的 Linux 主机（不是只看能 SSH 登录）；\`/srv\`、\`/zbcx\`、\`/data\` 等位置是否是**持久磁盘**、容量是否足够、目录的属主和写入权限。
2. GeoServer 是 Linux 裸进程还是 Docker/Podman 容器。如果是容器，**external.shp 中的绝对路径必须是 GeoServer 进程/容器内部可见的路径**。不可把宿主机路径直接当成容器内路径。当前自动化要求宿主机和 GeoServer 可见路径一致；否则停止，先由运维设置显式卷映射。
3. GeoServer 服务用户对各 SHP/SHX/DBF/PRJ/CPG 文件、父目录需要有读取/遍历权限；不能通过关闭 SELinux 或随意 chmod 777 解决。
4. 目标服务器的 SSH 主机公钥指纹，需要与运维/控制台的独立可信值对比后登记 known_hosts。**仅运行 ssh-keyscan 不构成已核实身份。**
5. 必须先在 GeoServer 建好独立测试 Workspace 并确认 REST 认证用户；后续业务数据库登记仍属独立审批步骤。

只读排查命令（使用自己的 Xshell 已授权会话；不输出密码）：

\`\`\`sh
uname -s
id
findmnt -T /zbcx
df -h /zbcx
ps -ef | grep -Ei '[g]eoserver|[t]omcat'
command -v podman && podman ps --format '{{.Names}} {{.Mounts}}'
command -v docker && docker ps --format '{{.Names}} {{.Mounts}}'
\`\`\`

## 推荐的服务器目录

示例结构（路径由现场部署运维确认后调整）：

\`\`\`text
/srv/geopublisher/releases/           <- 持久化且授权的 SFTP 数据根目录
  gpv_<task-id>.tar                   <- 完整 SHA256 审核归档
  gpv_<task-id>/                     <- 解包后绝不覆盖
    README.md
    data/
      SHA256SUMS.txt
      r0001/legend.shp .shx .dbf .prj .cpg
      r0002/fault.shp .shx .dbf .prj .cpg
\`\`\`

每个任务使用独立不可变目录。资源由服务器持久磁盘托管，不删除源 GDB，不依赖本机长期开机。Xftp 可以上传 tar，但不能只上传 .shp 单个文件，必须连同 SHX/DBF/PRJ/CPG 和核对清单。

## 命令行操作（隔离网络）

已有 CLI 可在受控的本机 Python 环境或可执行 CLI 中运行：

\`\`\`sh
# 1. 本机：生成 tar + SHA 映射（源 plan.json 必须预检通过）
geopub server-vector-bundle --plan /local/task/plan.json --output /local/new/server-release

# 2. 本机：配置 profile.json（只保存连接参数，无密码）。
# 使用 user 已核实的 known_hosts、SSH IP、用户名及 Linux 根目录。
geopub server-doctor --profile /local/profile.json

# 3a. 推荐：SFTP 上传并校验远端 tar；不创建 GeoServer 图层
geopub server-stage --profile /local/profile.json --archive /local/new/server-release/gpv_<task>.tar

# 3b. 若使用 Xftp 手工上传，必须上传同一个完整 tar，
# 然后确认远端目录里的 tar SHA256 和本地 release 一致，
# 不要在 Xftp 中自动覆盖同名归档。

# 4. 服务器解包并核对每个组件 SHA256；不会执行任意脚本
geopub server-extract --profile /local/profile.json --archive /local/new/server-release/gpv_<task>.tar

# 5. 明确批准后执行新建数据存储、featureType、SLD、WMS 验证：
geopub server-publish-staged-vector \
  --profile /local/profile.json \
  --plan /local/task/plan.json \
  --archive /local/new/server-release/gpv_<task>.tar \
  --manifest /local/new/server-release/vector-release.json \
  --geoserver-user <已有REST账号>
\`\`\`

第 5 步会再次核实服务器 tar SHA256、所有文件 SHA256 与路径，再交互要求输入 \`PUBLISH_STAGED_SHP\` 及一次性的 REST 密码。密码不得出现在 profile、CLI 参数、聊天、报告或日志中。默认拒绝无交互无人值守写操作。

桌面操作同样可在“发布任务 → 更多工具”中进行：**准备服务器 SHP 数据包 → Linux 服务器部署（SSH 只读预检）→ 上传 → 解包 → 已上传 SHP：从 Linux 文件路径发布**。

## 数据入库不等于 GeoServer 发布

GeoServer 新图层创建成功只是地图服务阶段成功。业务系统要展示父节点/真实地质图幅/多专题图层，需要使用已有目录草稿及 PostgreSQL 只读核验，按现有专题/比例尺/图幅分类，检查 WMS/样式后单独授权登记。不应把 1 SHP 当成 1 张实际地质图幅。

## 验收必须证明断联后仍可用

- 每个目标 GeoServer store 的 \`url\` 应为服务端文件 URI，不能引用 Windows 磁盘路径。
- GeoServer 所在主机或容器确实能读取文件；更换本机网络环境/关闭工作台后，在另一台授权客户端获取 WMS 图片仍正常。
- GeoServer 服务重启后服务、符号化配置和持久化数据目录仍存在，并对原有图层成功返回 WMS。
- 形成服务 URL、图层数量、服务端存储路径及远端 SHA256、审计日志、用户人工 WMS 样式验收记录；缺失任何项均不能称为正式上线验收。

**现场数据安全：** 无论 SFTP 还是 REST，只允许明确授权的内网 GeoServer 及授权路径。严禁上传公网、GitHub、云盘或联网 AI。源码、测试和公共 CI 只使用合成数据。
