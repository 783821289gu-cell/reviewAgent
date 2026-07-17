# Agent 能力增强独立计划

## 1. 文档状态

本文件是 `ContractReviewAgent` 在现有功能闭环之上的独立 Agent 技术迭代计划。

本计划不覆盖 `PLAN.md`、`NEXT_PLAN.md`、`UI_REDESIGN_PLAN.md` 或现有任务记录，不把计划内容描述为已实现。后续如进入开发，应另行生成对应任务文件，并按 `constitution.md` 逐项实现、Review 和验证。

## 2. 当前基线

当前已经具备：

1. `ReviewOrchestratorAgent` 自定义状态机和持久化 `AgentState`。
2. 12 个显式注册工具及输入输出契约。
3. 文档解析、条款结构化、Playbook 检索、相关条款检索、Memory 检索、风险分析、证据验证、人工反馈和报告生成闭环。
4. 默认 `local_structured` LLM 模式和 `local_sparse` Embedding 模式。
5. OpenAI-compatible LLM/Embedding Provider 边界及 MockTransport 自动化测试。
6. 任务恢复、SSE、Step Log、Provider 调用摘要和效果评测。
7. 当前效果评测使用 6 份合同样本和 14 项指标；相关条款 Recall@1 为 `0.3333`，未达到 `0.8` 阈值。

当前没有外部 DeepSeek 凭据下的真实调用结果，因此不能声称 DeepSeek 已接入完成或模型效果已验证。

## 3. 迭代目标

本轮目标是把当前“确定性 Agent MVP”增强为“具有真实模型调用、受控决策、检索修复、Memory 生命周期和可量化轨迹的 Agent 系统”。

目标能力：

1. 使用 DeepSeek 执行真实结构化风险分析和修改建议生成。
2. 在有限白名单动作内增加受控 Planner/Router，不允许模型任意调用工具。
3. 增加自适应检索和一次有上限的证据失败修复循环。
4. 建立真实 tokenizer 预算、Prompt Injection 防护和 Prompt 版本管理。
5. 保持 Risk Analyzer 与 Evidence Verifier 职责分离，并增加受控 Critic。
6. 增加 Memory 聚合、冲突、过期和置信度管理。
7. 增强任务取消、节点超时、重试上限、人工恢复和 Agent Trace。
8. 用扩大的人工标注集验证工具选择、结构化输出、检索、证据、Memory 和恢复效果。

## 4. 明确不做

1. 不拆成多个自由对话的 Agent。
2. 不迁移 LangGraph，继续使用当前自定义状态机。
3. 不允许模型直接执行任意 Python 函数、文件操作或网络请求。
4. 不增加无限自主循环；所有循环必须有固定次数和终止状态。
5. 不在本轮引入强化学习或模型微调。
6. 不因 Agent 增强顺手扩展合同类型、OCR、权限、多租户或审批流。
7. 不默认引入向量数据库；先使用现有当前合同内检索边界验证效果。

## 5. DeepSeek 技术选择

### 5.1 接口与模型

1. 使用 DeepSeek 官方 OpenAI-compatible Chat Completions 接口。
2. Base URL 使用 `https://api.deepseek.com`。
3. 第一条真实效果基线使用 `deepseek-v4-pro`。
4. 不使用旧 `deepseek-chat` 或 `deepseek-reasoner` 名称作为新配置；官方文档说明它们将在 2026-07-24 停止兼容。
5. 第一版继续使用 JSON Output，不直接切换到模型驱动的原生工具执行。
6. Planner 也必须输出结构化动作 JSON，由 Orchestrator 校验后执行显式 `tool_registry`。

官方依据：

- DeepSeek API Base URL 与当前模型名称：<https://api-docs.deepseek.com/>
- JSON Output 要求：<https://api-docs.deepseek.com/guides/json_mode/>
- Tool Calls 与 strict schema：<https://api-docs.deepseek.com/guides/tool_calls>

### 5.2 配置原则

沿用当前环境变量，不把 Key 写入源码、Markdown、日志、数据库或 Git：

