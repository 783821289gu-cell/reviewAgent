# TASKS_AGENT

## 1. 文档说明

本文件将 `SPEC.md` 和 `AGENT_EVOLUTION_PLAN.md` 拆分为可按顺序开发、Code Review、QA 和验收的 Agent 能力增强任务。

本文件只拆任务，不表示任何任务已经实现。开发时必须继续遵守 `constitution.md`，不得用计划、Mock、占位数据或本地确定性结果冒充 DeepSeek 真实调用结果。

## 2. 全局约束

以下约束适用于全部任务：

1. 保留 `SPEC.md` 的 NDA 第一版范围，不扩展合同类型、OCR、权限、多租户或审批流。
2. 正式风险继续限定为已定义的 8 类 NDA 核心风险，并必须绑定 `clause_id`、原文证据、风险原因、修改建议和 Playbook Rule。
3. 高风险、低置信度、证据失败、Analyzer 与 Verifier 冲突、不可恢复结构错误必须保留人工复核路径。
4. 保留单个 `ReviewOrchestratorAgent`、自定义状态机和显式 `tool_registry`，不拆自由对话 Multi-Agent，不迁移 LangGraph。
5. 模型只能输出受 Schema 和白名单约束的数据或动作，不能直接执行 Python 函数、文件操作、数据库修改或网络请求。
6. 所有循环必须有固定重试预算和终止状态，不允许无限自主循环。
7. Memory 只能辅助建议，不能覆盖 Playbook、审查立场或合同原文证据。
8. 保留现有 API、SSE `review_event`、局部审查、人工反馈、报告过滤和 Web Workbench 主流程；必要新增接口必须向后兼容。
9. DeepSeek Key、Authorization、完整合同 Prompt、数据库和运行产物不得进入 Git、日志或评测摘要。
10. 每个任务必须单独实现、Code Review、QA、验证和提交；未满足验收标准不能开始下一任务。

**第一个要实现的任务：任务 1。**

---

## 任务 1：冻结 Agent 基线与 DeepSeek API 契约

**标记：第一个要实现的任务。**

### 实现目标

在改变运行行为前，冻结当前 Agent 功能、API、效果指标和测试基线；建立脱敏的 DeepSeek Chat Completions 请求/响应契约夹具，为后续 Provider、Planner 和效果优化提供可比较基准。

### 涉及文件

1. `evaluation/baseline/agent-evolution-baseline.md`（新增）
2. `evaluation/baseline/agent-evolution-baseline.json`（新增）
3. `backend/tests/fixtures/deepseek/`（新增脱敏契约夹具）
4. `backend/tests/test_llm_provider.py`
5. `backend/tests/test_effect_evaluation.py`
6. `frontend/tests/e2e/review-workbench.spec.js`
7. `playwright.config.js`（仅在隔离基线运行确有必要时修改）

### 验收标准

1. 记录当前代码、Playbook、标注、Prompt、LLM 模式和 Embedding 模式版本。
2. 记录当前流程评测 10 个样本和效果评测 6 个样本、14 项指标的真实结果。
3. 明确记录相关条款 Recall@1 当前为 `0.3333`、阈值为 `0.8`，不得写成通过。
4. DeepSeek 契约夹具覆盖成功 JSON、空 `content`、截断 JSON、Schema 错误、超时、限流、认证失败和临时服务错误。
5. 契约使用 `https://api.deepseek.com/chat/completions`、`deepseek-v4-pro` 和 JSON Output，不包含真实 Key。
6. 当前 API 成功字段、主要错误码和 SSE 事件格式形成可回归基线。
7. 当前后端全量测试和 10 条 Playwright 测试实际运行并通过；失败必须记录，不能通过修改基线掩盖。
8. 本任务不接入真实 DeepSeek，不改变默认 `local_structured` 行为。

---

## 任务 2：接通并加固 DeepSeek Provider

### 实现目标

复用现有 OpenAI-compatible Provider 边界，将 DeepSeek 作为明确的真实 LLM 配置接入风险分析和修改建议生成；加固 JSON Output、错误分类、一次重试、调用元数据和敏感信息屏蔽。

### 涉及文件

1. `.env.example`
2. `backend/app/config.py`
3. `backend/app/providers/llm_provider.py`
4. `backend/app/services/llm_service.py`
5. `backend/app/services/risk_analyzer.py`
6. `backend/app/services/log_service.py`
7. `backend/app/models/log.py`
8. `backend/app/tools/contracts.py`
9. `backend/tests/test_llm_provider.py`
10. `backend/tests/test_risk_analysis.py`
11. `backend/tests/test_review_orchestrator.py`

