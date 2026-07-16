# Task 10 验证记录

验证日期：2026-07-16

## 最终验证

1. 后端全量测试：`python -m unittest discover -s backend/tests -p "test_*.py" -v`，实际运行 133 项，133 项通过。
2. Python 语法：`python -m compileall -q backend/app backend/tests`，通过。
3. JavaScript 语法：使用 Node.js 对 `playwright.config.js`、`frontend/src/**/*.js` 和 E2E spec 执行 `node --check`，通过。
4. 浏览器 E2E：`pnpm test:e2e`，Playwright Chromium 实际运行 7 项，7 项通过，最终一次耗时 14.2 秒。
5. 流程评测：实际运行 10 个合成样本，10 个跑通；结论仍为“仅验证流程跑通，不代表生产级准确率”。
6. 效果评测：实际运行 6 个合同样本和 14 项指标，状态为 `completed_with_failures`。
7. Git 发布：在本记录提交后执行工作区审计、提交并推送 `origin/main`；最终结果以远端分支和交付回复为准。

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
