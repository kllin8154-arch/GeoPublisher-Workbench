# 多 TIFF 导入与结构候选的处理

本阶段保留独立 GeoTIFF 发布能力，补上导入来源、明确意图和已知结构标记的检查。仍不提供 ImageMosaic、harvest 或 ImagePyramid 服务发布。所有检测都在本地进行，不初始化索引、不修改源数据、不连接配置文件中的数据源。

## 使用方式

单个独立 TIFF 按原流程预检。导入两个及以上 TIFF 时，默认停止并显示实际 TIFF 数量；确认这些影像应该分别成为独立图层后再继续。

桌面：在选择数据页勾选“将所选 TIFF 分别发布为独立图层”。改变数据选择会取消勾选，必须重新确认。检查页和执行确认框显示本计划的 TIFF 数量及总资源数。

CLI：显式增加 `--raster-mode independent`，例如：

```powershell
uv run --frozen --no-sync geopub plan D:/GIS-Samples/independent --environment data/test-environment.json --output data/runs/independent-001 --raster-mode independent
```

任务输出位置必须在导入目录之外，且必须尚不存在。`independent` 只确认逐文件发布意图；已发现的结构标记仍会阻断，不能用该选项忽略。

## 检测依据与边界

| 本地证据 | 当前处理 |
|---|---|
| `pyramid.properties`，含层级相关键 | 报告 ImagePyramid 结构并阻断 |
| `indexer.properties`，含已知索引键；或根元素为 `Indexer` 的 `indexer.xml` | 报告 ImageMosaic 结构并阻断 |
| 任意名称的 `.properties`，含 `Levels`、`LocationAttribute` 和 `LevelsNum` / `SuggestedSPI` 的组合 | 报告 Mosaic 配置候选并阻断 |
| 保留标记为空、损坏、不可读、超出 64 KiB 或为符号链接 | 要求检查结构，不能降级为普通影像 |
| 单独的 `datastore.properties` | 不据此认定 Mosaic，不读取连接参数值 |
| 无已识别标记的多个 TIFF | 要求用户明确确认是否分别发布 |

文件名大小写、UTF-8 BOM、Java properties 的键转义、分隔符和续行均有回归覆盖。配置内容只用于提取键或 XML 根结构，不写入计划、日志或错误信息；不会访问其中的 URL、路径或数据库。单个标记读取有大小上限。

检查范围包括所选目录的有效子树，以及所选文件或目录的父目录链。显式选择金字塔的层级目录、其中一张影像、Mosaic 的辅助 SHP，也不能绕过已有结构标记。父目录检查只看其自身配置，不扫描无关兄弟目录。导入包含结构数据的总目录会整体停止，不能静默跳过索引后发布剩余影像。

这是一组**已知结构标记检测规则**，不证明数据组有效，也不保证识别所有 Mosaic/Pyramid。尚未初始化的 `gdal_retile` 金字塔可以没有这些配置；未建立索引的拼接数据也可能只有 TIFF。无关或不可读的任意命名属性文件不会单独阻断整个盘符，因此也存在识别边界。相同 CRS、分辨率、相邻范围或数字目录名均不作为确定应拼接的依据；普通业务 SHP 也不会仅因包含 `location` 字段而被判作索引。

设计依据：

- [GeoTools Image Pyramid Plugin](https://docs.geotools.org/stable/userguide/library/coverage/pyramid.html)：根描述文件和层级键。
- [GeoServer ImageMosaic configuration](https://docs.geoserver.org/main/en/user/data/raster/imagemosaic/configuration/)：主配置、索引与数据源配置的不同职责，主配置可以使用不同名称。
- [GeoServer ImagePyramid 构建教程](https://docs.geoserver.org/main/en/user/tutorials/imagepyramid/imagepyramid/)：未初始化的目录结构可以先于属性文件存在。

## 冻结计划与恢复

新计划使用 `schema_version: 2`。`import_intent` 记录明确选择的文件/目录、去重后的数据文件及其相对来源、发布方式、TIFF 数量与总资源数量；全部纳入计划摘要，并与资源列表和原文件摘要核对。

执行前重新检查相同选择项。新增、删除、同数重命名或文件/目录类型变化会使旧计划失效；新增结构标记也会停止执行。程序不会把新文件补进旧任务，不会在恢复时重新选择发布方式。检查在构造 HTTP 客户端之前完成。

旧的 `schema_version: 1` 计划仍可读取和用于已有任务的配置清理。单 TIFF / SHP 任务执行前检查原源文件及其结构上下文；多个 TIFF 的旧计划缺少确认记录，停止发布并要求新建计划。若旧任务已部分发布，先核对原报告和任务记录；不要手工修改版本或重算哈希，不要用新任务自动接管旧任务对象。

配置清理不重新发现输入数据。它继续依赖原计划、任务库所有权和当前服务端指纹，因此源文件后来移动、删除或新增结构标记，不会单独阻止撤下本任务拥有的配置。

## Windows 增量验收

本阶段不改 GDAL 子进程环境、REST 请求、发布写入顺序或配置清理算法。使用现有 Windows 受控运行时，重点复核：

1. 全量 pytest、Ruff、离线锁检查及 sdist/wheel；明确记录符号链接权限跳过，原生 GDAL 用例仍需实际执行。
2. 在中文及空格路径中放两份不同文件名的合成 TIFF；未确认时不生成任务，明确 `independent` 后生成两个资源，恢复数量和方式一致。
3. 原生桌面默认未勾选，后台生成提示实际数量；勾选后正常生成计划，增加/删除输入后确认清除，检查页显示冻结数量。
4. 已知 Pyramid 目录、其子层 TIFF、Mosaic 索引 SHP 及损坏保留标记均被阻断；`independent` 不改变结果。
5. 计划生成后增加 TIFF 或结构标记，执行在网络连接前停止。无需接入生产服务或业务数据库。

以上范围已在 Windows 原生环境完成：`409 passed, 5 skipped`，5 项均为符号链接权限跳过，原生 GDAL 用例实际执行；桌面确认、中文显示与后台两资源计划通过。Linux 和远程 CI 结果分别记录于 [多 TIFF 增量验证](VALIDATION-RASTER-IMPORTS-2026-11-01.md)，Windows 提交、跳过清单与证据摘要见 [报告第 8 节](VALIDATION-WINDOWS-2026-11-01.md#8-pr-2-多-tiff-导入的-windows-增量验证)。