### 验收标准

1. 使用以下现有配置边界：`REVIEW_AGENT_LLM_MODE=openai_compatible`、Base URL、API Key、Model 和超时。
2. DeepSeek 默认示例配置使用 `https://api.deepseek.com` 和 `deepseek-v4-pro`，不硬编码 Key。
3. 请求启用 JSON Output，Prompt 明确要求 JSON 并提供与输出 Schema 一致的示例。
4. `analyze_risk` 和 `generate_revision` 的 DeepSeek 输出必须通过现有结构化模型校验。
5. 空内容、截断 JSON、字段缺失和类型错误进入 `LLM_OUTPUT_INVALID` 或人工复核，不进入正式风险列表。
6. 只对超时、限流和临时服务错误重试一次；认证、配置和确定性 Schema 错误不循环重试。
7. 日志记录真实模型、Provider 请求 ID、prompt/completion token、耗时、成本状态和错误类型。
8. 未配置 Key 时服务仍能启动；选择外部模式并发生调用时返回明确配置错误，不静默回退到本地成功。
9. 日志、异常、数据库和响应中不包含 Key、Authorization 或完整合同 Prompt。
10. Mock 契约测试全部通过；使用用户本机 Key 完成一次脱敏真实 DeepSeek NDA 主流程后，才允许声明真实接入已验证。

---

## 任务 3：建立 Prompt 安全、版本和真实 Token Budget

### 实现目标

将系统规则、任务、Playbook、合同原文和 Memory 作为不同信任级别的数据组装；增加 Prompt Injection 防护、Prompt 版本和与 DeepSeek 请求一致的 token 预算及裁剪轨迹。

### 涉及文件

1. `requirements.txt`（仅在确认 tokenizer 依赖必要时修改）
2. `.env.example`（仅加入经验证确有必要的配置）
3. `backend/app/config.py`
4. `backend/app/services/context_builder.py`
5. `backend/app/services/llm_service.py`
6. `backend/app/providers/llm_provider.py`
7. `backend/app/models/retrieval.py`
8. `backend/app/models/log.py`
9. `samples/prompt_injection/`（新增合成样本）
10. `backend/tests/test_retrieval_context.py`
11. `backend/tests/test_llm_provider.py`
12. `backend/tests/test_prompt_security.py`（新增）

### 验收标准

1. Prompt 明确区分 System Policy、任务、Playbook、合同数据、相关条款、Memory、Evidence Constraint 和输出 Schema。
2. 合同或 Memory 中的伪造系统指令、工具指令和输出格式指令不能改变 Agent 行为。
3. Planner、Analyzer 和 Critic 使用独立 Schema，不共享可被合同文本污染的动作字段。
4. 工具名、动作名、状态名、风险类型和目标条款均执行白名单校验。
5. token 预算不再使用固定字符数冒充真实 token；计数机制和适用模型有明确测试依据。
6. 超限时依次减少低相关 Memory、低排名相关条款和可压缩上下文，不删除当前条款、Playbook、Schema 和证据约束。
7. Step Log 记录各上下文类别 token、裁剪原因、最终预算和 Prompt 版本，不记录完整敏感文本。
8. 至少覆盖提示注入、伪造系统消息、伪造工具结果和超长恶意文本；失败时进入明确错误或人工复核。
9. Prompt Injection 样本不得触发额外工具或生成未绑定证据的正式风险。

---

## 任务 4：实现混合检索、rerank 与可验证召回提升

### 实现目标

在当前合同范围内增加确定性关键词召回，与现有 Embedding 余弦召回合并去重；完善结构化查询、动态 Top-K 和可解释 rerank，并优先解决当前相关条款 Recall@1 未达标问题。

### 涉及文件

1. `backend/app/services/clause_index_service.py`
2. `backend/app/services/rerank_service.py`
3. `backend/app/providers/embedding_provider.py`
4. `backend/app/models/retrieval.py`
5. `backend/app/services/context_builder.py`
6. `backend/app/services/log_service.py`
7. `samples/annotations/related_clauses.json`
8. `samples/annotations/schema.json`
9. `samples/manifest.json`
10. `backend/tests/test_retrieval_context.py`
11. `backend/tests/test_embedding_provider.py`
12. `backend/tests/test_effect_evaluation.py`

### 验收标准

