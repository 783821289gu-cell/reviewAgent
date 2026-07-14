# PLAN

## 1. 方案目标

本文件基于 `SPEC.md`，用于约束 ContractReviewAgent V2 第一版的技术实现方向。

本文件只描述技术方案和边界，不写代码，不拆开发任务，不定义阶段计划。

第一版目标是完成一个可演示、可解释、可追踪、可复核的 NDA 合同审查 Agent 闭环。技术实现必须服务于 `SPEC.md` 中的需求，不扩展到企业级法务系统、复杂协同系统或生产级平台。

## 2. 已确认技术决策

| 决策项 | 第一版选择 |
|---|---|
| 文件上传范围 | 支持 `.docx` 和 `.pdf` |
| 原文定位精度 | `clause_id + evidence_text` |
| 审查执行方式 | 后端流式事件输出，前端实时展示关键节点 |
| Tool Registry | 显式 `tool_registry` 字典 + 工具输入输出契约 |
| Risk Playbook | 本地 YAML / JSON 文件 |
| 相关条款检索 | 向量检索 + rerank |
| Memory 落地方式 | SQLite 结构化存储 |
| LLM 输出失败处理 | 自动重试一次，仍失败则进入人工复核或异常状态 |
| 执行日志粒度 | 节点、工具名、状态、耗时、错误、输入输出摘要、token/cost 摘要 |
| 报告格式 | Markdown |
| 基础评测方式 | 10 份 NDA 样本手动触发评测并生成评测摘要 |

## 3. 系统边界

第一版只围绕 NDA / 保密协议审查展开。

必须覆盖：

1. DOCX / PDF 上传。
2. 文档解析。
3. 条款结构化。
4. Playbook 检索。
5. 相关条款检索。
6. 动态上下文构建。
7. LLM 风险分析。
8. 证据验证。
9. 原文定位和高亮。
10. 人工复核。
11. Memory 写入和召回。
12. Markdown 报告导出。
13. Agent 执行日志。
14. 10 份 NDA 样本基础评测。

明确不做：

1. 不支持 NDA 之外的正式合同审查。
2. 不做多租户、审批流、复杂权限。
3. 不做规则管理后台。
4. 不做生产级可观测平台。
5. 不做多种报告格式。
6. 不做模型训练或微调。
7. 不做多个虚名 Agent 互相聊天。
8. 不让 Memory 替代 Playbook 或合同原文证据。

## 4. 总体形态

第一版采用一个轻量 Web Workbench 加一个后端审查服务。

Web Workbench 负责用户可见流程：

1. 上传合同。
2. 选择审查立场。
3. 查看合同原文和条款编号。
4. 查看流式审查进度。
5. 查看风险、证据、命中规则和修改建议。
6. 执行采纳、忽略、修改、加入报告、写入偏好记忆等反馈动作。
7. 框选原文发起局部审查。
8. 查看 Agent 执行日志。
9. 导出 Markdown 报告。
10. 查看基础评测摘要。

后端审查服务负责 Agent 闭环：

1. 管理审查任务状态。
2. 通过显式 `tool_registry` 调用文档解析、条款结构化、Playbook 检索、相关条款检索、Memory 检索、风险分析、证据验证、报告生成等工具。
3. 维护 AgentState。
4. 输出流式事件。
5. 记录执行日志。
6. 保存人工反馈和 Memory。

第一版只需要单主控 Agent。工具可以独立封装，但不需要拆成多个互相对话的 Agent。

服务模块可以实现工具内部逻辑，但主控 Agent 的调度入口只能是 `tool_registry`，不能把服务模块当作隐式工具调用面。

## 5. Tool Registry 方案

第一版采用显式 Tool Registry。

ReviewOrchestratorAgent 只能通过 `tool_registry` 调用工具。所有工具必须统一注册、统一记录日志、统一处理失败结果。

```python
tool_registry = {
    "parse_document": parse_document,
    "extract_clauses": extract_clauses,
    "extract_key_fields": extract_key_fields,
    "retrieve_playbook_rules": retrieve_playbook_rules,
    "retrieve_related_clauses": retrieve_related_clauses,
    "retrieve_memory": retrieve_memory,
    "analyze_risk": analyze_risk,
    "verify_evidence": verify_evidence,
    "generate_revision": generate_revision,
    "write_memory": write_memory,
    "generate_report": generate_report
}
```

核心工具输入输出契约：

