# GeoServer 2.24.4 数学合成 NoData/Alpha WMS 实测

> 日期：2026-11-01
> 类型：Gate D #7 的真实栅格 WMS 渲染子项
> 当前结论：**这项子测试通过，完整 Gate D 尚未关闭。**
> 测试范围：仅限一次性本机 GeoServer 和纯数学合成 GeoTIFF；没有使用或上传真实地质数据

## 环境、隔离与实际操作

- Windows 10，本机受控 GDAL **3.13.3**；GeoServer **2.24.4** 使用本机测试发行版的独立复制目录。
- 原始数据由 `scripts/validate_raster_mask_local.py` 数学生成，先用 GDAL 检查后才复制两份独立 TIFF。源目录后来被其他统计操作创建的 `.aux.xml` 没有复制进本测试；本测试输入只包含原始 TIFF 文件字节。
- 准备：`scripts/prepare_disposable_raster_catalog.py` 在**停止状态**复制测试 GeoServer 的配置与文件，仅在复制出来的 data_dir 中构建两个新的 GeoTIFF CoverageStore、Coverage 与 Layer。**不使用正式发布器 REST 上传，因此这不是 REST 幂等/清理验收。**
- 已停止的测试数据目录：`D:\Java-p\GeoPublisher-Workbench-validation\geoserver-disposable-raster-20261101`；原始发行目录不修改。启动前以 sentinel、输入来源标识和 SHA256 限制数据。
- GeoServer 仅监听 `127.0.0.1:18882`。验证脚本仅向该端点执行 WMS GetCapabilities/GetMap **GET**，禁用系统代理和 HTTP 重定向，不传任何账号/密码，不调用 REST POST/PUT/DELETE。
- 测试结束已根据记录的独立 Java PID 停止实例，并确认端口 **18882 无监听**。保留测试目录与仅含合成数据的审计 JSON，不放入 Git。

## 原始数学 TIFF

| 资源 | GeoServer 图层 | 预期行为 | SHA256 |
|---|---|---|---|
| 单波段，NoData=0 | `nurc:synthetic_nodata` | 中央 64×64 有效，其余无数据 | `40de4b4f6559e6f23ce0375732e32216e8e2ffcbbd4eb87883e1c3042236104e` |
| RGBA（Alpha 0、96、255） | `nurc:synthetic_alpha` | 中央有效区域，内侧不透明，边缘局部半透明 | `68d29780ca2f83e975069baf6d499c4f372a12968a3e0a12d0008a223e9019ca` |

两份 TIFF 都带 EPSG:4326，完整地理范围 `[110,29,111,30]`，数学已知有效区域 `[110.25,29.25,110.75,29.75]`。这不是从任意生产影像自动识别出的窗口。

## 真实 WMS 1.1.1 / EPSG:4326

请求图片均为 512×512、`styles=`、`transparent=true`、`image/png`。GeoServer 实际加载两个 CoverageStore、Coverage 和 Layer。工作空间限定的 Capabilities 返回的是**未加 `nurc:` 前缀的图层名**；脚本已改为解析 XML `Layer/Name`，两图层都正确列出，不能简单用全限定名称字符串搜索造成误报。

| 请求 BBOX | NoData 灰度影像 | RGBA 影像 |
|---|---|---|
| `110,29,111,30` 整体 | `PASSED`，65,536 可见像素 | `PASSED`，65,536 可见像素 |
| `110.25,29.25,110.75,29.75` 已知有效窗口 | `PASSED`，262,144 可见像素 | `PASSED`，262,144 可见像素 |
| `110,29.75,110.25,30` 无效角落 | `REVIEW_REQUIRED`，0 可见像素 | `REVIEW_REQUIRED`，0 可见像素 |
| `0,0,1,1` 错误地理范围 | `REVIEW_REQUIRED`，0 可见像素 | `REVIEW_REQUIRED`，0 可见像素 |

程序将完整图像的每个像素 Alpha 与数学已知遮罩比较，**所有位置一致（无错配）**，不是仅凭文件大小或 PNG 头判断。实际 Alpha 直方图：

| Alpha | NoData 图像像素数 | RGBA 图像像素数 |
|---|---:|---:|
| 0（全透明） | 196,608 | 196,608 |
| 96（半透明） | 0 | 15,360 |
| 255（不透明） | 65,536 | 50,176 |

完整图像 SHA256：
- NoData WMS PNG：`61d0fca8a851ecd46c5e992a33c2904af3884f45d1a69ad1491d4ad8210a8786`
- RGBA WMS PNG：`3d4e76a78c2d0de90a0f360cd139034d6a462117f9cced182643fa45950ad2d9`

## 与真实发布引擎共用 WMS 核验

新只读测试适配器通过和正式发布引擎完全相同的 `review_wms_views()` 再次调用 GeoServer：
- 两图层有效全范围：**SUCCEEDED，一次 GetMap**；
- 两图层完全错误范围：**REVIEW_REQUIRED，五次 GetMap**；
- 总计测试请求 **21 次**（1 次 Capabilities + 8 次独立 WMS 示例 + 12 次引擎 WMS 检查），全为 GET，未发送任何服务器写请求。

严格验收状态：`PASSED`，失败项：`[]`。本机原始审计：
`D:\Java-p\GeoPublisher-Workbench-validation\geoserver-disposable-raster-20261101\raster-wms-engine-and-mask-strict.json`

审计报告 SHA256：`BA4AECFCA3CBA1AFED0668D8E9CD41CBE57C1F9D534A95FAC03F19BC1CBB451B`

## 开发脚本与复现

1. 运行 `scripts/validate_raster_mask_local.py`，生成新的纯数学 TIFF（不能覆写已存在目录）。
2. 在停止的本机测试服务器复制目录中运行 `scripts/prepare_disposable_raster_catalog.py`，输出新的以 `geoserver-disposable-raster-` 开头的独立目录。默认不会复用已有 data_dir。
3. 为此实例选择未占用的本机回环端口并启动 GeoServer；确认端口只绑定 `127.0.0.1` 且属于该独立 Java 进程。
4. 在源码环境运行以下脚本（本次端口为 18882）：

```powershell
Set-Location D:\Java-p\GeoPublisher-Workbench
.\.venv\Scripts\python.exe scripts\validate_disposable_raster_wms.py --url http://127.0.0.1:18882/geoserver --catalog D:\Java-p\GeoPublisher-Workbench-validation\geoserver-disposable-raster-20261101 --output D:\Java-p\GeoPublisher-Workbench-validation\geoserver-disposable-raster-20261101\new-acceptance.json
```

5. 完成后按 test-only PID 停止 GeoServer，确认端口不再监听；不删除其他数据目录。

## 未完成的生产门槛

- 本测试采用**停止状态下准备独立 data_dir** 的专门验收手段，不是普通用户发布路径；不能据此证明程序的 REST 上传、同名冲突、失败补偿、准确配置清理。
- GeoServer 全透明／合法单色场景的人工确认留痕、复杂 SLD、覆盖比例尺及地理内容一致性仍需独立验证；非空 PNG 本身依旧不能证明真实业务图正确。
- **Gate D #7 仍开放**，下一项应在一次性 GeoServer 中通过正式程序 REST 发布这两份数学 TIFF，并检验计划/日志/重跑/精确清理；或在 PRD 明确把该子项单独验收并保留完整真实发布门槛。
- 当前 Windows 桌面便携版 `Current` 不因本次试验自动替换；签名、第三方许可证/NOTICE、干净断网机器验收仍按 #8 单独处理。
