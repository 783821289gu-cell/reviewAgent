# ContractReviewAgent

当前实现范围：`TASKS.md` 的任务 9，支持 AgentState、显式 Tool Registry、流式状态事件、执行日志、本地 NDA Playbook 规则检索、当前合同内相关条款检索、SQLite Memory 召回、风险分析上下文构建、结构化风险分析、证据验证、修改建议生成、Web 风险展示、原文高亮、风险跳转、局部审查、人工反馈动作、Memory 写入、Markdown 报告导出和基础评测。

## 当前已实现

1. 后端服务启动入口。
2. 后端健康检查接口。
3. 审查状态模型：`START`、`UPLOAD_RECEIVED`、`DOCUMENT_PARSED`、`CLAUSES_STRUCTURED`、`PLAYBOOK_RETRIEVED`、`CONTEXT_BUILT`、`RISK_ANALYZED`、`EVIDENCE_VERIFIED`、`HUMAN_REVIEW_PENDING`、`MEMORY_UPDATED`、`REPORT_READY`、`PARSE_FAILED`、`RETRIEVAL_FAILED`、`LLM_OUTPUT_INVALID`、`EVIDENCE_MISSING`、`NEED_MANUAL_REVIEW`、`TASK_ERROR`。
4. 前端页面启动入口。
5. 首页项目说明、合同上传入口、甲方 / 乙方审查立场选择。
6. 未选择合同文件或审查立场时不能上传解析。
7. Workbench 布局：顶部、左侧原文区、右侧结果区、底部执行记录区。
8. DOCX 段落和表格文本解析。
9. PDF 可读文本解析。
10. 条款编号、条款正文、条款类型、关键字段和原文位置结构化。
11. 完整显式 `tool_registry` 字典和工具输入输出契约。
12. 当前解析链路通过 `ReviewOrchestratorAgent` 调度，并只通过 `tool_registry` 调用工具。
13. 后端通过 Server-Sent Events 输出任务状态事件。
14. 前端实时展示状态进度和执行日志。
15. 本地 JSON NDA Risk Playbook，覆盖第一版 8 类核心风险。
16. `retrieve_playbook_rules` 已通过显式 `tool_registry` 调用，并按合同类型、条款类型、关键字段和审查立场检索规则。
17. 前端展示命中的 Playbook 规则；未命中规则时不会生成正式风险。
18. `retrieve_related_clauses` 已通过显式 `tool_registry` 调用，在当前合同条款内执行轻量向量召回和 rerank。
19. `retrieve_memory` 已通过显式 `tool_registry` 调用；支持从调用方提供的 `memory_items` 中过滤召回，也支持从 SQLite Memory 中按合同类型、条款类型、风险类型和审查立场检索历史反馈。
20. 后端构建风险分析上下文，包含当前条款、命中规则、相关条款、相关 Memory 字段、输出约束和证据约束。
21. `analyze_risk` 已通过显式 `tool_registry` 调用，当前使用 `REVIEW_AGENT_LLM_MODE=local_structured` 本地结构化分析器；工具契约会同时暴露 `runtime_calls_llm=false` 和实际运行模式，不把本地分析记录为真实外部 LLM 调用。
22. `verify_evidence` 已通过显式 `tool_registry` 调用，校验 `clause_id`、`evidence_text`、风险原因与证据文本相关性，以及命中规则一致性。
23. `generate_revision` 已通过显式 `tool_registry` 调用，生成结构化修改建议。
24. 前端展示上下文 Trace、已验证风险列表、规则详情、证据文本、风险原因和修改建议。
25. 点击风险可定位到左侧对应条款，并高亮正式风险证据文本。
26. 用户可以在合同原文中框选文本并发起局部审查，局部审查结果与正式风险列表分开展示。
27. 局部审查只返回当前框选文本的复核结果，不写入正式风险列表，不写入 Memory。
28. 用户可以对正式风险执行采纳、忽略、修改等级、修改建议，并选择是否加入报告。
29. 所有采纳、忽略和修改动作通过 `tool_registry["write_memory"]` 写入 SQLite Memory。
30. 后续相似审查会通过 `retrieve_memory` 召回相关 Memory；当 Memory 影响修改建议时，风险详情展示历史反馈引用。
31. 用户可以基于正式风险证据触发局部重审，局部结果仍不写入正式风险列表。
32. 用户可以导出 Markdown 审查报告，报告生成通过 `tool_registry["generate_report"]` 调用。
33. 报告包含合同名称、审查立场、Playbook 版本、风险等级、风险原因、原文证据、修改建议和人工反馈状态。
34. 报告只导出用户明确允许进入报告且不处于待人工复核状态的风险，不包含完整执行日志和技术实现细节。
35. `samples/` 提供 10 份项目内合成 NDA 样本，样本来源已在 `samples/README.md` 说明。
36. `evaluation/` 支持手动触发基础评测摘要，覆盖任务是否跑通、文档解析、条款结构化、Playbook 命中、风险证据、Memory 写入、报告导出和失败原因。

