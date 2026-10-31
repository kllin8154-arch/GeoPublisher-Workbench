# 业务目录 SQL 导出

目录模块只读取发布结果和经人工核验的字段映射，生成 PostgreSQL 注册、查询验证和人工回滚核对脚本。它不连接数据库，不执行 SQL，不猜测任何现场表结构。

`catalog-bindings.json` 由发布引擎产生，只包含实际验收成功的图层。生成 SQL 后，结果仍为 `SQL_GENERATED_NOT_EXECUTED`、`registered: false`；这不表示业务页面已经可以访问。

## 输入与接口

```python
from pathlib import Path
from geopublisher.catalog import generate_catalog

result = generate_catalog(bindings, reviewed_mapping, Path("data/catalog-output"))
```

返回 `registration_sql`、`verification_sql`、`rollback_sql` 的绝对路径，以及任务标识、映射摘要、图层数量和 `rollback_mode: manual-review`。

绑定文件必须为 `schema_version: 1`，且 `registered: false`。每个图层必须包含 `task_id`、`qualified_name`、`wms_url`、`crs`、`title`，一次最多 1000 个，且属于同一任务。WMS URL 不接受凭据、查询串和片段。

## 字段映射

`examples/catalog-mapping.example.json` 使用完全虚构的 `demo_mapservice`、`demo_maplayer`，不是任何现场数据库结构。示例中的核验标志故意为 `false`，不能直接用于生成现场 SQL。

先确认目标数据库的实际 schema、表、字段类型、非空字段、默认值、唯一键、关联、触发器及访问权限，再填写并设置：

- `schema_verified: true`：确认目标 schema 与表字段。
- 两张表的 `unique_key_verified: true`：确认 `key` 对应单字段主键或非部分唯一索引。
- 图层表的 `service_reference_verified: true`：确认关联字段确实引用服务表的唯一键。

程序不创建表、索引、归属字段或数据库序列。实际结构无法满足当前映射规则时，应先补相应适配器，不能把示例字段照搬到现场。

每个 `columns` 字段只能选择一种值来源：

```json
{
  "layer_title": { "source": "title" },
  "enabled": { "value": true }
}
```

不接受原始 SQL 表达式、`nextval()`、动态函数或自定义脚本。首版使用明确的文本/整数键值，不自动分配流水号，也不支持复合唯一键。

| 使用位置 | 可用来源 |
|---|---|
| 服务字段 | `task_id`、`wms_url`、`service_token`、`service_index` |
| 图层字段 | `task_id`、`qualified_name`、`wms_url`、`title`、`crs`、`layer_index`、`service_key` |

`service_token` 由任务 ID 与服务地址摘要确定，同一输入生成相同值；`service_index` 按服务 URL 排序从 1 开始。相同 WMS URL 归入一个服务。`service_key` 是解析服务映射后得到的服务唯一键值。

`layer_index` 按绑定清单顺序生成，从 `layer_order_start` 开始。业务规则为“序号越小越靠底”时，应先核对清单顺序和已有目录顺序；生成器不读取数据库中的最大序号。

标识符只接受 1–63 位 ASCII 字母、数字和下划线，首字符不能为数字。业务表名不带 schema 前缀。目标模式通过事务内 `search_path` 指定，并在注册脚本执行时校验解析到的表确实属于该模式。

## 服务创建和复用

默认 `service.mode: create`。服务键或任何图层键已经存在时，注册事务直接报错，所有 INSERT 都不会提交。注册脚本不含 UPDATE、UPSERT 或隐式覆盖。不要在已成功执行后重复运行注册脚本；先运行验证脚本核对现状。

明确需要使用已有服务时，可采用 `service.mode: reuse-verified`，并提供该服务已确认的键与字段值。执行时必须找到全部映射字段值匹配的服务，才能继续插入图层；不会更新或创建该服务。图层键仍必须全新。

注册脚本会再次检查两张表和单字段非部分唯一索引存在。唯一约束负责防止两个并发事务插入同一键。应用层预检查不能取代数据库约束。

## 三份脚本的含义

### register.sql

完整事务包含超时设置、受控查找路径、结构和冲突检查以及 INSERT。应整体审阅、整体执行，不能跳过保护条件只复制 INSERT。任一步失败会使事务不能提交。

### verify.sql

只读事务按目标键返回 `MISSING`、`MATCHED` 或 `MISMATCH`，同时显示键匹配数量与映射字段匹配数量。`MATCHED` 只说明当前已映射字段一致；未映射列、触发器效果和业务页面行为仍需现场验证。

### rollback.sql

这是**人工恢复核对脚本，不含 DELETE**。文件会列出目标记录的当前匹配状态、每个服务的全部引用图层数，以及当前计划之外的引用数。

仅凭 SQL 文件生成成功，无法证明注册脚本已经执行，也无法证明某一行由本任务实际插入。即使设置了 `created_task` 一类普通字段，也不能据此声称具备经过验证的完整执行回执和整行变更快照。因此本版本不生成自动删除。

真正清理前，需要人工核对数据库执行记录、任务前后快照、当前整行内容及外部引用。已有服务、发生变化的记录、非本任务创建的记录和仍被其他图层引用的父服务都不得删除。

## 输出保护与边界

- 不覆盖不同内容的已有 SQL 文件；相同输入可以重用相同输出。
- 拒绝符号链接输出路径。
- 所有值按 PostgreSQL 字面量转义，DO 语句的美元引号定界符也会避免与输入内容冲突。
- 不调用数据库客户端或执行外部脚本。
- SQL 输出保存在任务数据目录，不应提交到源代码仓库。
- 首版未针对任一现场真实 schema 验收；使用前必须核验字段映射。

## 技术依据

- [PostgreSQL 16：词法结构与字符串](https://www.postgresql.org/docs/16/sql-syntax-lexical.html)
- [PostgreSQL 16：DO](https://www.postgresql.org/docs/16/sql-do.html)
- [PostgreSQL 16：pg_index](https://www.postgresql.org/docs/16/catalog-pg-index.html)