1. 检索查询包含当前条款、条款类型、风险类型、Playbook 检查点和关键字段。
2. 关键词与 Embedding 候选合并去重，检索范围仍只限当前合同。
3. Top-K 根据候选数量和分数调整，但具有配置上限，不无限扩大上下文。
4. rerank 记录条款类型、关键词、向量相似度、字段匹配、Playbook 适用性和最终分数。
5. 不引入外部合同库、互联网检索或向量数据库。
6. 扩大相关条款标注，不能只针对现有三个样本硬编码同义词或排序结果。
7. 当前标注集 Recall@1 达到至少 `0.8`，且扩大后的标注集继续达到阈值。
8. Playbook Recall、证据 span 命中率和端到端成功率不得低于现有阈值。
9. 未达到阈值时必须保留失败状态和失败样本，不能进入“检索完成”声明。

---

## 任务 5：实现受控 Planner/Router 与一次检索修复循环

### 实现目标

在固定主状态流不变的前提下，为低置信度、证据失败、召回不足、Analyzer/Verifier 冲突和不可恢复结构错误增加受控 Planner；Planner 只返回白名单动作，由 Orchestrator 校验并执行最多一次修复。

### 涉及文件

1. `backend/app/models/planner.py`（新增）
2. `backend/app/services/planner_service.py`（新增）
3. `backend/app/models/review.py`
4. `backend/app/services/review_service.py`
5. `backend/app/services/llm_service.py`
6. `backend/app/services/clause_index_service.py`
7. `backend/app/tools/registry.py`
8. `backend/app/tools/contracts.py`
9. `backend/app/services/log_service.py`
10. `backend/tests/test_planner.py`（新增）
11. `backend/tests/test_review_orchestrator.py`
12. `backend/tests/test_retrieval_context.py`

### 验收标准

1. Planner 只允许 `RETRIEVE_AGAIN`、`ANALYZE_AGAIN`、`REQUEST_HUMAN_REVIEW` 和 `TERMINATE`。
2. Planner 输出包含固定 `reason_code`、当前合同内 `target_clause_id`、受限查询调整和置信度。
3. Planner 作为显式注册能力具有输入输出契约和 Step Log；不能绕过 `tool_registry`。
4. Orchestrator 校验动作、状态、条款归属和重试预算后才执行工具。
5. Planner 不能直接生成正式风险、修改数据库、调用任意工具或改变 Playbook。
6. Evidence 首次失败时最多执行一次检索改写和重新分析；再次失败进入 `HUMAN_REVIEW_PENDING` 或 `EVIDENCE_MISSING`。
7. 非法动作、未知条款、越权状态和耗尽预算均被拒绝并记录真实原因。
8. Planner 非法动作实际执行率为 `0`，不存在无限循环或重复副作用。
9. 原有确定性成功路径不需要 Planner 时保持原工具顺序和结果。

---

## 任务 6：增加受控 Critic 并保持 Evidence 最终裁决

### 实现目标

在 Risk Analyzer 和确定性 Evidence Verifier 之间增加受控 Critic，验证风险原因是否得到原文和 Playbook 支持；Critic 只能建议通过、拒绝或人工复核，不能创造证据或绕过 Evidence Verifier。

### 涉及文件

1. `backend/app/models/risk.py`
2. `backend/app/services/risk_critic.py`（新增）
3. `backend/app/services/risk_analyzer.py`
4. `backend/app/services/evidence_service.py`
5. `backend/app/services/review_service.py`
6. `backend/app/services/llm_service.py`
7. `backend/app/tools/registry.py`
8. `backend/app/tools/contracts.py`
9. `backend/app/services/log_service.py`
10. `backend/tests/test_risk_critic.py`（新增）
11. `backend/tests/test_risk_analysis.py`
12. `backend/tests/test_review_orchestrator.py`

### 验收标准

1. Analyzer、Critic、Evidence Verifier 的输入、输出和职责边界独立。
2. Critic 输出限定为 `PASS`、`REJECT` 或 `REQUEST_HUMAN_REVIEW` 及固定原因码。
3. Critic 不得返回新 `evidence_text`、新条款、新规则或直接修改风险等级。
4. Evidence Verifier 继续确定性校验条款、原文 span、风险类型和规则一致性，并拥有最终准入权。
5. 只有 Evidence Verifier 通过的候选才能进入正式风险列表。
6. Critic 与 Analyzer 冲突时进入人工复核或受控修复，不静默选择模型结论。
7. Critic 作为显式工具注册并产生独立 Step Log、token、耗时和错误摘要。
8. 未命中 Playbook、伪造证据、跨条款证据和注入文本测试均不能生成正式风险。

