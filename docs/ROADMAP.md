# 开发路线与验收状态

## M1：可执行发布原型

- [x] 仓库、Python 包、CLI 和接口边界
- [x] 受控 GDAL 运行时锁文件（Linux/Windows）
- [x] SHP/TIFF 结构预检、独立打包和 SLD
- [x] GeoServer REST 与真实 WMS/WFS
- [x] SQLite 意图、所有权、恢复锁和事件
- [x] 新建、跳过、固定名称新版本
- [x] 中文桌面薄壳与实际地图预览
- [x] 目录字段映射导出机制
- [x] Windows 开发环境中的原生 GDAL、中文路径及桌面预检验证（见 Windows 报告）
- [x] Windows 内部便携预览及桌面快捷方式（含受控 GDAL、原生桌面/CRS 验证）
- [x] WMS 空白主视图的有限象限补充检查与审计（非完整 Gate D）
- [x] 隔离 GeoServer 2.24.4 上的真实 WMS 比例尺样式空白→局部出图回退；只读 GetMap 与业务发布复用同一核心
- [x] 原生 GDAL 3.13.3 生成/预检 NoData 和 RGBA Alpha 数学合成 TIFF（仅本地前置，不代表服务端 WMS）
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
- [ ] SFTP/共享目录的大文件传输
- [ ] 已知 ImagePyramid 目录兼容
- [ ] QML 无界面转换原型门槛
- [ ] 样式/数据更新的数据级备份与缓存一致性

## 后续独立版本

ImageMosaic/harvest、PostGIS/GeoPackage/FileGDB、业务库自动写入、ArcGIS 项目适配、团队任务中心。

原位替换、自动写库和 data_dir 维护不由一般功能扩展隐式启用。
