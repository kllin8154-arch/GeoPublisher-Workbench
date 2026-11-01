# 隔离 GeoServer REST 栅格发布与桌面易用性增量验收

> 范围：数学合成 GeoTIFF、独立测试 GeoServer 2.24.4、Windows 10 原生桌面。不是生产发布许可或完整失败补偿证明。

## 一、用户界面改动

日常操作固定为三步：**选择数据 → 本地检查与确认 → 发布与结果**。首次环境设置只展示服务器根地址、已有工作空间、GDAL 目录和本次凭据；SLD、SHP 编码、缺失 CRS、图层名、证书、超时、环境变量等默认折叠。

新增：
- **测试连接（只读）**：使用真实 GeoServer 客户端仅调用版本检查和工作空间 GET；不会发布、创建工作空间或写业务库。缺少 GDAL 时也能检查服务器连接。
- **使用帮助**：在窗口中直接说明三步操作、多个 TIFF 确认、Mosaic/Pyramid 边界、异常处理。
- **检查结果引导**：显示“通过”“通过，有提示”“阻断”的具体动作建议，同名策略使用中文。
- **发布结果下一步**：区分“发布成功但业务目录未登记”“地图需人工核对”“未知写入结果请勿盲目重试”“跳过并非更新”。
- **详细运行日志默认收起**，错误时可展开，保留真实 WMS 图片与报告入口。

实测 Qt `windows` 原生窗口中文正常；`QT_QPA_PLATFORM=offscreen` 的无界面截图不能加载正确中文字体，因此**不能以无界面方框截图代表真实桌面 UI**。文档图片采用本机原生 Windows Qt 截图，且未使用真实用户数据或登录凭据。界面中的服务器地址为可编辑示例，不代表已连接。

## 二、真实 REST 发布：首次发现失败

本次创建了**一次性** GeoServer 2.24.4 测试实例：

- 独立 server 目录：`D:\Java-p\GeoPublisher-Workbench-validation\geoserver-disposable-rest-20261101`
- 绑定地址：`127.0.0.1:18883`，非外网；会话结束后已按 PID 停止实例。
- 输入：干净复制、各自带 EPSG:4326 的 `nodata-gray.tif` 和 `alpha-rgba.tif`，已逐个核对合成源 SHA256。
- 工作空间：`gp_mask_lab`（只在该测试实例内创建）。对预置 `topp:states` 留存快照以检查无关图层不受影响。
- 凭据：从测试进程环境变量读取，不写入报告/代码仓库；不使用生产凭据。

**修复前发现真实缺陷：** 默认 SLD 在所有 3 波段以上影像上显式指定 Red/Green/Blue 1、2、3，GeoServer 2.24.4 因而忽略第 4 波段的原生 Alpha 信息。原始数学 RGBA TIFF 包含 Alpha=0/96/255，但正式发布路径的 WMS 变为 `{255:262144}`，即 **全不透明**。虽然 `run_plan()` 的基础 WMS 检查返回 `SUCCEEDED`，它只能证明有效 PNG 出图，不能证明 Alpha 语义正确。第一次严格综合验收正确标记 `REVIEW_REQUIRED`，**不得记为通过**。

### 修复方案

只对 GDAL `gdalinfo` 已确认 **4 波段且第 4 波段 ColorInterpretation=Alpha** 的 GeoTIFF，默认 SLD 不再强制加入 `ChannelSelection`，而是让 GeoServer 采用原生 RGBA 渲染。普通 3 波段 RGB 和未声明 Alpha 的 4 波段影像继续保留显式 RGB 1/2/3。自定义 SLD 不受影响。新增代码与测试覆盖此行为。

### 修复后的第二轮真实复验

在**同一隔离服务器、新的独立工作空间** `gp_mask_lab_r02` 重新执行完整流程，未重用首次任务计划。