---

## 任务 7：实现 Memory 聚合、冲突和生命周期管理

### 实现目标

保留不可变的人工反馈 Episodic Memory，在其上建立可追溯的 Semantic Preference；支持重复反馈聚合、冲突并存、置信度变化和过期处理，并用有/无 Memory 对照评测验证实际影响。

### 涉及文件

1. `backend/app/models/memory.py`
2. `backend/app/db/sqlite.py`
3. `backend/app/db/repositories/memory_repository.py`
4. `backend/app/services/memory_service.py`
5. `backend/app/services/context_builder.py`
6. `backend/app/services/feedback_service.py`
7. `backend/app/models/log.py`
8. `samples/annotations/memory.json`（新增）
9. `samples/annotations/schema.json`
10. `samples/manifest.json`
11. `backend/tests/test_memory_feedback.py`
12. `backend/tests/test_memory_lifecycle.py`（新增）
13. `backend/tests/test_effect_evaluation.py`

### 验收标准

1. 原始人工反馈继续以稳定幂等键保存，不因聚合、冲突或过期被覆盖或删除。
2. Semantic Preference 按合同类型、条款类型、风险类型和审查立场聚合。
3. 聚合记录支持次数、反对次数、来源 Memory ID、最近使用时间和置信度。
4. 冲突反馈同时保留，不自动选择风险更高或更低的一方。
5. 被后续反馈否定或长期未使用时只降低派生偏好置信度，不删除审计记录。
6. Memory 注入记录来源、匹配分数、是否影响建议和裁剪状态。
7. Memory 不能改变 Playbook 命中、合同证据或生成无规则正式风险。
8. 使用同一批样本运行有 Memory/无 Memory 对照，真实记录建议一致性或人工采纳差异。
9. 没有可测提升时必须显示未提升，不能预设 Memory 有效。

---

## 任务 8：增强可恢复执行、人工控制和 Agent Trace

### 实现目标

在现有服务重启恢复基础上增加任务取消、节点/任务超时、错误重试预算、人工恢复和同任务并发保护；扩展 Trace 以完整记录 Planner、检索、Critic、Evidence、Context 和 Provider 决策链。

### 涉及文件

1. `backend/app/models/review.py`
2. `backend/app/models/log.py`
3. `backend/app/db/repositories/task_repository.py`
4. `backend/app/db/repositories/event_repository.py`
5. `backend/app/services/review_service.py`
6. `backend/app/services/event_service.py`
7. `backend/app/services/log_service.py`
8. `backend/app/services/task_service.py`
9. `backend/app/api/routes/tasks.py`
10. `frontend/src/App.js`
11. `frontend/src/components/ExecutionLog.js`
12. `frontend/src/components/ReviewProgress.js`
13. `frontend/src/components/WorkbenchLayout.js`
14. `backend/tests/test_task_recovery.py`
15. `backend/tests/test_task_persistence.py`
16. `backend/tests/test_agent_execution_control.py`（新增）
17. `frontend/tests/e2e/review-workbench.spec.js`

### 验收标准

1. 支持明确任务取消状态；取消后不再开始新节点，已完成结果和日志保留。
2. 单节点和全任务超时进入真实错误或人工复核，不记录为成功。
3. 每类错误有固定最大重试次数，重启恢复后不会重置并造成无限重试。
4. 人工恢复记录操作者动作、恢复原因、恢复起点和恢复次数。
5. 同一任务不能被两个执行线程并发推进；重复请求不产生重复 Memory、报告或日志副作用。
6. Planner、检索修复和 Critic 使用稳定幂等键。
7. Trace 记录父 Step、重试序号、Prompt/模型/Playbook/代码版本、token 分配、Planner 动作、检索候选变化、Critic/Verifier 结论和最终状态。
8. Trace 不记录 Key、Authorization、完整合同正文或完整 Prompt。
9. Web Workbench 能真实展示取消、超时、恢复、重试和决策摘要，不增加伪造状态。
10. 正常运行、取消、超时、并发、服务重启和人工恢复自动化测试通过。

---

## 任务 9：扩大标注集并完成 Agent 与 DeepSeek 效果验收

### 实现目标

扩大人工标注集，增加 Planner、结构化输出、检索修复、恢复、Memory 和 Prompt Injection 指标；使用用户本机 DeepSeek Key 运行真实效果、成本和稳定性评测，输出版本化真实结果。

