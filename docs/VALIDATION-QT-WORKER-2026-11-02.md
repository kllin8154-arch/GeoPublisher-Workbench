# Windows Qt 后台线程事件循环与测试等待修复（2026-11-02）

## 目标

定位并修复 GeoPublisher Workbench 全量 pytest 偶发在 PySide6 QThread 测试处无期限等待的问题；不修改 GIS 处理、GeoServer REST、用户数据或任何凭据。所有诊断保存在 D 盘验证目录。

## 复现证据和修复过程

1. **最初无压力测试**：原版全量测试连续两次 `512 passed, 5 skipped`，耗时 32.56s 和 26.45s，未暴露线程竞态。
2. **加入实际 Qt 压力测试**：16 次快速成功 worker 加 6 次失败 worker。独立执行桌面测试 45 passed，但完整测试曾在“快速成功任务”循环中等待超过 20 秒。faulthandler 显示主线程在 `test_desktop.py:wait_until`，还有 Qt 工作线程未结束。
3. **排队启动工作任务**：将 `QThread.started -> _Worker.run` 改成明确 `Qt.QueuedConnection`，确保 `_Worker.run` 由工作线程事件循环调用；成功任务另增加 `QThread.currentThread().loopLevel() >= 1` 回归断言。初期三次全量回归均为 `514 passed, 5 skipped`（23.20s、21.46s、24.52s）。
4. **新增断言后的再次复现**：某次全量测试卡在**快速失败任务**循环，faulthandler 再次确认主线程正在 `wait_until` 内等待，另有 Qt 线程；说明只排队启动还**不够**。
5. **确保退出不会等 GUI 处理 quit 事件**：Qt 文档明确 `QThread.quit()` 是线程安全方法，因此将 worker.finished 到 thread.quit 设为 `Qt.DirectConnection`，在 worker 结束时直接请求其事件循环退出。其他完成/失败信息仍通过正常 Qt 信号传递给主线程，没有在 worker 线程中更新 UI。
6. **避免测试工具自带嵌套事件循环**：原测试等待器在 GUI 线程调用 `QTest.qWait(5)`；故障现场位于其等待行。替换为显式 `app.processEvents()` + `time.sleep(0.005)` 和 monotonic 截止时间，不在测试内部开启嵌套 Qt 等待。

需要说明：这两次诊断能确认症状发生在 Qt 等待/worker 生命周期交界，并支持以上改动；**不能严格证明唯一根因**，因此还应进行后续稳定性观察。

Qt 官方参考：[QThread](https://doc.qt.io/qt-6/qthread.html)（`quit()` 是 thread-safe）和 [Threads and QObjects](https://doc.qt.io/qt-6/threads-qobject.html)（跨线程连接类型和事件循环规则）。

## 最终 Windows 原生回归

- 修复后的单独 Qt 测试：**45 passed**。
- 最终组合修复后，使用有界独立 Python 进程在同一源提交依次执行三次**完整 pytest**，均带 `-vv -x -o faulthandler_timeout=12`：
  - 第一次：**514 passed、5 skipped**，25.78s
  - 第二次：**514 passed、5 skipped**，26.37s
  - 第三次：**514 passed、5 skipped**，33.17s
- 5 项跳过均为 Windows 当前权限环境无法创建符号链接。
- 16 次成功 worker 与 6 次模拟失败 worker 的压力测试包含于上述全量运行；后台任务工作线程事件循环已经启动、回调顺序、线程清理和按钮恢复正常。
- 本机证据保存在：
  `D:\Java-p\GeoPublisher-Workbench-validation\qt-full-diag-eventloop-final-20261102\repeat-1.log`、
  `repeat-2.log`、
  `repeat-3.log` 和最终 `all-tests.xml`。
- 早期复现的异常线程堆栈保留在本次本机命令记录；仅终止**匹配本次 pytest 参数**的测试 Python 进程，没有停止桌面程序。

## GitHub CI 防回归

Windows、Ubuntu matrix 将分别执行：

1. `Qt background worker lifecycle`，最多 3 分钟，包含 Qt 后台压力测试；`faulthandler_timeout=15`。
2. `Full test suite with thread diagnostics`，最多 6 分钟；`faulthandler_timeout=20`。
3. Python wheel 与 Windows 桌面预览构建。

独立 Qt 测试与全量测试有意重叠，用于将线程问题归因到明确阶段。发生新的等待时 CI 将输出线程堆栈并在指定步骤超时后失败，不会无限消耗运行时间。

## 尚未完成

- PR #18 已合并，合并提交 `6c07b4d` 的 Windows/Ubuntu CI 均通过（Actions #41418951252）；仍需由本次及后续更新合并提交重新构建并验证 Windows 内部便携版。
- 真实 GeoServer 发布长任务中的网络超时、应用关闭、取消与任务恢复还需在授权隔离环境分别验收。
- 此修复不代表已经完成完整离线发行门槛，也没有改变任何现场 GIS 数据的离线安全要求。
