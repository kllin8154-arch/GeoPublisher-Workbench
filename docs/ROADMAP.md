# 开发路线与验收状态

## M1：可执行发布原型

- [x] 仓库、Python 包、CLI 和接口边界
- [x] 受控 GDAL 运行时锁文件（Linux/Windows）
- [x] SHP/TIFF 结构预检、独立打包和 SLD
- [x] GeoServer REST 与真实 WMS/WFS
- [x] SQLite 意图、所有权、恢复锁和事件
- [x] 新建、跳过、固定名称新版本
- [x] 中文桌面薄壳与实际地图预览
- [x] QThread 排队启动消除极快 worker 的线程事件循环竞态，连续压力回归及 Windows 多轮全量测试；[验证记录](VALIDATION-QT-WORKER-2026-11-02.md)
- [x] 正在检查、转换或发布时的安全关闭交互：检查完成后自动关闭、发布安全取消、清理会话凭据；[验证记录](VALIDATION-CLOSE-WHILE-RUNNING-2026-11-02.md)
- [x] 目录字段映射导出机制
- [x] Windows 开发环境中的原生 GDAL、中文路径及桌面预检验证（见 Windows 报告）
- [x] Windows 内部便携预览及桌面快捷方式（含受控 GDAL、原生桌面/CRS 验证）
- [x] WMS 空白主视图的有限象限补充检查与审计（非完整 Gate D）
- [x] 隔离 GeoServer 2.24.4 上的真实 WMS 比例尺样式空白→局部出图回退；只读 GetMap 与业务发布复用同一核心
- [x] 原生 GDAL 3.13.3 生成/预检 NoData 和 RGBA Alpha 数学合成 TIFF（仅本地前置，不代表服务端 WMS）
- [x] 一次性 GeoServer 2.24.4 从停机准备的数学 TIFF 测试目录加载两个覆盖图层，真实 WMS 逐像素验证 NoData/Alpha（仅 WMS 子项，非正式 REST 发布）
- [ ] 完整可再分发离线包（许可证合规、签名、干净机断网验收；详见 RELEASE-AUDIT）

## M2：现场试用

- [x] 用户本机 D 盘检出并运行
- [ ] 用授权测试 GeoServer 校验真实代理、权限、证书和文件大小限制
- [ ] 在现场本地确认目录表字段/主键/关系，建立项目适配模板
- [ ] 用户本机运行脱敏或授权样本，核对真实中文字体和样式
- [ ] 1 GB TIFF 和代表性 SHP 的资源/性能基准

## M3：优先扩展

- [x] 多 TIFF 显式独立发布意图、已知 Mosaic/Pyramid 标记阻断与冻结来源
- [x] 本次多 TIFF 改动的 Windows 原生增量验收与双平台 CI（见 Windows 报告第 8 节）
- [x] GDB→SHP 受控 GDAL 离线转换原型，按图层预检/选择、短字段映射与 CSV、失败隔离，已有真实本地测试 GDB 证据
- [x] MXD→SHP/LYR 的 ArcMap 10.x 离线辅助脚本与纯模拟调用测试；LYR→SLD 经 QGIS 插件导出后同名批量配对
- [x] GDB 字段对照与 SLD 的简单字段引用显式映射，逐文件哈希和计划副本留痕，复杂表达式拒绝自动修复
- [x] 数学生成 GDB→SHP→短字段映射→SLD 在 GeoServer 2.24.4 上的真实只读 WMS 分类像素验证（停机准备测试 catalog，非正式 REST 发布；[验证](VALIDATION-SLD-CLASSIFICATION-2026-11-02.md)）
- [x] 真正的 ArcMap 10.8 合成二进制 MXD→SHP/LYR 与 QGIS 4.2.1 + SLYR 批量 QML/SLD 离线转换，中文路径及源文件 SHA256 验证通过；[本机验收](VALIDATION-ARCMAP-QGIS-2026-11-02.md)
- [x] 中文桌面界面直接执行受控本地 ArcMap MXD 与 QGIS LYR 转换、可见进度与安全取消、结果报告及人工确认后回填 SHP/SLD；[验收](VALIDATION-DIRECT-MXD-LYR-2026-11-02.md)
- [ ] 用授权的复杂 MXD/LYR 在离线 ArcMap/QGIS 验证样式、标注与 GeoServer WMS 实图视觉一致性
- [ ] SFTP/共享目录的大文件传输
- [ ] 已知 ImagePyramid 目录兼容
- [ ] QML 无界面转换原型门槛
- [ ] 样式/数据更新的数据级备份与缓存一致性

## 后续独立版本

ImageMosaic/harvest、PostGIS/GeoPackage/FileGDB、业务库自动写入、ArcGIS 项目适配、团队任务中心。

原位替换、自动写库和 data_dir 维护不由一般功能扩展隐式启用。


## 多级业务目录与各比例尺图幅（2026-11-08）

