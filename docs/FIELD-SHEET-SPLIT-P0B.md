# 离线图幅属性分幅｜P0-B 第二阶段

适用场景：一个 SHP 图层内有多个真实图幅编号，希望将每个编号的要素导出成独立 SHP，再通过既有工作台逐图层发布 GeoServer、按来源组织业务数据库目录。

**严格区分：这是按属性值分组，不是按照图幅范围面进行裁剪。不能把属性分组直接当作地质图幅完整性验收。**

## 桌面操作（离线 Windows）

1. 进入 **发布任务 → 按图幅字段拆分 SHP**；选择单个 SHP，必须带 SHX、DBF、PRJ 等必要组件。建议带有 CPG；如无 CPG，先核对 DBF 实际编码，再到 **高级设置 → SHP 文字编码** 显式选 UTF-8 或 GBK。不会给原文件补写 CPG。
2. 软件只读列出字符串/整数属性字段。人工查看原数据属性表，明确选择实际图幅编号字段（如 `SHEET_ID`、`TFBH`）。字段名仅是候选，软件绝不自动批准。
3. 软件只读枚举所有不重复图号、每个编号的要素数量、总数和源范围，自动检查空值和丢失计数。确认弹框中核对各图幅统计后才允许导出。
4. 导出到 **任务目录下新建的独立文件夹**，成功要素全部打包为不同 SHP；每份单独核实要素数、完整字段名、EPSG、空间范围与图幅编号；所有结果范围的并集与源数据范围一致，源 SHA-256 前后不变。
5. 成功后自动核验 `sheet-split-manifest.json`、`sheet-split-summary.csv` 核对表及文件组件的 SHA-256，合格分幅 SHP 加入当前任务的选择列表；点击 **下一步：检查图幅 → 确认并发布** 才可能访问 GeoServer。完整分幅报告可通过 **打开本地转换报告** 查阅。
6. 发布后按 P0-B 第一阶段的 **核对图幅关系** 整理真实图幅编号，多个图层属于同一个真实图幅时可按同一编号归组。之后人工核实 WMS 和业务目录，再单独批准 PostgreSQL 登记。

## CLI（Linux/Windows 离线服务器）

只读预览（不建目录）：

```powershell
geopub sheet-split "D:\input\map.shp" --field SHEET_ID --gdal-bin "D:\GeoPublisher\Current\runtime\Library\bin" --preview
```

预览会输出 `review_sha256`、图幅编号与数量。核对确认后：

```powershell
geopub sheet-split "D:\input\map.shp" --field SHEET_ID --gdal-bin "D:\GeoPublisher\Current\runtime\Library\bin" --output "D:\work\sheet-split-new" --review-sha256 "<从只读预览获得的完整摘要>"
```

Windows 安装包没有独立的 `geopub.exe` CLI；以上命令适用于安装了 geopublisher 包的离线 Python 环境。普通 Windows 实施人员只需用桌面界面。对 Linux 路径使用其本机的 GDAL bin 和输出路径，不运行 Windows EXE。

## 有效性与失败处理

- 只支持 SHP，图幅字段类型为 **String / Integer / Integer64**。最多 **100 个不同图幅编号**；无效编号/空值/编号大小写冲突/超出数量上限一律阻断，绝不静默跳过；整数编号可能丢失前导 0，标准图号建议用字符串字段保存。
- 不接受网络共享、符号链接、覆盖旧目录、把结果输出到源目录内。GDAL 固定私有进程环境，禁止 PROJ 在线访问。
- 数据在预览后被修改，SHA-256 不匹配即阻断。转换失败或取消会保留 `_partial` 隔离文件，但**不生成 COMPLETE_VERIFIED 报告，不加入发布列表**；实施人员核对原因后重新选择全新目录，不覆盖原结果。
- 数据来自受转换报告核验的 GDB/MXD 时，拆分完成后继续保留原 GDB/MXD 所属父目录标识；普通来源的 SHP 保留独立源关系，仍须人工审核专题、比例尺与数据库父节点。
- `source_count = sum(child_count)`、输出中每份图层只有一个对应的图幅属性值、子集范围均在源范围内、所有子集范围的并集等于原范围。**这不是复杂拓扑或面积无缝性的充分证明**；裁剪、图框与地质图件完整性仍需独立核验。
- 发布 GeoServer 和登记 PostgreSQL 均沿用原工作台的明确确认、只读预检、WMS 实图验收、审计和未知结果禁止自动重试规则。当前分幅 CLI/GUI 不接触生产数据库。

## 目录结构

```text
原文件：湖南基础地质.gdb（只读）
└── 导出图层 SHP：断层点.shp（来自可信转换报告）
    └── 本地新建 split 目录
        ├── 断层点__H49E001006_....shp/.shx/.dbf/.prj/.cpg
        ├── 断层点__H49E001007_....shp/.shx/.dbf/.prj/.cpg
        └── sheet-split-manifest.json（完整核验后才存在）

本机目录草稿（全部待人工业务审核）：
待分类 → 湖南基础地质.gdb → 图幅 H49E001006 → 断层点图层
                                → 图幅 H49E001007 → 断层点图层
```

执行环境：Windows GDAL 3.13.3 及 PySide6；支持合成地质图框/点线面测试。真实现场数据只在经许可离线主机或服务器处理，禁止上传公网。
