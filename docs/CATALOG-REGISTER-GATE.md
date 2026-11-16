# 业务数据库父子节点登记闭环（P0-A）

此功能会**实际写入用户指定的 PostgreSQL 数据库**，不同于过去只生成 JSON 草稿或 SQL 文件。默认拒绝写入。本机/离线运行，不访问公网，程序不自动选择现场数据库。

## 最简操作路径

1. 从 GeoServer 发布任务生成 `business_tree_draft.json`。在本机根据真实图幅资料审核专题、比例尺、GDB/MXD 来源父节点及子节点。若一个要素类包含多个图幅，必须先明确图幅编号和边界，不能直接按 SHP 数量登记。删除临时未分类节点。
2. 逐图核对转换/字段/样式和实际 WMS 图片，把对应 `classification_reviewed`、`conversion_reviewed`（GDB/MXD）、`wms.visual_reviewed` 设为真。**仅当确认真实表、主键序列、字段映射时**，将 `schema_verified` 设为真。不能用程序自动猜测此状态。
3. 桌面「更多工具 → Linux 服务器部署 → 7. 业务数据库表与父节点只读核验」，或在离线终端执行 `geopub server-catalog-doctor`。读取指定业务表、列、现有父节点及服务名，保存 `business_db_readonly_review.json`。只读报告与最终审核 JSON 的 SHA256 精确绑定。
4. 核对实际数据库名、服务器地址、schema、父节点与图幅数量。只有只读结论 `READ_ONLY_EVIDENCE_READY_NOT_APPROVED` 且无阻断项，才能选择「8. 登记审核后的数据库父子节点」。选择原始 JSON 与**刚才对应**的核验报告，输入**独立授权的写权限账号**，二次确认目标信息和 `REGISTER <数据库名> <目录摘要前12位>`。
5. 程序先在同一受控 PG 连接**重新只读核验**；无冲突时自动生成受控 SQL、按事务锁表并校验已有父节点/序列/服务，最后插入并提交。所有登记 SQL 均来自既有 `build_business_tree_sql`，**不能让程序执行任意用户上传的 SQL 文件**。
6. 提交后逐行只读回查父节点、图幅子节点、服务及所有已映射字段。成功状态为 `VERIFIED_REGISTERED`，审计位于本机 `catalog_registration_audit.json`。**仍须用现场业务系统核验前端可见性、图层上下层次及显示顺序**。

CLI 示例（命令行不会包含密码，所有密码交互输入）：

```powershell
geopub server-catalog-doctor --profile server.json --catalog-json final-tree.json --database demo_db --database-user reviewer --output D:\work\db-review-new
geopub server-catalog-register --profile server.json --catalog-json final-tree.json --evidence D:\work\db-review-new\business_db_readonly_review.json --database demo_db --database-user approved_writer --output D:\work\db-registration-new
```

*没有 `--yes` 或无人值守登记选项*。SSH 连接要求已经核对的主机指纹，目标只允许授权的内网地址或 Linux 本机回环。写入凭据不保存到环境档案或日志；所有表名为不带 schema 前缀的审核字段映射。

## 错误处理和恢复

- 未分类、WMS 图片未核验、源转换未审核、schema 未核对、核验文件被改、目标数据库不同、父节点重名、服务冲突：**写入前阻断**。
- SQL 执行超时、连接断开或无法证明提交状态：在本地记录 `MANUAL_RECONCILIATION_REQUIRED`，**不自动重试**。先只读核对数据库，再决定下一步。
- 已提交但逐字段回查失败：同样标记待人工对账，不能把它报告成失败或已验证成功。
- 旧 SQL 自身仍提供人工只读回滚审阅文件，不提供自动 DELETE；任何恢复需核实业务是否有新引用和实际服务器对象所有权。
- 目前仅通过合成数据与专用 PG16 服务库验收程序行为。**未在生产 206 上实际登记、未核验真实前端业务页面，因此不宣称现场部署闭环已正式验收。**

下一阶段 P0-B：基于图幅编号、来源图层与空间范围建立逻辑图幅分组，特别解决“一个要素类包含多个图幅”和“一张图幅由多层组成”的对应关系。当前仍必须人工核对图幅语义。
