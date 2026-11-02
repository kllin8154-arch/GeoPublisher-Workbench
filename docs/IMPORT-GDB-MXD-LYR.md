# GDB、MXD、LYR 离线转换与 GeoServer 发布流程

> **当前阶段：内部开发预览。** 仅处理已授权的本地数据；现场地质 GDB/MXD/SHP 不得上传 GitHub、公网 API 或远程服务器。所有生成文件均在用户选择的本地目录中，默认不自动发布。

## 从什么格式开始

| 原始类型 | 数据包含什么 | 转换流程 | 前置条件 |
|---|---|---|---|
| **FileGDB（.gdb 文件夹）** | 可能包含点、线、面要素类、字段和注记 | 工作台“GDB / MXD / LYR 转换” → GDB 批量转 SHP → 对照属性与坐标系 → 发布 | 受控 GDAL 3.13.3（OpenFileGDB） |
| **MXD（.mxd）** | ArcMap 地图工程，引用数据源和渲染/图层顺序；通常没有真实要素几何 | 导出 ArcMap 10.x 辅助脚本 → 在可访问原数据的离线 ArcMap 中产生 SHP、LYR → QGIS 样式迁移 | 需要有效的 MXD **及其被引用的数据源**，ArcMap / ArcPy |
| **LYR（.lyr）** | ArcMap 符号、分级、标注、比例尺等 | QGIS + QGIS Style Assistant 0.4.0 + SLYR → QML/SLD → 工作台 SLD 校验 → WMS 实图检查 | 与插件、SLYR 实际兼容的 QGIS 版本；复杂对象可能需要其他 SLYR 能力或人工修正 |
| **GPKG、SHP、TIFF** | 已经是空间数据文件 | SHP、独立 TIFF 可直接进入 GeoPublisher；GPKG 先通过离线 GDAL 显式转换 | 对来源 CRS、字段和权限作预检 |

**不要把 GDB 或 TIFF 中的所有内容一律变为 SHP**：Shapefile 只适合单一几何类型的矢量数据，栅格应保留 TIFF；包含大量字段、注记或复杂属性结构时，GeoPackage/PostGIS 更适合生产用途。

## GDB 批量转 SHP（推荐先使用这一入口）

1. 打开桌面 **GeoPublisher Workbench** → **发布任务** → **GDB / MXD / LYR 转换**。
2. 点击“GDB 批量转换为 SHP（本地离线）”，选择实际的 `.gdb` 文件夹。程序会先在后台**只读检查**图层数量、要素数量、字段名、注记层、CRS 与风险，不生成 SHP。
3. 在弹出的图层清单中选择需要导出的图层；默认勾选非空的普通矢量图层。默认**不勾选**注记层：注记边界不是文字排版，不应误当完整地质注记。如果明确勾选“也导出注记边界”，才允许选择对应图层。
4. 确认后在本地 D 盘创建全新输出目录，GDAL 逐要素类只读 GDB 并生成 SHP，失败的部分结果放到 `artifacts/`。
5. 检查自动生成的三份报告，再决定是否将成功的 SHP 加入发布任务：

   - `conversion-report.json`：图层状态、原始要素数与导出数、CRS、原字段/目标字段映射、错误代码。
   - **`字段对照.csv`**：可用 Excel 直接查看字段名称变化和省略字段。
   - **`转换说明.txt`**：面向普通使用者的结果概况。

**安全策略**：始终创建全新输出目录；原始 GDB 前后 SHA256 对比。失败图层的部分结果被隔离在 `artifacts/`，不会作为可发布 SHP 导入。没有可靠 CRS、无法读取的数据索引、空层和注记层均按明确结果说明，不静默跳过后声称全量完成。

### 本机 CLI 示例

在 PowerShell、当前仓库目录执行：

```powershell
.\.venv\Scripts\python.exe -m geopublisher convert-gdb "D:\LocalGIS\Sample.gdb" --gdal-bin ".pixi\envs\default\Library\bin" --output "D:\Java-p\GeoPublisher-Workbench-validation\conversion-new" --preview

.\.venv\Scripts\python.exe -m geopublisher convert-gdb "D:\LocalGIS\Sample.gdb" --gdal-bin ".pixi\envs\default\Library\bin" --output "D:\Java-p\GeoPublisher-Workbench-validation\conversion-new"
```

