# Windows 本机 ArcMap → QGIS 真实离线转换验收（2026-11-02）

## 验收目标与边界

本次只使用本机数学合成的点、线、面，不使用真实地质工程或生产 GeoServer。目标是验证以前仅有模拟测试的真实二进制 MXD、ArcPy、LYR、QGIS Style Assistant、SLYR 和 GeoPublisher SLD 检查能连通。所有工作目录和证据留在本机 D 盘，仓库不提交样本、MXD、LYR、SHP、SLD、环境档案或日志。

## 软件能力核实

- ArcGIS Desktop **10.8**，ArcPy 使用 Python **2.7.16**，`ProductInfo() = ArcInfo`，`SpatialReference(4326)` 可用。
- Windows 本机提供 ArcMap 10.8 的真实空白 MXD 页面模板，程序从该模板保存有效二进制 MXD；非假造文件。
- QGIS **4.2.1**、QGIS Python **3.12.13**，PyQGIS 加载成功，SLYR Community 可用，QGIS Style Assistant 0.4.0 的样式函数可调用。插件由用户已有的 QGIS profile 提供，未复制进仓库。

## 真实转换验证

1. 运行 `arcmap_create_synthetic_mxd.py`，生成 1 个 262144 字节的有效 MXD 和 3 个数学合成 SHP（TEST_POINT、TEST_LINE、TEST_AREA）。无网络或真实业务数据。
2. 用真正的 ArcPy 执行 `arcmap_mxd_to_shp.py`：导出清单列明 **3 个 EXPORTED**，实际生成 **3 SHP + 3 LYR**。ArcPy 能重新打开刚生成的 MXD。
3. 首次执行发现 Python 2.7 在被重定向的 stdout 上打印中文时发生 `UnicodeEncodeError`，虽然导出已完成但进程返回 1。已通过按控制台编码安全写入字节解决，重新运行退出 0；再次传入旧输出目录会明确拒绝覆盖，退出 2。
4. QGIS 真正读取这 3 个 ArcMap LYR，通过 SLYR 应用到同名 SHP 并导出 QML 与 SLD。使用新加入的 `qgis_lyr_to_sld.py` 再独立批量运行，得到 **3 个 EXPORTED，进程退出 0，报告 PASSED**；既支持显式 `--qgis-prefix` 也支持本机默认 QGIS 配置。
5. 各 SHP 均为 **1 个要素，EPSG:4326**。SLD 1.1.0 在 GeoPublisher 受控校验中分别包含 PointSymbolizer、LineSymbolizer、PolygonSymbolizer；没有外部资源引用或阻断错误。
6. 另复制 18 个纯合成 SHP/LYR 组件到包含**中文与空格**的本地目录，使用中文输出路径再次转换，**3 个全部通过**；转换前后 18 个源文件的 SHA256 全部一致。

## 安全检查与测试

- 新的 QGIS 批处理脚本仅按同名严格配对，前置条件检查包含 SHP/SHX/DBF/PRJ/CPG 与 LYR。缺失、同名冲突、符号链接、旧输出目录、重叠输入输出目录均禁止静默跳过或覆盖。
- 样式文件仅在全新输出目录生成，QML/SLD 双文件校验通过后才进入成功结果；失败产物保留在 `artifacts/`，失败不会显示成“全部完成”。
- SLD 导出限制为 5 MiB，拒绝外部图形、OnlineResource、远程 href 与几何类型不匹配。
- 已加入 Python 2.7 重定向输出回归、QGIS 配对/输出/SLD 安全测试、桌面脚本导出不覆盖测试。
- 新增专项组合测试：**68 passed**。完整 Windows 本机 pytest 连续两轮均为 **531 passed、5 skipped**（37.10 秒、30.33 秒，退出码 0）；5 项跳过为本机当前符号链接权限限制。Windows/Ubuntu CI 以最终 PR 结果为准。

## 证据和未完成项

- 本机证据：`D:\Java-p\GeoPublisher-Workbench-validation\arcgis-real-20261102`。原始文件和结果仅在本机，严禁提交仓库或发送到任何远程服务器。
- 本次验证证明**真实 ArcMap 二进制转换 + QGIS 样式导出流程可用**，但只用默认点线面符号，没有证明复杂地质渲染、中文标注、比例尺、透明度或实际 GeoServer WMS 像素级视觉等价。
- 未连接或更改生产 GeoServer、业务数据库；1 GB TIFF 基准、完整干净断网发行包及真实地质样式现场验收仍未完成。
