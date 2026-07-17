# Task 10 验证记录

验证日期：2026-07-16

## 最终验证

1. 后端全量测试：`python -m unittest discover -s backend/tests -p "test_*.py" -v`，实际运行 133 项，133 项通过。
2. Python 语法：`python -m compileall -q backend/app backend/tests`，通过。
3. JavaScript 语法：使用 Node.js 对 `playwright.config.js`、`frontend/src/**/*.js` 和 E2E spec 执行 `node --check`，通过。
4. 浏览器 E2E：`pnpm test:e2e`，Playwright Chromium 实际运行 7 项，7 项通过，最终一次耗时 14.2 秒。
5. 流程评测：实际运行 10 个合成样本，10 个跑通；结论仍为“仅验证流程跑通，不代表生产级准确率”。
6. 效果评测：实际运行 6 个合同样本和 14 项指标，状态为 `completed_with_failures`。
7. Git 发布：`git push origin main` 已成功，远端已包含 `historical-commit` 和 `historical-commit`；本行作为随后独立的发布确认提交推送。

## 未达标指标

`related_clause_recall_at_k` 实际得分为 `0.3333`，阈值为 `0.8`，3 个样本中通过 1 个、失败 2 个。未将该结果改写为通过，也未将流程成功率当作效果准确率。

## 浏览器失败与修复

1. 首次运行因 Playwright 1.61.1 对应的 Chromium revision 1228 不存在，7 项均未启动；在线安装在 10 分钟后超时。
2. 项目改为锁定与本机已安装 Chromium revision 1223 精确对应的 `playwright@1.60.0`，不写入本机绝对路径。
3. 第二次运行 5/7 通过；发现主流程浏览器未消费终态 SSE 最后一帧，以及离线提示被表单刷新覆盖。
4. 前端增加终态查询对账并修复离线提示，随后定向主流程 1/1 通过，最终全量 7/7 通过。

## 剩余限制

1. 当前环境没有外部 LLM/Embedding 凭据，未发生付费网络调用；外部 Provider 的请求 ID、token/调用量、耗时、成本状态、错误和敏感信息屏蔽由 `httpx.MockTransport` 自动化测试验证。
2. 默认仍为 `local_structured` 和 `local_sparse`，不代表真实外部模型已达到可用效果。
3. OCR 未实现；扫描 PDF 会真实进入 `PARSE_FAILED` 并提示需要 OCR。
4. Playwright 浏览器二进制位于本机缓存，不进入 Git；`node_modules`、测试数据库、上传文件、报告、评测运行输出、截图和 trace 均被忽略。
5. 历史提交 `historical-commit` 合并了此前尚未提交的任务 2-9 与任务 10 可观测性修改，因此“每个阶段形成独立提交”未完全达标。远端历史已发布，本轮不擅自改写历史或强制推送。

## Code Review 复核（2026-07-17）

1. 发现工具异常消息可能回显完整敏感合同 prompt，已按本次工具输入中的敏感值脱敏，并只保留异常首行，避免内部堆栈进入执行日志。
2. 发现 Playwright 固定复用 `test-results/e2e/review-agent.sqlite3`，会让历史 Memory 和任务污染后续运行；已改为每次运行分配独立数据库、上传、报告和评测目录。
3. 浏览器验收新增“已选立场但未选文件”用例，并直接读取 SSE 响应体校验终态事件，避免仅由终态查询对账掩盖 SSE 缺帧。
4. 首次加严后的 Playwright 实际运行结果为 7/8：SSE 响应只发送到 `RISK_ANALYZED`，页面终态来自后备查询。修复事件流竞态并新增 API 回归测试后，Playwright 连续两次复跑均为 8/8 通过，最终一次耗时 14.5 秒。
5. 后端全量测试实际运行 134 项，134 项通过；Python 编译和 Node.js 语法检查通过。
6. 流程评测实际运行 10 个样本，10 个跑通；效果评测实际运行 6 个样本、14 项指标，状态仍为 `completed_with_failures`。
7. `related_clause_recall_at_k` 仍为 `0.3333`，阈值 `0.8`，通过 1 个、失败 2 个，没有改写为达标。
8. 最新 E2E 运行目录实际包含独立的 `review-agent.sqlite3`、`uploads`、`reports` 和 `evaluation`，且均位于 Git 忽略的 `test-results/e2e/run-*` 下。
9. 审查修复提交 `historical-commit` 已成功推送到 `origin/main`；提交包含行为范围以及 134 项后端测试、8 项 Playwright 测试的验证结果。