```text
REVIEW_AGENT_LLM_MODE=openai_compatible
REVIEW_AGENT_LLM_BASE_URL=https://api.deepseek.com
REVIEW_AGENT_LLM_API_KEY=<本机 DeepSeek API Key>
REVIEW_AGENT_LLM_MODEL=deepseek-v4-pro
REVIEW_AGENT_LLM_TIMEOUT_SECONDS=60
```

新增配置只有在实现和验收需要时才允许引入，候选项包括：

```text
REVIEW_AGENT_LLM_MAX_TOKENS
REVIEW_AGENT_LLM_THINKING_MODE
REVIEW_AGENT_LLM_PROMPT_VERSION
```

是否加入这些候选配置，必须由 DeepSeek 兼容性测试结果决定，不提前增加无效配置。

### 5.3 Provider 加固

DeepSeek 接入阶段必须验证：

1. `response_format={"type":"json_object"}` 生效。
2. System Prompt 明确包含 JSON 约束和输出示例。
3. 空 `content`、截断 JSON、Schema 不匹配分别进入真实错误状态。
4. 仅对超时、限流和临时服务错误重试一次；认证、配置和确定性 Schema 错误不循环重试。
5. 记录模型、请求 ID、prompt/completion token、耗时和成本状态。
6. 日志不记录 API Key、Authorization、完整合同正文或完整 Prompt。
7. 未配置 Key 时启动可以成功，但外部调用必须返回明确配置错误，不能回退成伪造的 DeepSeek 结果。

## 6. Prompt 与输入安全

### 6.1 数据边界

发送给 DeepSeek 的消息必须明确区分：

1. 系统规则和输出 Schema。
2. 当前审查任务。
3. Playbook 规则。
4. 合同原文和相关条款等不可信数据。
5. Memory 等不可信历史输入。

合同正文中的“忽略此前规则”“调用工具”“输出其他格式”等文本只能作为合同数据处理，不能改变系统行为。

### 6.2 防护要求

1. 对 Prompt 中的数据段使用明确边界和固定字段，不拼接为可执行指令。
2. Planner 动作、风险输出和 Critic 输出分别使用独立 Schema。
3. 对工具名、动作名、状态名和风险字段执行白名单校验。
4. 增加包含提示注入、伪造系统消息、伪造工具结果和超长恶意文本的合成测试合同。
5. 注入测试失败时不得生成正式风险或执行额外工具，应进入人工复核或明确错误状态。

## 7. Token Context Budget

当前 6000 字符预算改为与实际 DeepSeek 请求一致的 token 预算，但不得删除现有安全约束。

预算顺序：

1. P0：System Policy、输出 Schema、当前条款、命中 Playbook、Evidence Constraint。
2. P1：关键字段、审查立场、高分相关条款。
3. P2：高相关 Memory。
4. P3：低分相关条款和低相关 Memory。

超限时：

1. 先删除低相关 Memory。
2. 再删除低排名相关条款。
3. 必要时对相关条款做可追踪压缩。
4. 不删除当前条款、Playbook、输出 Schema 和证据约束。

每次 DeepSeek 调用记录各上下文类别的 token 数、裁剪动作、最终预算和 Prompt 版本，但不记录完整敏感文本。

## 8. 自适应检索循环

### 8.1 检索策略

在现有 `clause_type + Embedding + cosine + rule rerank` 基础上增加：

1. 从当前条款、风险类型、Playbook 检查点和关键字段生成结构化检索查询。
2. 增加确定性关键词召回，与 Embedding 结果合并去重。
3. 根据候选数量和分数动态选择 Top-K，但必须设置上限。
4. rerank 显式记录条款类型、关键词、向量相似度、字段匹配和 Playbook 适用性因子。

### 8.2 修复循环

```text
首次检索
→ 风险分析
→ Evidence Verifier
→ 证据通过：输出正式风险
→ 证据失败：改写一次检索查询并重新分析
→ 再次失败：进入 HUMAN_REVIEW_PENDING 或 EVIDENCE_MISSING
```

