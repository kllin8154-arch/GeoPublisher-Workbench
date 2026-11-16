# P0-C｜离线图幅空间边界只读校验

## 解决的问题

P0-B 已经把 SHP 按**人工选择的图幅编号属性**拆成多份。属性分组正确，并不意味着几何落在正确的地质图幅面内。本阶段允许实施人员使用自己核实过的**标准图幅范围 SHP**，对完整分幅成果执行**真正的多边形几何覆盖检查**。

**检查不是空间裁剪。** 核验使用本机 GDAL/GEOS SQLite `ST_Covers` 对每个要素的完整几何进行判断，几何恰好在边界上也算覆盖。不会只比较外包矩形，也不会删除不符合的要素。

## Windows 桌面使用

1. 在 **发布任务 → 更多工具 → 图幅空间边界核验** 打开工具。
2. 选择之前生成且包含 `sheet-split-manifest.json`、`sheet-split-summary.csv` 的**完整分幅成果目录**。
3. 选择实施人员已确认、坐标系一致、属性表含图幅编号的**标准图幅范围面 SHP**（含 SHX、DBF、PRJ、CPG；无 CPG 时需先确认真实编码）。
4. 在字段下拉框**人工确认**标准图框的字符串编号字段（如 `SHEET_ID`）。候选字段仅用于辅助查找，不会自动作为真实图号。
5. 软件逐图幅检查：是否存在对应且唯一的有效多边形、范围面 CRS 是否匹配、每个要素的实际几何是否被标准图幅面覆盖。核对失败或有越界，明确显示**不通过**和各图幅越界条数。
6. 系统只在本机新目录生成 `boundary-review.json`；如果发现越界，报告仍保存，但状态是 `BLOCKED_OUTSIDE_BOUNDARY`，**绝不将其包装成业务、样式、拓扑、WMS 验收通过**。

## 离线 CLI

需已安装对应 Linux 或 Windows CLI Python 包，并具备本机受控 GDAL、SQLite/GEOS 支持；不需要连接任何服务器：

```bash
geopub sheet-boundary-check /data/split-results \
  --boundary /data/reference/approved-map-sheet-boundaries.shp \
  --field SHEET_ID --gdal-bin /usr/bin
```

可选：使用 `--output /data/audit/new-boundary-review` 将报告保存至**全新**目录。通过时退出码 `0`；有几何越界退出码 `3`；输入不合法、GDAL/GEOS 不满足条件等错误退出码 `2`。

```text
{
  "status": "BLOCKED_OUTSIDE_BOUNDARY",
  "sheet_count": 2,
  "outside_count": 1,
  "geological_completeness_reviewed": false
}
```

审计报告只包含图号、数量、越界统计、源文件组件路径和 SHA-256；不导出图幅的完整 WKT、GeoJSON 或其他几何数据。所有输入保持原样。

## 校验前提及限制

- 标准图幅面必须是真实的 Polygon/MultiPolygon，并且具备有效几何和正面积。每个待验收图号必须**唯一且存在**。无 CRS 或 CRS 与分幅源不同会阻断。
- 图框面本身可以包含更多图幅；只查询当前分幅需要的编号，便于使用更大的离线参考图幅集。
- 对复杂图框，某个面 WKT 超过 250 KB 会要求先在本地整理简化参考数据，避免一次性将巨大几何注入数据库 SQL；程序仅向本机 GDAL 传递只读 SQLite 表达式，**不会发送至业务 PostgreSQL**。
- 如图幅跨越反经线、坐标参考系不等价或存在复杂几何修复需求，必须在离线 GIS 软件中核对后另行验收。程序不会代替地学专家判断地层、图框接缝、符号标注、图层拼接等业务质量。
- 检查日志和目录草稿本身**不能绕过** GeoServer WMS 人工验收、现有专题/比例尺分类审核及生产数据库二次授权。
- 真实地质数据只能保留在经批准的本地离线主机/内网环境，不上传公网、GitHub、插件或 AI 服务；开发测试使用脚本生成的合成几何。

## Linux CLI 离线交付

仓库提供 `scripts/build_linux_cli_offline.sh` 与 `scripts/install_linux_cli_offline.sh`：

- 在**可信 Ubuntu 24.04 x86_64 构建机**上运行，生成包含纯 Python 应用 wheel、依赖 wheelhouse、中文文档、SHA-256 的 tar.gz。构建机可访问公开的 Python 包源，不含业务数据。
- 目标主机**不需要联网**，但需预装 `Python 3.12 + venv/pip + GDAL >=3.7 + ogrinfo/ogr2ogr/gdalinfo/gdalsrsinfo + PROJ`，并具备可运行 GDAL SQLite/GEOS 的能力；安装脚本先验证系统依赖与全部文件哈希，再通过 `pip --no-index --find-links` 安装。
- 此包是 **wheelhouse 离线 CLI 包，不是自带 Python/GDAL 的 Linux 自包含可执行程序**。运行现场程序前仍需对目标 Linux 实际架构、动态库和授权软件进行验收。