| 工具 | 输入 | 输出 | 是否调用 LLM |
|---|---|---|---|
| `parse_document` | file_id | paragraphs, tables, page_map | 否 |
| `extract_clauses` | document | clauses | 可选 |
| `extract_key_fields` | clause | key_fields | 是 |
| `retrieve_playbook_rules` | contract_type, clause_type, key_fields, review_position | matched_rules | 否 |
| `retrieve_related_clauses` | contract_type, current_clause, clauses, risk_type, playbook_check_point, limit | related_clauses | 否 |
| `retrieve_memory` | contract_type, clause, risk_type, review_position, memory_items, limit | memories | 否 |
| `analyze_risk` | review_context | risk_finding | 是 |
| `verify_evidence` | finding, clauses, matched_rule | citation_status | 可选 |
| `generate_revision` | finding, preferred_position | revision_suggestion | 是 |
| `write_memory` | human_feedback | memory_item | 可选 |
| `generate_report` | task_id | report_file | 可选 |

Tool Registry 边界：

1. 服务模块是工具实现，不是 Agent 调度接口。
2. 主控 Agent 不直接调用底层服务模块。
3. 每个工具必须有明确输入和输出。
4. 每次工具调用必须进入 StepLog。
5. 工具失败必须返回失败状态或抛出可记录错误。
6. LLM 工具必须输出结构化结果。
7. 基础评测可以调用已注册工具完成审查链路验证，但不作为核心审查工具注册进 `tool_registry`。

## 6. 审查状态流

系统应围绕 `SPEC.md` 定义的状态流实现：

```text
START
→ UPLOAD_RECEIVED
→ DOCUMENT_PARSED
→ CLAUSES_STRUCTURED
→ PLAYBOOK_RETRIEVED
→ CONTEXT_BUILT
→ RISK_ANALYZED
→ EVIDENCE_VERIFIED
→ HUMAN_REVIEW_PENDING
→ MEMORY_UPDATED
→ REPORT_READY
```

异常状态包括：

```text
PARSE_FAILED
RETRIEVAL_FAILED
LLM_OUTPUT_INVALID
EVIDENCE_MISSING
NEED_MANUAL_REVIEW
```

状态流要求：

1. 每个关键状态都要能进入执行日志。
2. 每个关键状态都要能通过流式事件通知前端。
3. 异常状态必须有用户可理解的提示。
4. 工具失败不能被包装成成功。
5. 状态恢复不追求企业级长任务恢复，但同一次审查过程中的中间状态必须可追踪。

## 7. 文件解析方案

第一版支持 `.docx` 和 `.pdf`。

解析输出统一为内部文档结构，至少包含：

1. `contract_id`。
2. `file_name`。
3. `file_type`。
4. `content_hash`。
5. 段落文本。
6. 表格文本。
7. 原始顺序。
8. 可回溯文本位置。

解析边界：

1. DOCX 需要保留段落和表格文本。
2. PDF 需要提取可读文本。
3. 第一版不追求复杂版式还原。
4. 第一版不要求页眉页脚、批注、修订痕迹的完整解析。
5. 解析失败进入 `PARSE_FAILED`。

## 8. 条款结构化方案

解析后的文档必须被转换为条款级结构。

每个 Clause 至少包含：

1. `clause_id`。
2. `title`。
3. `text`。
4. `clause_type`。
5. `key_fields`。
6. `source_location`。

条款结构化要求：

1. `clause_id` 必须稳定、可展示、可被风险引用。
2. 条款正文必须可用于证据校验。
3. 条款类型必须覆盖 NDA 常见条款。
4. 关键字段至少覆盖义务主体、权利主体、保密期限、允许披露对象、使用目的、责任范围、违约责任。
5. 条款结构化结果必须支持原文跳转。

第一版不要求把所有非核心条款都完美分类，但与 8 类核心风险有关的条款必须能被识别或进入人工复核。

## 9. Risk Playbook 方案

Risk Playbook 使用本地 YAML / JSON 文件维护。

每条规则至少包含：

1. `rule_id`。
2. `contract_type`。
3. `clause_type`。
4. `preferred_position` 或适用审查立场。
5. `risk_type`。
6. `check_point`。
7. `risk_criteria`。
8. `severity_default`。
9. `revision_template`。

Playbook 只覆盖第一版 8 类核心 NDA 风险：

1. 保密信息范围过宽。
2. 缺少保密信息例外。
3. 保密期限不合理。
4. 使用目的或使用限制不清。
5. 允许披露对象过宽。
6. 返还或销毁义务不明确。
7. 责任无限或责任边界不清。
8. 违约责任或违约金明显不合理。

Playbook 检索要求：

1. 必须基于合同类型、条款类型、关键字段和审查立场进行筛选。
2. 命中规则必须能在风险详情中展示。
3. 未命中规则时，不能强行生成正式风险。
4. Playbook 是判断主依据之一，Memory 不能覆盖 Playbook。

## 10. 相关条款检索方案

第一版采用向量检索 + rerank。

检索对象为当前合同内的条款，而不是外部法务知识库。

检索流程：

