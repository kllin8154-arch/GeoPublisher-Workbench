# 业务目录数据库只读核验（206 PostgreSQL）

目标：将 GeoServer 发布后的“业务目录 SQL 生成”前置一步可核查的数据库连接。工作台能读取目标表字段、自增序列、已有业务父节点和服务重名冲突，但**只读，不直接执行 206 INSERT/UPDATE/DELETE/DDL**。

## 桌面使用方式

在 Windows/macOS/Linux 工作台打开 **Linux 服务器部署**，将执行阶段切换到 **7. 业务数据库表与父节点只读核验**。填写：

1. 206 Linux **实际私有 IP** 和 SSH 端口（Linux 本机模式可省略 SSH）；选择已独立核实指纹的 known_hosts，使用 SSH 密码、私钥或 SSH Agent。
2. PostgreSQL **服务器本机端口**（默认 5432，但须以现场实际为准）。数据库连接通过 SSH 转发至 206 上的 127.0.0.1，**不要求将数据库端口开放到局域网**。
3. 数据库名、只读数据库账号、本次会话密码。建议在 206 由管理员专门分配有目标表 SELECT 权限、没有 DML/DDL 权限的账号。
4. 选择 business_tree_draft.json 或同结构的、已经人工填写真实目标表/字段/父目录的业务目录 JSON；若字段映射尚未明确，则从之前生成的只读发现脚本收集结果再核对。
5. 点击 **执行所选阶段**，本机生成全新 business-db-review-* 文件夹，其中 business_db_readonly_review.json 是逐项核验的证据。

工作台不会将 JSON 中的 schema_verified 改成 true，也不会因为表字段存在就自动认为样式、分类、业务依赖全部通过。

## CLI

连接信息与 GeoServer 208 的配置完全分开管理。建议单独保存一份 **206 SSH 配置**（仅举例，勿复制为生产地址）：

    {
      "mode": "ssh",
      "root": "/srv/geopublisher/review-only",
      "host": "192.168.1.20",
      "port": 22,
      "username": "readonly_ssh",
      "auth": "key",
      "known_hosts": "/local/verified-known-hosts",
      "key_file": "/local/id_ed25519",
      "postgres_port": 5432
    }

root 是通用 LinuxTarget 配置的一部分，本次数据库只读核验**不会向该目录写文件，也无需调用文件传输预检**。主机与所有密钥路径必须由实际管理员确认。账号/密码不要放进 JSON、GitHub、进程命令行或任务日志。

    geopub server-catalog-doctor --profile <本地206连接配置.json> --catalog-json <业务目录.json> --database <数据库名> --database-user <只读用户名> --output <本机全新目录>

终端交互输入 SSH 密码/私钥口令（按认证方式）以及 PostgreSQL 只读账号密码。无交互式终端时不会允许从命令行注入密码。

## 核对项目

- **目标数据库身份**：实际数据库名、用户、目标 schema、transaction_read_only=on 和 PostgreSQL 服务端版本；不匹配直接停止。
- **两张业务表**：service/node 映射到实际关系、字段是否存在、字段是数值还是文本、必填字段能否安全省略。
- **id 自增序列**：存在关系表拥有的自增/序列；缺失时阻断，避免旧业务表手工 ID 序列问题。
- **已存在父节点**：精确核对 code、title、sys 与可选 parent，要求恰好匹配一条记录；不猜测或创建父节点。
- **准备新建的目录、比例尺节点和图幅**：检查 code 或同名同父目录冲突。
- **服务注册**：create 不允许既有同名服务或同 WMS layer；reuse 必须有且仅有一条记录的 key、layer、url、crs、format 全部一致。
- **回退/审核**：结果只写本机受控 JSON，包含来源配置 SHA256、阻断原因和查询到的有限元数据；即使全部通过也标记 schema_verified=false 和 schema_verified_remains_manual=true。

实现使用 psycopg 3，通过 SSH localhost 隧道以 default_transaction_read_only=on + BEGIN READ ONLY + statement_timeout 查询，结束执行 ROLLBACK。只使用固定的 SELECT/系统目录查询、受控 SQL 标识符及绑定参数；不接受任意 SQL 文本作为查询内容。数据库断开/超时返回阻断，不会将不完整结果写成通过报告。

## 流程边界

本功能是 **数据库登记前的审核工具**，不是自动迁移/同步/注册引擎。

只有在真实 208 GeoServer WMS/WFS 验证、MXD/GDB→SHP/SLD 转换报告、业务图幅归属、真实数据库字段、既有父节点及 SQL 事务审核均完成后，才能生成并由授权操作员在 DataGrip/psql 中执行经审查的 02_register_reviewed.sql。自动执行生产 DML、备份和回滚还没有实现。

**数据安全：** 现场表名、业务节点、服务地址及查询报告仅保存在本机/内网，不得提交 GitHub、上传互联网或发送给外部服务。不要把 206 的 SSH/数据库凭据写入工作台项目文件。

## 测试基线（合成数据）

- Windows 10 + 本机隔离 postgres:16-alpine（实际 PostgreSQL 服务端版本 16.14）：只读连接、字段与序列、唯一父节点均通过。
- 添加一个合成图幅编码冲突后，真实查询给出 BLOCKED_PENDING_HUMAN_REVIEW 并标出对应编码。
- 合成只读账号尝试向测试表 INSERT，被 PostgreSQL 权限系统拒绝。
- 仅在本机端口 127.0.0.1:15439 连接一次性测试实例；测试完成后容器已停止并删除。
- 测试记录保存在 D:\Java-p\GeoPublisher-Workbench-validation\server-lan-20261108，没有复制到公共仓库。真实 206 尚未连接或核验。