- [x] 通用目录树模型：已存在父节点、独立专题、多个比例尺分组和实际图幅；不再限定 baseMap。
- [x] 按来源标识 MXD/GDB→SHP、SHP、GeoTIFF、ImageMosaic；拒绝错误数据存储类型和比例尺错挂。
- [x] CLI 与中文桌面入口：生成只读目录预检、带事务和碰撞保护的入库 SQL、验证 SQL、只读撤销审查及树摘要。
- [x] 未核验生产 schema、MXD/GDB 转换或目标 WMS PNG 时，只生成不可执行的阻断 SQL。
- [x] 合成目录 SQL 在本机 PostgreSQL 16 验证多级插入、旧节点保留、重复执行阻断与字段结构保护。
- [ ] 现场 206 目录真实父节点与基础地质/水文等比例尺编码核实；208 真实图幅逐图层 WMS/WFS 确认。
- [ ] 按现场授权的真实目录映射输出可审核的生产 SQL；默认不自动连接/执行数据库。
- [ ] 将经核对的发布验收任务自动转换为目录配置初稿，再由用户选择专题和比例尺（当前先手工选 JSON）。


## 自动提取已发布图层的目录初稿（2026-11-08）

- [x] 从哈希校验过的 plan.json 读取图层名、工作空间、SHP/GeoTIFF 类型和原始 EPSG；不修改 plan。
- [x] 仅从同任务有效 report.json 预填安全的本机 WMS PNG 路径；不将机器成功转换成业务人工验收状态。
- [x] 默认所有图层放在 draft_unclassified，转换来源、专题、比例尺、现有父节点都需人工确认；未删除临时目录永远无法生成可执行登记 SQL。
- [x] 支持 CLI catalog-draft 与桌面“业务分类 / 比例尺 SQL”一键选择任务生成初稿。拒绝 UNC/软链接输出和已存在目录。
- [ ] 可视化的业务目录拖放树编辑器、从 206 只读拉取真实目录并进行现场审阅。


## 局域网远端 GeoServer 发布阶段（2026-11）

- [x] 统一 SSH/SFTP 与 Linux 本机交付适配，私网 IP 白名单、已核验 known_hosts、会话级密码/私钥口令和拒绝符号链接。
- [x] 带进度的独占上传、存储空间预检、归档 SHA256 与 manifest 文件数校验、全新目录安全解包；拒绝覆盖。
- [x] 固定 GeoServer loopback SSH 隧道；本机 Linux 直接使用 localhost；其他服务转发拒绝。
- [x] 服务端 GeoTIFF 与 ImageMosaic 官方 REST 新存储登记、WMS PNG、granule 数量检查与审计日志。
- [x] SHP/单 GeoTIFF 不可变 plan.json 复用既有 SQLite journal、SLD、WMS/WFS 检查，强制与 SSH 服务器地址精确匹配。
- [x] 桌面一步一阶段连接、预检、传输、解包、REST 注册及现有任务发布；写入操作二次确认；SFTP 可请求中止并保留 .partial 以便核对。
- [x] Windows 本机隔离 GeoServer 2.24.4/JDK17 真正验证合成 GeoTIFF + 8 分幅 ImageMosaic WMS；不传外部资料。
- [ ] 目标 208 SSH 主机密钥/运行态/JDK 进程/插件、实际目录权限、服务器 SHA256 和真实 WMS 的现场验证与授权发布。
- [ ] 目标 206 数据库真实字段/父节点只读核验、正式业务分类 SQL 执行与前端验收（当前仍阻断自动写入）。
- [ ] macOS 原生安装包、Linux wheel 离线部署包、服务器侧跨连接断点续传。


## P0 一线实施体验（Ponytail：复用现有功能）

- [x] 结果页中文状态、下一步动作与四阶段进度链路；跳过/未知写入不可误报为全部成功。
- [x] 复用任务内 WMS PNG 打开方式（不发起新请求），复用未分类业务目录初稿生成器。
- [x] 不自动授予业务数据库登记、人工验收或安全重试权限；定向 UI/草稿测试。
- [ ] P1：主发布界面进一步收敛高级参数与环境设置；P2：GDB/MXD/LYR 导入助手和任务历史分类。

## 206 业务数据库只读核验（2026-11-09）

- [x] PostgreSQL 16 通过独立 SSH loopback 隧道或 Linux 本机连接；只读事务 BEGIN READ ONLY，statement_timeout，完成 ROLLBACK。
- [x] 只读校验目标 schema/table、字段及数据类型、非空必填字段、id 自增序列、已有父节点 code/title/sys/parent 及图层/服务冲突。
- [x] UI 第 7 阶段按操作类型动态显示 SSH/GeoServer/PostgreSQL 字段；凭据仅在本次会话内存。
- [x] CLI server-catalog-doctor；只写本机新报告目录，记录配置 SHA256，永不自动批准 schema_verified。
- [x] 本机隔离 PostgreSQL 16.14 合成表只读/服务及节点冲突实测通过；只读角色 INSERT 被拒绝，测试容器已删除。
- [ ] 206 实际表结构/序列、父节点、SELECT 权限现场授权核验和结果人工签收。
- [ ] 生产数据库正式 SQL 手动事务执行、先备份、后查询复核；自动事务执行与回滚尚未上线。