```text
当前条款
→ 构造查询文本
→ 向量召回候选相关条款
→ 结合规则和元数据 rerank
→ 选出少量相关条款进入上下文
```

向量召回输入应包含：

1. 当前条款正文。
2. 当前条款类型。
3. 当前风险类型。
4. 命中 Playbook 检查点。

rerank 因子至少包含：

1. 向量相似度。
2. 条款类型相关性。
3. 是否属于定义、例外、期限、责任、违约等关键条款。
4. 是否与当前风险类型存在固定关联。
5. 是否包含关键字段。

V1 rerank 不需要独立训练模型。优先采用“向量相似度 + 规则权重”的轻量重排方式。

相关条款检索边界：

1. 不检索互联网内容。
2. 不做完整法务知识图谱。
3. 不要求跨合同全局检索。
4. 不把相关条款本身输出为风险。
5. 只把相关条款作为上下文依据。

## 11. Memory 方案

Memory 使用 SQLite 结构化存储。

第一版记录所有采纳、忽略、修改动作。

Memory Item 至少包含：

1. `memory_id`。
2. `memory_type`。
3. `contract_type`。
4. `clause_type`。
5. `risk_type`。
6. `user_action`。
7. `original_severity`。
8. `final_severity`。
9. `original_suggestion`。
10. `final_suggestion`。
11. `ignore_reason`。
12. `source_finding_id`。
13. `created_at`。

Memory 使用方式：

1. 审查相似条款时，根据合同类型、条款类型、风险类型检索相关 Memory。
2. 检索到的 Memory 作为上下文补充。
3. 当 Memory 影响修改建议时，风险详情应能说明参考了历史反馈。
4. Memory 不得直接生成正式风险。
5. Memory 不得覆盖 Playbook 或合同原文证据。

第一版不做向量 Memory、不做跨用户画像、不做复杂记忆合并。

## 12. 上下文构建方案

ReviewContextBuilder 负责在每次风险分析前组装最小必要上下文。

上下文必须包含：

1. 当前条款原文。
2. 当前条款类型。
3. 当前条款关键字段。
4. 审查立场。
5. 命中的 Playbook Rule。
6. 相关条款。
7. 相关 Memory。
8. 输出结构约束。
9. 证据约束。

上下文优先级：

| 优先级 | 内容 |
|---|---|
| P0 | 当前条款、审查立场、命中规则、输出格式、证据约束 |
| P1 | 相关条款、关键字段 |
| P2 | Memory、历史反馈 |
| P3 | 合同整体摘要 |

超限时处理：

1. 先减少 Memory。
2. 再减少低相关相关条款。
3. 保留当前条款。
4. 保留命中规则。
5. 保留输出格式和证据约束。

## 13. LLM 风险分析方案

LLM 风险分析必须输出结构化结果。

每条风险结果至少包含：

1. `risk_type`。
2. `severity`。
3. `confidence`。
4. `risk_reason`。
5. `clause_id`。
6. `evidence_text`。
7. `matched_rule_ids`。
8. `revision_suggestion`。
9. `review_status`。

LLM 输出约束：

1. 不能只返回自然语言。
2. 风险类型必须属于第一版 8 类核心风险。
3. 风险等级只能是高 / 中 / 低。
4. 必须给出命中规则。
5. 必须给出证据文本。

失败处理：

1. 如果输出结构不合法，自动重试一次。
2. 重试仍失败，进入 `LLM_OUTPUT_INVALID` 或 `NEED_MANUAL_REVIEW`。
3. 不伪造结构化输出。
4. 不把失败结果写入正式风险列表。

## 14. 证据验证方案

证据验证使用 `clause_id + evidence_text`。

正式风险必须通过以下校验：

1. `clause_id` 存在。
2. `clause_id` 指向当前合同中的有效条款。
3. `evidence_text` 能在对应条款正文中找到。
4. 风险类型与命中规则匹配。
5. 风险原因与证据文本相关。

校验失败处理：

1. 不进入正式风险列表。
2. 进入人工复核或 `EVIDENCE_MISSING`。
3. 在执行日志中记录失败原因。
4. 前端展示用户可理解的复核提示。

第一版不要求 span offset 精确定位，但必须能跳转到对应条款并展示证据文本。

## 15. 人工复核和反馈方案

以下结果必须进入人工复核：

1. 高风险。
2. 低置信度。
3. 证据缺失。
4. Risk Agent 与 Evidence Verifier 结论冲突。
5. 框选局部审查结果。
6. LLM 输出无法恢复。

人工反馈动作：

1. 采纳风险。
2. 忽略风险。
3. 修改风险等级。
4. 修改建议文本。
5. 要求局部重审。
6. 加入报告。
7. 写入偏好记忆。

反馈处理规则：