### 涉及文件

1. `samples/README.md`
2. `samples/manifest.json`
3. `samples/annotations/schema.json`
4. `samples/annotations/contracts.json`
5. `samples/annotations/clauses.json`
6. `samples/annotations/risks.json`
7. `samples/annotations/related_clauses.json`
8. `samples/annotations/memory.json`
9. `samples/prompt_injection/`
10. `backend/app/models/evaluation.py`
11. `backend/app/services/evaluation_service.py`
12. `backend/app/api/routes/evaluation.py`
13. `backend/tests/test_effect_evaluation.py`
14. `backend/tests/test_llm_provider.py`
15. `evaluation/README.md`
16. `evaluation/release/agent-evolution-verification.md`（新增脱敏记录）

### 验收标准

1. 标注集达到 20–30 份合同、至少 50 条风险条款、100 条普通条款和 20 组跨条款检索标注。
2. 包含至少 20 条 Memory 对照样本和 10 条 Prompt Injection/恶意输入样本。
3. 所有样本来源为合成、公开授权或脱敏授权，并通过 Schema 校验。
4. 保留现有 14 项指标并新增 Planner 动作准确率、非法动作率、JSON 合法率、Schema 修复率、检索修复率、恢复率、无证据风险率、Memory 一致性、Injection 阻断率、token、成本和 P95 延迟。
5. DeepSeek 真实调用完成完整 NDA 主流程，评测记录模型、请求 ID 摘要、Prompt、Playbook、标注和代码版本。
6. LLM JSON 合法率达到 `98%`，一次修复后达到 `100%`；无法修复时进入明确错误或人工复核。
7. Planner 非法动作执行率为 `0`，Prompt Injection 阻断率为 `100%`。
8. 相关条款 Recall@1 达到 `0.8`，Evidence span 命中率不低于 `0.9`。
9. Memory 效果根据对照结果如实记录，不预设通过。
10. 运行至少两次稳定性评测，记录结果波动、成本和 P95 延迟，不只保留最好一次。
11. 未达标指标和失败样本进入版本化摘要；Key、完整合同和完整 Prompt 不进入摘要或 Git。

---

## 任务 10：完成 Web 验收、文档同步和发布交付

### 实现目标

统一验证 DeepSeek、Planner、检索修复、Critic、Memory、执行控制和 Trace 的用户可见行为；同步真实配置与限制，完成全量回归、独立提交和远端发布。

### 涉及文件

1. `.env.example`
2. `.gitignore`
3. `README.md`
4. `frontend/src/components/ExecutionLog.js`
5. `frontend/src/components/EvaluationPanel.js`
6. `frontend/src/components/WorkbenchLayout.js`
7. `frontend/tests/e2e/review-workbench.spec.js`
8. `frontend/tests/e2e/review-workbench.visual.spec.js`
9. `frontend/tests/fixtures/`
10. `playwright.config.js`
11. `evaluation/release/agent-evolution-verification.md`
12. Git 提交和远端分支

### 验收标准

1. Web Workbench 能区分 DeepSeek 真实调用、本地模式、Planner 动作、检索修复、Critic、Evidence、Memory、取消、超时和人工恢复状态。
2. 人工复核入口保持突出，高风险、证据失败和冲突结果不能伪装成确定结论。
3. 现有上传、SSE、风险跳转、高亮、局部审查、反馈、Memory、报告、评测和错误流 E2E 断言未被删除或弱化。
4. 新增 DeepSeek 配置错误、Planner 非法动作、一次修复、Injection 阻断、取消、超时和恢复的浏览器或 API 验收。
5. 后端全量测试、API 契约、流程评测、效果评测和 Playwright 全量测试实际运行并记录真实结果。
6. README 明确 DeepSeek Key 通过 `REVIEW_AGENT_LLM_API_KEY` 环境变量配置，当前代码是否自动加载 `.env` 必须按真实行为说明。
7. README 只写已通过真实 DeepSeek 验证的能力；Mock 或本地模式不能描述为真实模型效果。
8. DeepSeek Key、数据库、上传文件、报告、完整评测运行产物、截图、trace、缓存和本机路径均不进入 Git。
9. 每个任务形成独立可审查提交，提交说明包含行为范围和验证结果。
10. 最终工作树干净，计划内改动已推送到确认远端；任何未通过指标、未验证外部调用和剩余限制如实保留。
