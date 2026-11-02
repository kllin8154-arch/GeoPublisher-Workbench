# 真实 GeoServer 分类样式测试：GDB → SHP → 字段映射 → SLD → WMS

> 2026-11-02。**已通过真实只读 WMS 测试；不是正式 REST 写入测试，更不是实际 MXD/LYR 转换验收。**

## 测试范围与隔离

仅在本机 D 盘创建数学合成的两个多边形。它们分别带有属性 `LongFormationCategory=LEFT` / `RIGHT`，以及纯示例中文标签“合成甲”“合成乙”，并非任何真实地质数据。

验证环境：GDAL 3.13.3，GeoServer 2.24.4 的独立复制实例，仅监听 `127.0.0.1:18884`；不复用生产服务、项目 GIS 数据或生产数据库。全部源文件与产物保留在：

`D:\Java-p\GeoPublisher-Workbench-validation\synthetic-sld-lab-20261102`

## 端到端验证步骤

1. 使用 GDAL OpenFileGDB 在该测试目录数学生成有效的 `synthetic-original.gdb`，两个左右分离的 EPSG:4326 多边形。
2. 通过正式的 `convert_gdb()` 执行 GDB → SHP，逐文件验证原始 GDB 哈希、源/目标要素数量与字段名映射。长字段 `LongFormationCategory` 转为 `LONGFO_E65`，保留 `conversion-report.json`。
3. 以数学创建的两级 SLD 做测试。第一条规则字段值 `LEFT` 填充 `#D43B36`；第二条 `RIGHT` 填充 `#2762CE`。原 SLD 仍引用长字段名。
4. 使用正式 `build_plan()` 和 `remap_sld_fields=True`，在**本地**冻结新样式副本。计划 `ready=true`，记录原始与修复的 SHA256，原 GDB/SHP/SLD 均未修改。
5. 仅在 GeoServer 停止状态下，为**该测试副本的 data_dir** 准备新的数学 SHP、Catalog XML 与样式。并非 REST 发布流程。
6. 启动隔离 GeoServer，确认监听的唯一地址为 `127.0.0.1:18884` 和 PID 对应测试 Java 进程，然后仅发未经认证的 WMS `GetCapabilities` 与 `GetMap` 两类 GET。
7. 校验实际 WMS 图层可见、512×512 PNG 可解码，以及左右两处内部像素颜色、背景透明度。
8. 结束后仅停止该隔离 Java PID，已确认端口 18884 不再监听。源数据、已验收报告和测试数据目录保留供本机复核。

## 真实 WMS 结果

| 项目 | 预期与实际 |
|---|---|
| WMS 层 | `topp:SYNTHETIC_CLASS`，Capabilites 存在 |
| 左侧内部像素 (163,256) | `RGBA [212,59,54,255]`；与 `#D43B36` 完全一致 |
| 右侧内部像素 (349,256) | `RGBA [39,98,206,255]`；与 `#2762CE` 完全一致 |
| 背景像素 (256,55) | `RGBA [255,255,255,0]`，全透明 |
| 真实可见像素 | 53,280 |
| WMS 图像摘要 | `05507283e3ab60a70e071c24b9cc6729a87f76ba7f33fcbdf2ead15db74070f1` |
| 样式字段 | `LongFormationCategory → LONGFO_E65` |
| 服务端写请求 | **0**（此轮样式目录于停机状态准备） |
| WMS 最终结论 | **PASSED** |

已使用最初的只读脚本和仓库中重构后的独立只读验收脚本分别验证，二者得到相同的分类颜色和 PNG SHA256。

本机验证结果：

`D:\Java-p\GeoPublisher-Workbench-validation\synthetic-sld-lab-20261102\read-only-wms-final.json`

## 可重复执行的测试脚本

- `scripts/integration_sld_classification.py prepare`：先生成数学 GeoJSON/OpenFileGDB、使用正式 GDB→SHP 转换，并冻结已修复字段引用的 SLD 计划。**本地离线，无服务器请求。**
- `scripts/prepare_disposable_sld_catalog.py`：仅接受此指定数学测试目录，要求 GeoServer 已停止，在**现有测试副本**中加入合成 SHP 与样式；拒绝覆盖已有测试配置。
- `scripts/integration_sld_classification.py verify-readonly`：只接受验证过的 Windows Java 回环监听与数学数据标记，调用只读 GetCapabilities/GetMap，检查实际 RGBA 像素。不包含 GeoServer REST 写入模式。
- `tests/test_synthetic_sld_classification.py`：纯数学单测覆盖源几何/分类、SLD 两规则、真实 PNG 像素判断、颜色错位与背景不透明反例、Catalog 模板引用一致性与非法测试目录阻断。

**注意：** 本次真实 WMS 所使用的测试 catalog 最初由本机独立的停机准备脚本生成；仓库中的通用准备脚本后续已做纯函数回归测试，但尚未在第二个全新服务副本独立完成完整准备/启动/清理循环。真实测试证据对应本轮已记录的服务副本，不夸大覆盖范围。

## 未通过或未测试的部分

- 正式 GeoPublisher REST 上传这份合成 SHP、真正的持久化图层创建/恢复、重跑零新增写入与精确配置清理：**本轮未执行**。写请求执行被安全检查阻断，已改为纯只读 WMS 验证，未尝试绕过。
- ArcMap MXD 二进制读取、SLYR 解析真实 LYR、QGIS 导出分类/标注与 GeoServer 的真实视觉等价：**尚待安装相应软件的离线环境**。
- 真实地质符号库、复杂文本标注/表达式、超长中文 DBF 字段值和特殊 CRS：**需要独立验证**。

成功出图并不等于真实地质业务地图的空间、颜色、文字与图例绝对正确。所有测试地质资料仍只在本机 D 盘处理，不上传公开仓库。