1. 所有采纳、忽略、修改动作都写入 Memory。
2. 被忽略的正式风险默认不进入报告，除非用户明确加入。
3. 待复核但未处理的风险不应被包装为已确认结论。
4. 用户修改后的等级和建议应覆盖当前报告中的对应字段。

## 16. Web Workbench 方案

Web Workbench 页面结构：

1. 顶部：合同名称、审查状态、Playbook 版本、报告导出入口。
2. 左侧：合同原文、条款编号、风险定位、高亮、框选入口。
3. 右侧：风险列表、命中规则、证据文本、修改建议、人工反馈操作。
4. 底部：Agent 执行记录。

流式事件展示至少覆盖：

1. 上传已接收。
2. 文档解析中 / 完成。
3. 条款结构化中 / 完成。
4. Playbook 检索中 / 完成。
5. 上下文构建中 / 完成。
6. 风险分析中 / 完成。
7. 证据验证中 / 完成。
8. 等待人工复核。
9. Memory 已更新。
10. 报告已生成。

前端显示原则：

1. 用户可见状态必须真实反映后端状态。
2. 不展示没有证据的确定风险。
3. 错误提示要友好。
4. 执行日志默认不压过核心审查结果。
5. 框选局部审查结果应与正式风险区分。

## 17. 执行日志方案

执行日志用于证明 Agent 过程可追踪。

每个 StepLog 至少包含：

1. `task_id`。
2. `step_name`。
3. `tool_name`。
4. `status`。
5. `latency_ms`。
6. `input_summary`。
7. `output_summary`。
8. `token_cost_summary`。
9. `error_message`。

日志边界：

1. 不默认展示完整 prompt。
2. 不默认展示完整模型响应。
3. 不记录敏感密钥。
4. 不做生产级链路追踪。
5. 不隐藏失败步骤。

## 18. Markdown 报告方案

报告格式为 Markdown。

报告至少包含：

1. 合同名称。
2. 审查立场。
3. Playbook 版本。
4. 风险清单。
5. 风险等级。
6. 风险原因。
7. 原文证据。
8. 修改建议。
9. 人工反馈状态。

报告生成规则：

1. 只导出用户允许进入报告的风险。
2. 被忽略且未加入报告的风险不导出。
3. 待复核但未确认的风险不应作为确定风险导出。
4. 报告不包含完整执行日志。
5. 报告不包含技术实现细节。

## 19. 基础评测方案

第一版使用 10 份 NDA 样本手动触发评测并生成评测摘要。

评测输入：

1. 10 份 NDA 样本。
2. 每份样本的审查立场。
3. 可选人工标注或人工观察记录。

评测摘要至少包含：

1. 样本名称。
2. 任务是否跑通。
3. 文档是否解析成功。
4. 条款是否结构化成功。
5. Playbook 是否命中。
6. 风险是否绑定原文证据。
7. 人工反馈是否能写入 Memory。
8. 报告是否导出成功。
9. 失败原因。

评测边界：

1. 不追求大规模自动评测。
2. 不声称达到生产级准确率。
3. 不使用未授权真实客户合同。
4. 样本来源必须可说明。

## 20. 数据持久化边界

第一版需要持久化：

1. 审查任务状态。
2. 合同元数据。
3. 条款结构。
4. 风险结果。
5. 人工反馈。
6. Memory。
7. 执行日志。
8. 报告文件或报告内容。
9. 评测摘要。

持久化目标是支持演示、复核和基础评测，不是支持多租户生产运营。

文件、Playbook、报告、样本和评测摘要可以用本地文件组织。Memory 使用 SQLite。向量检索所需索引可以随合同任务生成，第一版不要求独立向量服务。

## 21. 验证口径

第一版完成时，至少应能验证：

1. `.docx` 和 `.pdf` 都能上传。
2. 用户未选择审查立场时不能开始审查。
3. 审查状态能通过流式事件展示。
4. 合同能形成条款结构。
5. 存在显式 `tool_registry` 字典。
6. 主控 Agent 只能通过 `tool_registry` 调用工具。
7. 每个注册工具都有明确输入输出。
8. Playbook 能检索并展示命中规则。
9. 相关条款检索使用向量召回和 rerank。
10. 风险分析输出结构化结果。
11. LLM 输出失败会重试一次。
12. 证据验证使用 `clause_id + evidence_text`。
13. 无证据风险不会进入正式风险列表。
14. 高风险、低置信度、证据缺失会进入人工复核。
15. 采纳、忽略、修改动作会写入 SQLite Memory。
16. Markdown 报告能导出。
17. 执行日志能看到节点、工具、状态、耗时、摘要、错误和 token/cost 摘要。
18. 10 份 NDA 样本能生成基础评测摘要。

没有完成验证的能力不能声明完成。