最多允许一次检索修复，不允许无限循环。每次查询、候选变化、重试原因和终止状态进入 Agent Trace。

### 8.3 目标

1. 当前标注集相关条款 Recall@1 从 `0.3333` 提升到至少 `0.8`。
2. 扩大标注集后仍达到既定阈值，不能只针对现有三个样本硬编码。
3. 不降低 Playbook Recall、证据 span 命中率和端到端成功率。

## 9. 受控 Planner/Router

### 9.1 使用边界

固定主状态流保持不变。Planner 只用于以下非确定性节点：

1. 风险分析置信度不足。
2. Playbook 命中但证据失败。
3. 相关条款召回不足。
4. Risk Analyzer 与 Evidence Verifier 结论冲突。
5. LLM 输出在一次修复后仍不合法。

### 9.2 白名单动作

```json
{
  "action": "RETRIEVE_AGAIN | ANALYZE_AGAIN | REQUEST_HUMAN_REVIEW | TERMINATE",
  "reason_code": "固定枚举",
  "target_clause_id": "必须属于当前合同",
  "query_adjustments": {},
  "confidence": 0.0
}
```

Orchestrator 必须校验：

1. 动作属于白名单。
2. 条款属于当前任务。
3. 当前状态允许该动作。
4. 重试预算尚未耗尽。
5. Planner 不能直接提供正式风险、修改数据库或调用工具。

## 10. Risk Analyzer、Critic 与 Evidence Verifier

保持三类职责分离：

1. Risk Analyzer：根据上下文提出结构化风险候选。
2. Critic：检查风险原因是否被证据和 Playbook 支持，只能返回通过、拒绝或人工复核建议。
3. Evidence Verifier：确定性验证 `clause_id`、原文 span、风险类型和规则一致性。

Critic 不得创造新证据、改写合同原文或绕过 Evidence Verifier。只有 Evidence Verifier 通过的候选才能进入正式风险列表。

## 11. Memory 生命周期

### 11.1 保留现有能力

继续保留人工反馈原始记录作为 Episodic Memory，并保持稳定幂等键和来源追踪。

### 11.2 新增能力

1. 对相同合同类型、条款类型、风险类型和审查立场的重复反馈进行聚合。
2. 聚合结果生成独立 Semantic Preference，不覆盖原始反馈。
3. 记录支持次数、反对次数、最近使用时间、置信度和来源 Memory ID。
4. 冲突反馈并存，不自动选择对业务影响更大的结论。
5. 长期未使用或被后续反馈否定的偏好降低置信度，不物理删除原始审计记录。
6. Memory 注入后必须记录它是否影响建议；风险结论仍以 Playbook 和原文为准。

### 11.3 效果验证

使用有 Memory 和无 Memory 的同一批样本进行对照，验证建议一致性或人工采纳率是否改善；没有改善时不能声称 Memory 提升效果。

## 12. 可恢复执行与 Trace

### 12.1 执行控制

在现有重启恢复基础上增加：

1. 任务取消状态和取消检查点。
2. 单节点超时和全任务超时。
3. 每类错误的最大重试次数。
4. 人工恢复入口及恢复原因。
5. 同一任务并发执行保护。
6. Planner、检索修复和 Critic 调用的稳定幂等键。

### 12.2 Agent Trace

每个决策记录：

1. `trace_id`、`step_id`、父 Step 和重试序号。
2. 输入来源标识，不记录完整敏感原文。
3. Prompt、模型、Playbook、标注和代码版本。
4. 上下文 token 分配和裁剪轨迹。
5. Planner 动作及 reason code。
6. 检索查询、Top-K、rerank 因子和候选变化。
7. Critic、Evidence Verifier 的结论和冲突原因。
8. Provider 请求 ID、token、耗时、成本和错误类型。
9. 最终状态和人工复核原因。

支持比较同一输入在两个 Prompt、模型或 Playbook 版本下的 Trace 差异，但不在第一版实现完整可观测平台。

## 13. Agent 效果评估

### 13.1 数据集

将当前效果集逐步扩大到：