| 检查 | 实测 |
|---|---|
| GeoServer / GDAL | 2.24.4 / 3.13.3 |
| 首次正式 `run_plan()` | `SUCCEEDED`，2 个图层 |
| 首次服务器写操作 | 8 次（不含专用工作空间初始化） |
| 相同计划再次执行 | `SUCCEEDED`，新增写操作 **0 次** |
| 灰度 NoData 真实 WMS Alpha | `0:196608, 255:65536` |
| RGBA 真实 WMS Alpha | `0:196608, 96:15360, 255:50176` |
| 明确 `rollback_plan()` | `ROLLED_BACK`，精确移除 **8 个服务配置对象** |
| 测试无关 `topp:states` | 完全保持原指纹 |
| 本任务 Layer 残留 | 无 |
| 原始上传数据文件 | 按原安全设计保留，**未执行物理数据删除** |
| 严格最终结果 | `PASSED` |

数学 RGBA 修复后的 GeoServer WMS PNG SHA256：`3d4e76a78c2d0de90a0f360cd139034d6a462117f9cced182643fa45950ad2d9`。灰度 NoData PNG SHA256：`61d0fca8a851ecd46c5e992a33c2904af3884f45d1a69ad1491d4ad8210a8786`。两者和此前停机准备数据目录的 WMS 对照一致。

本机原始 JSON 留在：
`D:\Java-p\GeoPublisher-Workbench-validation\rest-mask-integration-r02\rest-integration-result.json`。

## 三、未知写入结果与后续门槛

现有引擎和单元测试已有“服务器收到上传但客户端响应丢失”后停止、不得盲目重试的逻辑。本轮新增测试脚手架可受控模拟 TIFF PUT 响应丢失，并使用**无网络** `httpx.MockTransport` 验证“不自动重放同一次写请求”。

本轮未取得新场景的**真实 GeoServer 故障注入通过证据**；不得将已有 SHP 的真实响应丢失验证或新增 MockTransport 单测冒充 GeoTIFF 的真实服务故障注入。它仍列在恢复门槛 #9 中。

仍未完成：
1. 用户授权场景下的复杂样式与业务地理内容人工验收；
2. 真实 REST 在更多异常阶段的故障矩阵及授权恢复；
3. 许可证全文、NOTICE、签名及无开发工具/断网 Windows 干净机验收（#8）；
4. 正式对外发行与生产部署。

这次所有真实服务器写入仅针对本机一次性 GeoServer 和自生成数学 TIFF；现场地质数据未上传 GitHub 或公网服务器。


## 四、最终安全护栏与本机回归

REST 验证 CLI 现要求**同时**满足：明确的 `127.0.0.1:18883/geoserver`、固定的测试目录名称和 sentinel、完全匹配的数学 TIFF SHA256、未使用过的测试输出/工作空间、Windows 本机测试 Java PID 以及该 PID 独占的 18883 回环监听。仅有测试目录或 `server.pid` 文件不足以授权执行 REST。

最终从新工作空间 `gp_mask_lab_r04` 再次执行真实 GeoServer 2.24.4 正常发布：**PASSED**，首次 8 次写入、同计划重跑 0 次新增写入、显式清理 8 个配置对象、无 Layer 残留、无关 `topp:states` 指纹不变，RGBA/NoData 透明度语义仍匹配数学预期。r04 JSON SHA256：
`205182ffd7609990bd09425a73714f0aadb6998e58cd2a580fff6da65d8bf5d6`。

本轮新增测试覆盖 只读连接检查、缺失工作空间、无 GDAL 时连接测试、部分凭据阻断、错误时保留已审核计划、进阶选项收起与展开、帮助文字、错误状态中文操作建议、RGB 与 RGBA 样式语义、严格测试环境拒绝误连与虚拟丢包传输（无网络 MockTransport）。

Windows 本机最终全量：**472 passed，5 skipped**；跳过项均为受限 Windows 文件系统的符号链接权限测试。Ruff、sdist/wheel 构建通过。真实 Windows Qt 窗口的简洁布局与中文截图经过人工查看；界面图片没有真实 GIS 数据与凭据。

**仍保持开发预览定位**：尚无真实 GeoServer 的 TIFF 丢失响应故障注入通过证据，亦未完成干净 Windows 断网机和逐项许可证/NOTICE 审核；在对外发布前必须另行关闭门槛。
