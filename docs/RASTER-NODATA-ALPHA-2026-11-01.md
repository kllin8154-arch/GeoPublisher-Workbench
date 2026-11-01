# 纯离线合成 GeoTIFF：NoData / Alpha 验证（2026-11-01）

## 目标与严格边界

作为 [Gate D 合成 GeoTIFF 的真实 WMS 验收任务](https://github.com/kllin8154-arch/GeoPublisher-Workbench/issues/7) 的**前置本地验证**。本阶段仅证明：锁定 GDAL 3.13.3 可生成并识别含 NoData、显式 Alpha 通道的独立 GeoTIFF；本地 PNG 验证器可以区分可见数据与透明像素。

**不声称：** GeoServer 已发布这两份 GeoTIFF、NoData 在真实 WMS 中呈现正确、能够自动从任意影像推断有效数据窗口、或 Gate D 整体验收通过。

## 数据生成方式

- 使用 `scripts/validate_raster_mask_local.py` 在全新输出目录生成两张 **128×128 数学图像**，不读取外部影像、Shapefile、项目资料或地图切片。
- 灰度像素：透明/无效区域值为 0，中央有效矩形 `[32,32)-[96,96)` 使用稳定非零梯度；GDAL 转换时明确设置 `-a_nodata 0`。
- RGBA：中央有效矩形为真实颜色，边缘含 alpha=96，中心 alpha=255；外部 alpha=0，但保留隐藏 RGB 数据，用于检测透明像素误判。
- 通过固定 GDAL 3.13.3 转换为 `nodata-gray.tif` 和 `alpha-rgba.tif`，声明 EPSG:4326，完整 GeoTIFF 范围 `[110,29,111,30]`。
- 有效窗口经**数学构造已知**：`[110.25,29.25,110.75,29.75]`。这只是合成数据的期望位置，**不是产品已实现对任意 TIFF 的自动有效窗口识别**。

原始合成文件、生成的 TIFF 和本地预览 PNG 全部保留在：
`D:\Java-p\GeoPublisher-Workbench-validation\nodata-alpha-local-20261101`

## 原生 GDAL 3.13.3 实测

| 指标 | 本机结果 |
|---|---|
| 灰度 GeoTIFF 波段 | 1 |
| NoData 元数据 | 0 |
| RGBA GeoTIFF 波段 | 4 |
| Alpha 通道索引 | 4 |
| Alpha ColorInterpretation | `Alpha` |
| CRS | `EPSG:4326` |
| GeoTIFF 全范围 | `[110,29,111,30]` |
| 本地透明 PNG 有效像素 | 65,536 |
| 本地 PNG 全透明像素比例 | 75% |
| 网络与服务端请求 | 无（0） |
| 当前结论 | `LOCAL_GDAL_VALIDATION_ONLY` |

灰度 TIFF SHA256：`40de4b4f6559e6f23ce0375732e32216e8e2ffcbbd4eb87883e1c3042236104e`。
RGBA TIFF SHA256：`68d29780ca2f83e975069baf6d499c4f372a12968a3e0a12d0008a223e9019ca`。

本地 PNG SHA256：`d41573743243fadfdc695b06b57afa01d43e71cb834b9f54ad66f8982790b36d`，**并非 GeoServer WMS 的 PNG**。

## 复跑（只使用本地工具）

```powershell
Set-Location D:\Java-p\GeoPublisher-Workbench
.\.venv\Scripts\python.exe scripts\validate_raster_mask_local.py --gdal-bin .pixi\envs\default\Library\bin --output D:\Java-p\GeoPublisher-Workbench-validation\nodata-alpha-new-run
```

输出目录必须不存在；脚本拒绝覆盖和符号链接输出路径。新增的 `tests/test_raster_mask_local.py` 对数学像素与 NoData/Alpha PNG 分类进行无需 GDAL 的测试，并在已安装受控 GDAL 的 Windows/Linux 环境执行原生集成测试；CI 没有受控运行时会明确 skip。

## 下一步才可关闭 Gate D

1. 在独立、回环绑定、一次性的 GeoServer 2.24.4 中发布**上述新生成的数据**，不使用真实地质影像；
2. 根据原始有效区域的已知边界，验证 WMS 有效像素位置、NoData/Alpha、带透明背景/纯色合法例外、错误 BBOX 和局部窗口的正反例；
3. 固定服务端图层/样式、实际请求 BBOX、图片 SHA256、像素统计、人工判定及清理记录；
4. 确认没有盲目重试、没有误删其他层、清理后关闭独立 GeoServer。

完成前不得将本轮本地测试的成功扩大为已通过真实 WMS 栅格验收。