上述示例中的 `Sample.gdb` 是占位路径，不会自动读取实际业务数据。第二条命令只有在输出目录不存在时才执行。

### 字段名与注记的限制

Shapefile DBF 字段名最多 10 个**字节**，长字段、中文字段、重复截断名和 Binary/DateTime 等不支持的字段不能保证原样保留。本工具采用稳定的 10 字符内 ASCII 映射，完整对照存入报告；对不受支持的字段给出省略记录，**不能把“要素数量一致”当作属性无损**。UTF-8 数据字段值仍需要抽样核对，尤其是中文长文本和空值。

一个 GDB 注记层的面几何只是文字区域或边界，不能代表原来的字号、字体、文本位置和标注表达式。需要用 MXD/LYR 和 QGIS Style Assistant 迁移样式，并对 WMS 地图做人工验收。

若 OpenFileGDB 报记录索引错位或行长度异常，不能把 `-skipfailures` 当作正式完整转换。应在**隔离的源库副本**中使用 ArcGIS/其他兼容驱动检查，然后另行确认可恢复要素数；本工具会标记失败并隔离部分产物。

## MXD → SHP + LYR

MXD 中存储的是地图工程关系，真实要素通常在外部 GDB/SHP。**仅有 MXD，没有它引用的数据源，无法凭空恢复 SHP 要素。**

在工作台“GDB / MXD / LYR 转换”里可导出 `arcmap_mxd_to_shp.py`。此脚本只适用于装有 **ArcMap 10.x / ArcPy（Python 2.7）** 且可访问原始数据的离线电脑：

```powershell
python arcmap_mxd_to_shp.py "D:\LocalGIS\project.mxd" "D:\LocalGIS\mxd-export-new"
```

脚本只导出本地矢量图层，断开的数据源、UNC、SDE、远程服务和未明确授权的 Definition Query 子集默认跳过；并保存 `mxd-export-manifest.json`。不会修改/保存原 MXD；导出的 LYR 仍可能指向原数据源，需要用 QGIS 插件将其样式套用到正确 SHP。

### 合成 MXD 验证

**2026-11-02 已在本机 ArcGIS Desktop 10.8 / ArcPy 2.7 环境真实生成二进制 MXD，完成 MXD→3 SHP + 3 LYR 的本地转换。** 旧版说明中“本机未安装 ArcMap”的判断已过时。工作台提供 `arcmap_create_synthetic_mxd.py`，用真实 ArcMap 10.x 的空白 MXD 模板创建数学点/线/面 SHP，并保存有效的 MXD；不会伪造二进制格式：

```powershell
python arcmap_create_synthetic_mxd.py "D:\ArcMap\blank.mxd" "D:\LocalGIS\synthetic-mxd-new"
```

新目录须不存在。源代码的模拟调用测试与 ArcMap 10.8 的真实二进制生成、重新打开并导出 3 个 SHP/LYR 均已通过；**真实地质图 MXD 的复杂符号、标注和最终 GeoServer WMS 视觉一致性仍未验收**。

## LYR → SLD（复用 QGIS Style Assistant 0.4.0）

1. 在 QGIS 中安装用户已有的 QGIS Style Assistant 0.4.0，并按其提示安装 SLYR（Community Edition 或具备相应解析许可的版本）。
2. 先加载正确的数据图层，选择 `.lyr`（或其支持的 `.mxd`）应用原始分类符号，再导出 `.qml` 与 `.sld`。QML 作为 QGIS 复核依据，SLD 供 GeoServer 使用。
3. 在工作台 **展开高级选项 → 批量 SLD 目录** 选择输出文件夹。**仅允许同名精确匹配**：`fault.shp` 必须配 `fault.sld`。缺少、大小写冲突或异常大的 SLD 会在本地预检阶段阻断。
4. 如果 SLD 仍引用 GDB 原字段而 SHP 已缩短字段名，可先在转换目录检查 `字段对照.csv`，再在工作台高级选项显式勾选 **“我已核对字段对照：修复 SLD 中的原字段名”**。程序会要求 SHP 同目录存在原转换器生成的 `conversion-report.json`，核对 SHP、DBF、SHX、PRJ、CPG 每个文件的 SHA256 与实际字段目录后，**仅生成新任务的 SLD 副本**，支持简单的 `ogc:PropertyName` 和 `fes:ValueReference` 精确原字段替换。不会修改原始 SLD、QML、GDB、SHP。
5. 表达式型字段引用（如 XPath、函数）、缺失字段、旧报告、重复字段或转换不成功的图层**会阻断**，不会猜测修复。用户未勾选时，系统沿用原 SLD，缺少目标 DBF 字段会按既有预检阻断。不能把启用字段对照视作 ArcGIS 图例完全兼容。
6. 发布后必须检查实际 WMS 地图、图例、分类颜色、线型、标注与比例尺。QML/SLD 文件成功生成不代表 GeoServer 视觉上完全等价。