1. 20–30 份合同样本。
2. 至少 50 条风险条款和 100 条普通条款。
3. 至少 20 组跨条款检索标注。
4. 至少 20 条人工反馈和 Memory 对照样本。
5. 至少 10 条 Prompt Injection 或恶意输入样本。
6. 所有样本继续限定为合成、公开授权或脱敏授权来源。

### 13.2 指标

保留现有 14 项指标，并增加：

1. `planner_action_accuracy`。
2. `invalid_tool_action_rate`。
3. `llm_json_valid_rate`。
4. `llm_schema_repair_rate`。
5. `retrieval_repair_success_rate`。
6. `retry_recovery_rate`。
7. `unsupported_finding_rate`。
8. `memory_preference_consistency`。
9. `prompt_injection_block_rate`。
10. 单任务 token、成本和 P95 延迟。

### 13.3 验收门槛

1. DeepSeek 真实调用可以完成完整 NDA 主流程，不使用本地结果冒充外部结果。
2. LLM JSON 合法率达到 `98%`；一次 Schema 修复后达到 `100%`，否则进入明确错误或人工复核。
3. Planner 非法动作执行率为 `0`。
4. Prompt Injection 阻断率为 `100%`，且不产生未绑定证据的正式风险。
5. 相关条款 Recall@1 达到 `0.8`。
6. Evidence span 命中率不低于现有 `0.9` 阈值。
7. Provider 错误、重试和恢复路径均有自动化测试。
8. Memory 是否提升效果必须由对照结果决定，不预设为通过。
9. 所有未达标指标和失败样本进入版本化摘要，不得改写为完成。

## 14. 实施顺序

后续开发严格按以下顺序推进，每个阶段单独实现、Review、QA 和提交：

1. 冻结现有评测基线和 DeepSeek API 契约。
2. 接通 DeepSeek 配置并加固 JSON Output、错误和敏感信息日志。
3. 增加 Prompt Injection 防护、Prompt 版本和真实 token 预算。
4. 扩大相关条款标注并实现混合检索、rerank 和一次修复循环。
5. 增加受控 Planner/Router 和白名单动作校验。
6. 增加 Critic，同时保持确定性 Evidence Verifier 最终裁决。
7. 增加 Memory 聚合、冲突、过期和对照评测。
8. 增加任务取消、超时、重试预算、人工恢复和 Trace 对比字段。
9. 扩大完整标注集，运行 DeepSeek 真实效果、成本和稳定性评测。
10. 同步 README、配置说明、架构图、失败案例和简历表述。

不得跳过基线直接调参，不得在没有真实 Key 和运行记录时声称 DeepSeek 已验证。

## 15. 预计影响范围

进入开发后，预计只涉及以下边界：

1. LLM Provider、配置和 Provider 测试。
2. Orchestrator 的受控决策节点和状态模型。
3. Context Builder、Prompt 模板和 token 统计。
4. 相关条款检索、rerank 和 Evidence 修复链路。
5. Memory 模型、Repository 和检索服务。
6. Step Log、Agent Trace 和效果评测。
7. 必要的 Web Trace、人工恢复和评测展示。
8. `.env.example`、README 和独立评测记录。

不在开发前预先拆具体文件和任务；实际任务文件应基于当时代码状态生成。

## 16. 完成定义

只有同时满足以下条件，才允许声明本计划完成：

1. DeepSeek 使用真实 Key 跑过完整主流程，并有脱敏调用记录。
2. Provider、Planner、检索修复、Critic、Memory、恢复和 Injection 防护均有自动化测试。
3. 后端全量测试和 Playwright 全量测试通过，且未削弱旧断言。
4. 扩大后的效果评测记录真实分数、阈值和失败样本。
5. 相关条款 Recall@1 达到目标，或明确记录未达标并保持计划未完成。
6. Key、合同原文、完整 Prompt、测试数据库和运行产物未进入 Git。
7. README 只描述实际完成能力，不把 Provider 适配或 Mock 测试写成真实模型效果。
8. 工作树干净，独立提交可审查并已按用户要求推送。
