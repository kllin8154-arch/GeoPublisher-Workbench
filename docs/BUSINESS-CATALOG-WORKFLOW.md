# 通用 GIS 转换 → GeoServer 发布 → 业务目录 SQL 工作流

本工作流适用于多个业务专题、地质图幅比例尺、世界/区域底图。**文件格式、GeoServer 图层和业务节点属于不同对象，不能把所有资源挂在底图下。**

## 一、四类信息分别维护

1. 源资料：MXD、GDB、SHP、GeoTIFF、LYR/QML/SLD。一个 MXD/GDB 可能对应多个真实空间要素图层，不能把 MXD 当成可直接发布的 SHP。
2. GeoServer：工作空间、存储源、图层、SRS、SLD、真实 WMS（必要时 WFS）证据。
3. 业务服务：唯一业务服务编码，必须准确指向已经发布成功的 workspace:layer；可以与文件名不同。
4. 业务目录：专题 → 比例尺 → 具体图幅，或底图 → 世界/中国/湖南影像；每个节点的 code、namec、glcode 从现场表核实。

## 二、格式与发布类型路由

| 原始类型 | 离线转换 / 校验 | GeoServer 存储 | 重要检查 |
|---|---|---|---|
| MXD | ArcMap 10.x 解析原始数据引用，每图层导出 SHP + LYR | Shapefile | 断链、坐标系、注记和符号保真 |
| GDB | GDAL OpenFileGDB 逐要素类导出 SHP + 字段映射报告 | Shapefile | 字段截断、中文编码、属性完整性 |
| SHP | 配对 SHX/DBF/PRJ/CPG、几何和 CRS 检查 | Shapefile | PNG WMS、必要时 WFS |
| LYR / QML / SLD | QGIS/SLYR 样式转换；不作为空间数据直接发布 | 样式资源 | 分类线型、图例、注记 |
| 单张 TIFF | GeoTIFF，按需构建分块、overview、检查 NoData | GeoTIFF | 透明边界、缩放和画面 |
| 相邻 TIFF 分幅 | 统一坐标系与格式，配置相对路径索引 | ImageMosaic | granules、接缝、实际 WMS |
| 同一区域的多级金字塔 | 核对金字塔目录及各级别覆盖范围 | ImagePyramid | 级别、裁剪、边界 |

当前工作台的安全发布核心自动支持 SHP 和单张 GeoTIFF。MXD/GDB/LYR 提供本机离线转换入口。ImageMosaic/ImagePyramid 目前仍应采用独立的受控流程和手工审核，不能因为存在多个 TIFF 就假定能自动拼成可发布服务。

## 三、多级业务分类与比例尺

采用可配置树而非固定 baseMap 目录：

- existing_parents：待复用的已有父节点，填写实际 code/title；SQL 要求它在目标数据库中恰好存在一次。不能再次插入已经存在的工作程度图幅。
- groups：需要新增的专题或比例尺父目录。每个节点显式填写 code、title、parent、display_order、layer_index；parent=null 表示新建一个顶层业务分类。
- layers：实际新图层，填 code、title、parent、service_key、qualified_name、source_format、publish_kind、crs、zoom_start、zoom_end、layer_index、wms。
- group.scale 与 layer.scale 必须一致，否则拒绝输出可执行 SQL。
- 可将已验证并确实存在的服务设置为 service_mode=reuse：引用旧服务，不重复 INSERT。已有的旧节点本身无需进入新建清单。

业务目录示例（仅信息结构示意，非实际已核实的节点编码）：

    业务目录
    ├─ 底图（已有父节点）
    │  └─ 世界影像 → 全球概览 / 全球高清
    ├─ 基础地质
    │  ├─ 1:5万基础地质图 → 每一幅图
    │  ├─ 1:20万基础地质图 → 每一幅图
    │  └─ 1:25万基础地质图 → 每一幅图
    ├─ 水文地质 → 水文图层 / 水文点等专题
    └─ 工作程度图幅（已有时复用）
       └─ 1:5万 / 1:20万 / 1:25万 / 1:50万（依现场目录决定）

**水文点属于专题要素而不必然属于比例尺图幅**；业务类型需人为判断，不因扩展名自动分配父节点。

## 四、CLI 与桌面入口

CLI（本地离线生成）：

    geopub catalog-tree examples/business-tree.example.json --output D:\LocalGIS\catalog-review-new

桌面“发布任务”中新增按钮“业务分类 / 比例尺 SQL”。选择经人工审核的 JSON 配置和输出父目录，程序创建全新审核子目录，内容：

