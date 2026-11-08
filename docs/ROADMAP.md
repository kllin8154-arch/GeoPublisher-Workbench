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
