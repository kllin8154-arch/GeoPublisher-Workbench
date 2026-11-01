# WMS 只读真实服务回退验收（2026-11-01）

本次将发布引擎中已存在的 WMS 五窗口保守判空流程，抽出为共享的 `review_wms_views()`，使**同一逻辑**既可由正式发布中的只读验收调用，也可在不发布数据的情况下独立测试。它对服务端只调用 `get_map()`，不创建 Store/Layer/Style，不执行 SQL，也不访问任何用户项目资料。发布场景可提供本地预览保存回调，在首图验证成功后立即落盘；即使后续局部复核被安全取消，仍保留首图证据。独立只读脚本不传保存回调。

## 测试边界

- 环境：本机 Windows 10，GeoServer **2.24.4** 已有测试发行版的**独立副本**；数据目录隔离在 `D:\Java-p\GeoPublisher-Workbench-validation\geoserver-disposable-wms-20261101\data_dir`。
- 绑定：只监听 `127.0.0.1:18881`，无代理、重定向或外网通信。
- 目标图层：GeoServer 自带公开演示 `tiger:poi`；不是用户地质数据或上传的业务数据。
- HTTP 请求：仅 WMS 1.1.1 / EPSG:4326 / 512×512 PNG **GET**；不会携带用户名/密码。
- 比例尺样式：使用不持久化的内联 `SLD_BODY`（`MaxScaleDenominator=500000`），作为本次 GET 的临时样式。**不包含 `layers/styles` 请求参数**，由 SLD `NamedLayer` 自行定义图层；这样避免 GeoServer 将未命名 UserStyle 当作样式库却继续使用默认样式。
- 所有验证结果为演示数据实测，不代表未来任何项目图层或任意 CRS 的地图语义正确。

官方依据：[GeoServer Working with SLD](https://docs.geoserver.org/main/en/user/styling/sld/working/) 关于 SLD_BODY 及自包含 SLD 与 Library Mode 的区别。

## 真实验收结果

| 用例 | 真实 WMS 请求数 | 预期 | 结果 |
|---|---:|---|---|
| 小范围演示点图层 | 1 | 立即显示有效地图 | `SUCCEEDED`，2662 个可见像素 |
| 自包含比例尺 SLD、宽范围 | 2 | 首图全透明，局部窗口可见 | `REVIEW_REQUIRED → PASSED`，选择索引 1，142 个可见像素 |
| 完全错误范围 0,0,1,1 | 5 | 主图与四个局部均空白 | `REVIEW_REQUIRED`，0 个可见像素 |

比例尺视图选择 BBOX：`[-74.618,40.185,-73.982,40.715]`；有效局部 PNG SHA256：`d5ac2172d8e69bfa29facdd12396989c190ed95c4cca64a3890223d2907a8eb0`。

全部空白 PNG SHA256：`c1fdd3b26f4fd85e47293752b61244aa0494349cd0676fc7af30fff6a9a84896`。

**保留的失败记录：** 首次试验在 WMS GET 同时传入 `LAYERS` 与未命名 `UserStyle`，使默认样式继续生效，宽范围并未按预期变为空白（`selected_sample=0`）；这项试验不能算通过。改为 SLD 自包含模式后，重复从头验证全部三类请求通过。原始两份运行 JSON 分别留存在独立测试目录，不进入 Git。

## 验收复现

1. 先在 Windows 本机启动隔离的、只监听回环地址的 GeoServer 2.24.4，确认存在默认演示图层 `tiger:poi`；
2. 在源码仓库目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\validate_wms_readonly.py --url http://127.0.0.1:18881/geoserver --output D:\Java-p\GeoPublisher-Workbench-validation\wms-readonly-new-run.json
```

`--output` 必须为尚不存在的新路径。脚本拒绝非回环地址、URL 中嵌入凭据、查询参数和不匹配的服务根路径；只请求 `tiger:poi`。所有真实响应仅输出**摘要、可见像素、样本状态和范围**，不把 GeoServer 的图片或服务端敏感错误正文提交 Git。

3. 完成后仅根据该独立测试实例 PID 停止 Java 进程，并确认端口不再监听。本次已经确认 **18881 关闭**。

## 与 PRD Gate D 的关系

本次是真实 GeoServer 对**多窗口只读检查链路**的验收；此前只有正常/空白 GetMap 读图证据，未实际验证首次空白后选中正确局部 PNG。这项差距现在关闭。

仍然**不能**据此宣称完整 Gate D 通过：

- 未完成发布图层真实数据位置、SLD 与实际业务内容语义一致性对照；
- 未在真实 GeoServer 发布人工生成的 NoData/Alpha GeoTIFF 并验证有效数据窗口；
- 合法全透明、合法纯色与比例尺不显示仍需人工确认路径；
- 未进行真实五数据发布/补偿及缓存一致性复验。

继续保持当前桌面便携程序为已经验收的开发预览版；新发布逻辑只在经过更完整的 GeoServer 集成和离线包回归后更新。