- 01_preflight_readonly.sql：仅查业务表结构、父节点、重名服务及图层。
- 02_register_reviewed.sql：只有业务表结构、源转换和 WMS 真实 PNG 人工审查全部通过才生成；包含事务、父子引用校验及查重。
- 02_register_BLOCKED.sql：条件不满足时仅保留阻断语句，不含 INSERT。
- 03_verify_readonly.sql：核对新增服务、目录组、图层节点是否符合配置。
- 04_rollback_review_readonly.sql：仅列出精确撤销候选记录，不删除其他人的节点。
- business_tree_review.json：人可理解的树结构、来源类型、比例尺、核验状态、阻断原因。

示例配置在 examples/business-tree.example.json，完全为合成示例，schema_verified=false、WMS=PENDING，**只能生成阻断脚本，绝不能复制当作现场 SQL 使用。**

生成后仍不连接数据库。即使产生 02_register_reviewed.sql，也必须先核对真实生产库表、唯一父节点、真实 WMS、备份方案，由授权人员手工决定是否执行。

## 五、执行前的强制检查与安全边界

- 源资料不可修改/覆盖。MXD/GDB 完成 SHP 导出后要验证几何、字段与样式报告，尤其长字段缩名和中文编码。
- WMS 必须为目标 GeoServer 返回的真实 PNG，不得以浏览器示意图或其他服务的图片代替。保存本机 PNG 并人工核对位置、颜色、接缝、标签及比例尺。
- 数据库 profile 由现场人员核实。必须为每个 service 和 node 表提供真实名称、字段名，要求自增 id 序列；表名不带 schema 前缀，生成 SQL 明确设置 search_path。
- 新业务节点默认 visible=0。现有服务与父节点只复用不重建。
- 所有 SQL 均只生成、不自动执行。SQL 具有事务与服务/节点/同父重名保护；没有默认数据库密码、连接、上传或生产修改。
- 回滚不自动执行 DELETE，因为不能只凭本地文件断言远端对象仍为当前任务所有；需要操作人员核对后精确处理。
- 工具源代码、测试和合成示例不含现场服务器配置或真实 GIS 数据。现场 profile 仅保存在本机或授权内网目录，不提交公开仓库。

## 六、后续可能扩展

可进一步把已经完成的任务日志与 WMS 验证报告映射成业务目录 JSON 初稿，自动提出需要归类的每个图层，再由人员确认父目录、专题、比例尺；以及在完成安全与实际环境测试后加入正式 ImageMosaic/ImagePyramid 发布适配。


## 从 GeoServer 发布任务自动生成图幅分类初稿

完成“选择数据 → 本地检查 → 确认发布”后，可以使用原有任务文件自动整理图幅，而不必重新手填所有服务层名。

桌面：进入 **发布任务 → 业务分类 / 比例尺 SQL**，选择原任务中的 plan.json；工作台会生成全新 catalog-draft-* 文件夹，包含 business_tree_draft.json（待编辑）和 draft_review.json（逐层待确认事项）。也可以选择一份已经核实的业务目录 JSON，在同一入口生成 SQL。

CLI：

    geopub catalog-draft D:\LocalGIS\task-123\plan.json --output D:\LocalGIS\catalog-draft-new

如有经审核的业务表名/字段模板，可带 --template D:\LocalGIS\site-reviewed-schema.json。它仅预填表与列的候选值，**始终重置 schema_verified=false**，也不会继承模板中的已存在父目录或图层：避免把上一现场目录套到新现场。

自动提取的仅有：发布任务 ID、受控工作空间名、限定图层名、SHP/独立 GeoTIFF 类型、明确的 EPSG 编码。若任务目录下的 report.json 与计划完全匹配、该图层记录为 SUCCEEDED 且真实预览路径位于任务目录内，可附带 PNG 位置供人工复核；不会自动将 WMS 状态改为 PASSED。

所有图幅初始均属于 draft_unclassified，且 classification_reviewed=false、WMS=PENDING。即使操作员直接改为 WMS=PASSED，**临时未分类目录仍会永久阻断数据库 INSERT**；必须把图幅移动至真实专题/比例尺父目录，删除临时组，并单独完成 WMS、业务结构与数据来源核验，才可生成审核 SQL。

源数据可能先经 MXD/GDB→SHP 导出。plan.json 仅能证明发布的 SHP，并不能恢复原 GDB/MXD 来源的全部业务信息；请依照转换报告与原图层清单进行复核。生成器不会猜测比例尺、坐标系转换或业务分类，也不会接触 GeoServer/数据库、网络或远程文件共享。