参考：GDAL [OpenFileGDB](https://gdal.org/en/stable/drivers/vector/openfilegdb.html)、[Shapefile](https://gdal.org/en/stable/drivers/vector/shapefile.html)；SLYR [GeoServer 样式限制](https://slyr.north-road.com/user_guide/limitations)。

## 当前验收边界

- 已用 GDAL 3.13.3 对**用户指定的本机测试 FileGDB**进行实际元数据读取和批量 SHP 导出；全部数据及字段对照只留在 D 盘，不提交仓库，也不上传公网。
- 已有真实 SHP 成功产物；个别源数据 OpenFileGDB 索引读取异常被明确记录，没有采用忽略错误继续转换的策略。
- 本机 ArcMap 10.8 + ArcPy 2.7、QGIS 4.2.1 + SLYR Community + Style Assistant 0.4.0 已通过**真实合成 MXD→SHP/LYR→QML/SLD 的离线转换**：3 个几何图层全部成功，导出 SLD 均通过 GeoPublisher 的字段、外部资源和符号类型校验。中文及空格路径成功，18 个源文件转换前后 SHA256 一致。此验证**不是**复杂地质样式视觉保真或 GeoServer 服务端 WMS 发布验收。
- 生成的 SHP 尚未自动发布到生产 GeoServer；现有 GeoPublisher 的正式发布功能仍需由授权人员按环境确认后执行。


## QGIS LYR→SLD 批量离线脚本（2026-11-02 新增）

在工作台 **发布任务 → GDB / MXD / LYR 转换 → 导出 QGIS LYR→SLD 批量离线转换脚本**，将 `qgis_lyr_to_sld.py` 保存到本机 D 盘。该操作只导出脚本，不安装插件、不运行 QGIS、不联网或发布服务。

若已经用 ArcMap 辅助脚本获得一个包含 `同名.shp`、`同名.lyr`、`.shx`、`.dbf`、`.prj`、`.cpg` 的文件夹，就能调用已安装的 QGIS Python 运行时批量处理。下面是**本机已验证的 QGIS 安装路径示例**，换机时以该机实际安装位置为准：

```powershell
& "D:\QGIS\bin\python-qgis.bat" "D:\LocalGIS\qgis_lyr_to_sld.py" "D:\LocalGIS\mxd-export-new" "D:\LocalGIS\sld-export-new"
```

需要安装 QGIS Style Assistant 和 SLYR Community。脚本默认读取当前用户、当前 QGIS 主版本的默认 profile 插件目录，必要时可显式传入 `--plugin-parent "插件父目录"`；QGIS 安装无法识别时可传入 `--qgis-prefix "QGIS 应用目录"`。工具**不会从 PATH 隐式调用其他 Python 或 ArcPy**。

脚本逐个读取同名 SHP/LYR、应用原 ArcMap LYR 样式、输出 QML 与 SLD，并创建 `lyr-conversion-report.json`。失败的中间文件留在新输出目录 `artifacts/` 中，不作为成功 SLD。返回值：0 代表本次全部导出；1 代表部分图层失败；2 代表来源、环境或路径前置条件被阻断。源文件不写入，输出目录须全新、且不能位于输入目录内部。

**继续发布：** 在工作台添加刚才的 SHP 文件，展开高级选项，在“批量 SLD 目录”中选择 `sld-export-new`，再次执行本地预检。成功导出 QML/SLD、字段及几何类型检查通过后，仍需人工确认分类色值、线型、标注和最终 GeoServer WMS 效果；绝不自动将“转换成功”判定为“视觉无损”。
