# 0.1.0 开发预览验证记录

本记录区分本地实测、模拟故障测试和尚待现场验收的部分。所有 GIS 数据均由数学规则生成，没有使用真实项目数据或业务数据库。

## 本地基线

| 项目 | 实测环境 |
|---|---|
| 操作系统 | Linux x86-64 |
| Python | 3.12.14 |
| GDAL | conda-forge 3.13.3，使用仓库 Pixi 锁文件 |
| GeoServer | 官方 2.24.4 二进制分发，独立临时目录，回环地址 |
| Java | 17 |
| 桌面 | PySide6 Essentials 6.12.0，offscreen |

选择 2.24.4 是为了验证该兼容基线，不表示推荐将旧版本直接用于新建生产环境。其他 GeoServer 版本仍需独立验收。

## 自动测试与构建

```text
ruff check .                       All checks passed
QT_QPA_PLATFORM=offscreen pytest -q 314 passed
python -m build --no-isolation      sdist + wheel built successfully
```

测试覆盖 REST 认证和错误状态、写结果未知、对象所有权、SLD 正文变化、外部修改拒绝清理、SQLite 跨进程锁与崩溃恢复、CRS/编码/组件、透明图像、XML 异常、目录 SQL、桌面计划失效和后台执行约束。

输入边界测试包含非对象 JSON、错误类型、UUID、计划摘要、符号链接、已有任务目录和目录创建竞争，以及 SLD 体积变化后的有界读取。

## 真实 GeoServer 集成

脚本：`scripts/integration_check.py`。使用 5 个合成数据集：UTF-8 点、线、面 SHP，GBK 面 SHP，以及 RGB 渐变 GeoTIFF。

| 验证项 | 结果 |
|---|---|
| 预检、打包、独立上传和图层/样式绑定 | 5 / 5 成功 |
| WMS PNG 真实验收 | 5 / 5 成功 |
| 矢量 WFS 抽样 | 4 / 4 成功；栅格不请求 WFS |
| 首次任务写操作记录 | 20 条 |
| 同任务再次执行新增写操作 | 0 条 |
| 配置清理 | `ROLLED_BACK`，移除 20 个本任务配置对象 |
| 清理后无关示例图层 | 配置快照保持不变 |
| 服务器完成上传后人为丢弃响应 | `MANUAL_ACTION_REQUIRED` |
| 对未知上传任务再次执行 | 仍要求核对，新增写请求 0 次 |

清理调用不递归、不要求 GeoServer 清除实际数据。测试服务内的合成源数据文件仍保留；清理服务配置和回收磁盘空间是两个不同操作。

集成验证中曾发现 GeoServer 在首次 WMS/WFS 后补充默认 `advertised` 属性，导致过严的快照比较误报外部修改。当前比较会规范化已知默认值；实际用户更改和 SLD 正文变更仍会阻止自动恢复/清理。

## SQL 与桌面证据

目录模块完成 34 项单元测试；另外使用临时 pglast 8.7 对 18 份创建/复用、中文、引号、换行和美元定界符冲突脚本进行 SQL 与 PL/pgSQL 语法解析。全部通过。没有连接数据库，因此这不构成真实 schema 或触发器行为的验收。

`docs/images/desktop-preview.png` 是实际 Qt 界面截图，展示真实任务结果及 GeoServer 返回的地图，并显示目录尚未登记。截图没有用静态假结果冒充执行成功。

## Windows 与发行物

已配置 `windows-latest` 与 `ubuntu-24.04` CI，以及 Windows PyInstaller 桌面预览构建。具体远程结果以对应 GitHub Actions 运行记录为准。

本地上述结果来自 Linux；Windows 10/11 原生桌面、目标机器中文字体、完整 GDAL DLL 布局和内网离线启动尚待实机验收。PyInstaller 预览不包含 GDAL，因此不能当作完整可离线分发的安装包。

## 尚未完成的验收

- 现场反向代理、证书链、账号权限及目标 GeoServer 版本。
- 真实业务目录字段、默认值、唯一键、外键、触发器与页面可见性。
- 1 GB TIFF、长时间上传、磁盘不足等容量与性能基准。
- 全量无效几何、空间拓扑、完整 SLD XSD 和复杂样式视觉等价。
- Mosaic/Pyramid、已有数据原位更新、缓存一致性和任意历史版本恢复。

这些边界在产品状态和路线中保留，当前成功结果只代表上面列出的样本与故障场景。
