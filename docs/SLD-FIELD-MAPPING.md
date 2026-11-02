# GDB → SHP 后的 SLD 字段修复（显式确认、纯离线）

此功能只能修复已由 QGIS 插件导出的 SLD 中的**简单字段引用**，并不解析 LYR/MXD 二进制，也不保证 ArcMap 的符号、标注、比例尺与 GeoServer 完全一致。

## 用户操作

1. 先用工作台完成 GDB → SHP，检查转换目录里的 `字段对照.csv` 和 `conversion-report.json`；失败图层、注记层需要单独处理。
2. 在安装 QGIS Style Assistant 0.4.0/适配 SLYR 的离线电脑上，将 LYR 样式应用到对应 SHP，导出同名 SLD。当前电脑尚未完成真实 LYR 转换验收。
3. 在 GeoPublisher 中添加转换后的 SHP，并在高级选项选择单个 SLD 或批量 SLD 目录。
4. 如果 SLD 使用原 GDB 长字段名或中文字段名，先人工核对 CSV，再**勾选“我已核对字段对照：修复 SLD 中的原字段名”**。默认不勾选。
5. 点“下一步：检查数据”，工具根据当前 SHP 同目录的转换报告检查文件哈希和字段目录，仅在新任务目录创建修改后的 `style.sld`，保留原始 SLD。
6. 检查页会列出原字段名与新的 SHP 字段名；发布后仍需核对真实 WMS 颜色、分类、标注与地理位置。

## 安全规则

- 只接受转换器生成的 `conversion-report.json`：版本 1、操作 `GDB_TO_SHP_LOCAL_ONLY`、输入未更改、GDAL 3.13.3。
- 分别核对 SHP/SHX/DBF/PRJ/CPG 的 SHA256，并确认报告中的目标字段与实际 SHP 字段一一对应。
- 仅修复 `ogc:PropertyName` / `fes:ValueReference` 的简单纯文本叶子字段引用。XPath、复杂表达式、重复字段或未识别字段直接阻断，不猜测。
- 使用 defusedxml 拒绝 DTD、XML 外部实体；继续执行现有 SLD 预检。
- 不修改原 GDB、SHP、QML 或 SLD，也不访问网络或 GeoServer；修改写入全新的计划样式副本。
- 新计划包含 `sld_field_rewrite` 留痕：原 SLD/新 SLD/转换报告/SHP 的 SHA256、修改过的字段列表及需人工复核标记。

哈希比对只能证明文件与本地报告一致，**不能代替人工确认数据来源与地图语义**。

## CLI 示例

仅示意命令，目录需换成经过授权的 D 盘本地环境，具体 flags 请用 `geopub plan --help` 核对：

```powershell
.\.venv\Scripts\python.exe -m geopublisher plan --environment D:\LocalGIS\test-env.json --output D:\LocalGIS\tasks\new-task --sld-directory D:\LocalGIS\style-sld --remap-sld-fields D:\LocalGIS\shp-conversion\sample.shp
```

## 验证

合成测试覆盖成功替换、报告/DBF 不一致、SHP 哈希改变、字段冲突、XPath/函数阻断、非法 XML、源 SLD 不覆盖、用户未授权默认不转换等。已用本机授权测试 GDB 转换出的真实 SHP 和**数学生成的 SLD**完成离线 GDAL 发布计划预检，冻结样式匹配成功、源 SHP/SLD 不变、0 次服务器和网络访问。

真实 MXD/LYR 样式兼容与 GeoServer WMS 视觉比对仍需独立离线测试，见 [Issue #14](https://github.com/kllin8154-arch/GeoPublisher-Workbench/issues/14)。