## 当前未实现

后续待做包括：使用 FastAPI / Uvicorn 替换当前 `ThreadingHTTPServer`，由 FastAPI 统一托管前端并移除 `python -m http.server`；补充非 NDA 拒绝、真实外部 LLM、审查立场差异化、任务持久化和恢复、真实语义 Embedding、效果型评测、PDF 边界增强、真实 token/cost 计量以及浏览器端到端验证。

完整技术方案、实施顺序和验收口径见 `NEXT_PLAN.md`。其中内容均为待做，不代表已经实现。

真实外部 LLM 接入仍属于后续任务。当前基础评测只验证流程跑通，不声明生产级准确率。

当前 11 个工具均已在 `tool_registry` 中注册契约，其中 `analyze_risk` 和 `generate_revision` 默认使用本地结构化实现，不记录为真实外部 LLM 调用。

## 运行后端

```powershell
python backend\app\main.py
```

健康检查：

```text
http://127.0.0.1:8000/health
```

## 运行前端

```powershell
python -m http.server 5174 --bind 127.0.0.1 --directory frontend
```

访问：

```text
http://127.0.0.1:5174
```

## 验证任务 9

1. 启动后端。
2. 访问 `http://127.0.0.1:8000/health`，确认返回 `status: ok`。
3. 启动前端。
4. 访问 `http://127.0.0.1:5174`。
5. 确认首页有合同上传入口和甲方 / 乙方审查立场选择。
6. 不选择合同文件或审查立场时，确认按钮不可用。
7. 上传 `.docx` 或 `.pdf` 并选择审查立场。
8. 确认右侧流式进度展示 `START`、`UPLOAD_RECEIVED`、`DOCUMENT_PARSED`、`CLAUSES_STRUCTURED`、`PLAYBOOK_RETRIEVED`、`CONTEXT_BUILT`、`RISK_ANALYZED`、`EVIDENCE_VERIFIED`；如果存在高风险或低置信度风险，则最终进入 `HUMAN_REVIEW_PENDING`。
9. 确认 Workbench 左侧显示条款编号、条款正文、条款类型和关键字段。
10. 确认右侧展示命中的 Playbook 规则；未命中时提示不会生成正式风险。
11. 确认右侧展示上下文 Trace，包含当前条款、相关条款、相关 Memory 和约束信息。
12. 确认右侧展示已验证风险列表、证据文本和修改建议；无证据风险不得展示为正式风险。
13. 点击风险列表中的定位按钮，确认左侧跳转到对应条款，并能看到证据高亮。
14. 在左侧条款原文中框选文本，点击局部审查，确认右侧展示局部审查结果，且正式风险列表不被改写。
15. 框选不属于该条款的文本调用局部审查接口时，确认返回友好错误。
16. 在风险详情中执行采纳、忽略、修改等级、修改建议，确认任务状态更新为 `MEMORY_UPDATED`，风险复核状态、报告选择和 Memory ID 可见。
17. 对风险点击局部重审，确认以该风险证据文本发起局部审查，正式风险列表不被改写。
18. 确认底部执行记录显示 `parse_document`、`extract_clauses`、`extract_key_fields`、`retrieve_playbook_rules`、`retrieve_related_clauses`、`retrieve_memory`、`analyze_risk`、`generate_revision`、`verify_evidence`、`write_memory` 的工具名、状态、耗时、摘要和真实 token/cost 摘要；本地结构化工具应显示 `*_no_external_llm`，不能显示外部 LLM 计量占位。
19. 对需要进入报告的风险提交人工反馈并勾选加入报告，点击导出报告，确认浏览器下载 Markdown 文件，且后端任务状态变为 `REPORT_READY`。
20. 确认报告只包含已允许进入报告的风险，不包含未加入报告的忽略风险、待人工复核但未确认风险、完整执行日志或技术实现细节。
21. 点击基础评测中的运行评测，确认返回 10 份合成 NDA 样本的摘要；摘要包含样本名称、任务跑通、文档解析、条款结构化、Playbook 命中、风险证据、Memory 写入、报告导出和失败原因。
22. 访问 `http://127.0.0.1:8000/api/tools`，确认 11 个工具都有输入输出契约、`runtime_calls_llm`、`runtime_llm_mode`，且 `write_memory` 输入包含 `human_feedback`、`generate_report` 输入包含 `task_id`。
23. 上传相似合同后，确认相关 Memory 能进入上下文 Trace；当 Memory 影响建议时，风险详情展示历史反馈引用。
24. 上传不支持或不可解析文件时，确认状态进入错误提示，不显示伪造解析结果。

默认上传请求大小上限为 10 MB，可通过 `REVIEW_AGENT_MAX_UPLOAD_BYTES` 调整。
默认 SQLite Memory 路径为 `backend/app/data/review_agent_memory.sqlite3`，可通过 `REVIEW_AGENT_MEMORY_DB_PATH` 调整。

## 运行测试

```powershell
python -m unittest discover backend\tests
```
