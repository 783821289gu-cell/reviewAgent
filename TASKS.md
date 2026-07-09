# TASKS

本文件严格基于 `SPEC.md` 和 `PLAN.md`，用于把 ContractReviewAgent V2 第一版拆成可执行开发任务。

任务按实际开发顺序排列，总数不超过 10 个。

**第一个要做的任务：任务 1。**

## 任务 1：建立项目骨架、基础状态和 Web Workbench 入口

### 目标

建立最小可运行的前后端骨架，打通首页、上传入口、审查立场选择、基础任务状态和 Web Workbench 页面结构。

本任务只建立承载后续能力的壳，不实现合同解析、风险分析、向量检索、Memory、报告或评测。

### 涉及文件

1. `README.md`
2. `.env.example`
3. `.gitignore`
4. `backend/`
5. `backend/app/main.*`
6. `backend/app/config.*`
7. `backend/app/models/`
8. `backend/app/services/`
9. `frontend/`
10. `frontend/src/App.*`
11. `frontend/src/components/UploadPanel.*`
12. `frontend/src/components/WorkbenchLayout.*`

### 验收标准

1. 后端服务可以启动。
2. 前端页面可以启动。
3. 首页展示项目说明、合同上传入口和审查立场选择。
4. 用户未选择审查立场时不能开始审查。
5. Workbench 页面包含顶部、左侧原文区、右侧结果区、底部执行日志区的基础布局。
6. 系统存在基础任务状态模型，至少包含 `START`、`UPLOAD_RECEIVED`、异常提示状态。
7. README 说明本地启动方式和当前任务范围。
8. 不出现合同解析、风险识别、报告导出等后续任务能力。

## 任务 2：实现 DOCX/PDF 上传、文档解析和条款结构化

### 目标

支持 `.docx` 和 `.pdf` 上传解析，通过 `parse_document`、`extract_clauses`、`extract_key_fields` 注册工具将合同转换为可审查的条款级结构，并能在左侧原文区展示条款编号和正文。

### 涉及文件

1. `backend/app/models/contract.*`
2. `backend/app/models/review.*`
3. `backend/app/parsers/`
4. `backend/app/services/document_service.*`
5. `backend/app/services/clause_service.*`
6. `backend/app/services/log_service.*`
7. `backend/app/main.*`
8. `frontend/src/components/UploadPanel.*`
9. `frontend/src/components/ContractViewer.*`
10. `backend/tests/`

### 验收标准

1. `.docx` 文件可以上传并解析出段落和表格文本。
2. `.pdf` 文件可以上传并解析出可读文本。
3. 解析结果包含合同名称、文件类型、内容 hash、原始顺序和可回溯文本位置。
4. 合同内容能被拆分为条款。
5. 每个条款至少包含 `clause_id`、标题或起始文本、正文、条款类型、关键字段、原文位置。
6. 条款类型覆盖 NDA 常见条款：定义、保密义务、使用限制、例外、允许披露、期限、返还销毁、违约责任、争议解决。
7. 关键字段至少覆盖义务主体、权利主体、保密期限、允许披露对象、使用目的、责任范围、违约责任。
8. 左侧原文区可以展示合同原文和条款编号。
9. 解析失败进入 `PARSE_FAILED`，并展示友好错误提示。
10. 文档解析必须通过 `tool_registry["parse_document"]` 调用。
11. 条款抽取必须通过 `tool_registry["extract_clauses"]` 调用。
12. 关键字段抽取必须通过 `tool_registry["extract_key_fields"]` 调用。

## 任务 3：实现 AgentState、显式 Tool Registry、流式状态事件和执行日志

### 目标

建立可追踪审查流程，提供显式 `tool_registry` 字典和工具输入输出契约，让后端以流式事件输出审查节点，前端实时展示状态，并记录执行日志。

本任务要明确：服务模块可以承载工具实现，但 ReviewOrchestratorAgent 只能通过 Tool Registry 调度工具，不能把服务模块当作隐式工具。

### 涉及文件

1. `backend/app/models/review.*`
2. `backend/app/models/log.*`
3. `backend/app/models/tool.*`
4. `backend/app/tools/registry.*`
5. `backend/app/tools/contracts.*`
6. `backend/app/services/review_service.*`
7. `backend/app/services/event_service.*`
8. `backend/app/services/log_service.*`
9. `backend/app/main.*`
10. `frontend/src/components/ReviewProgress.*`
11. `frontend/src/components/ExecutionLog.*`
12. `backend/tests/`

### 验收标准

1. 系统支持正常状态：`START`、`UPLOAD_RECEIVED`、`DOCUMENT_PARSED`、`CLAUSES_STRUCTURED`、`PLAYBOOK_RETRIEVED`、`CONTEXT_BUILT`、`RISK_ANALYZED`、`EVIDENCE_VERIFIED`、`HUMAN_REVIEW_PENDING`、`MEMORY_UPDATED`、`REPORT_READY`。
2. 系统支持异常状态：`PARSE_FAILED`、`RETRIEVAL_FAILED`、`LLM_OUTPUT_INVALID`、`EVIDENCE_MISSING`、`NEED_MANUAL_REVIEW`。
3. 系统存在显式 `tool_registry` 字典。
4. `tool_registry` 至少注册 `parse_document`、`extract_clauses`、`extract_key_fields`、`retrieve_playbook_rules`、`retrieve_related_clauses`、`retrieve_memory`、`analyze_risk`、`verify_evidence`、`generate_revision`、`write_memory`、`generate_report`。
5. 每个注册工具都有明确输入和输出契约。
6. ReviewOrchestratorAgent 只能通过 `tool_registry` 调用工具。
7. 服务模块不能作为绕过 Tool Registry 的隐式工具调度入口。
8. 后端能输出流式事件，前端能实时展示关键节点。
9. 执行日志记录 `task_id`、`step_name`、`tool_name`、`status`、`latency_ms`、`input_summary`、`output_summary`、`token_cost_summary`、`error_message`。
10. 每次工具调用都进入执行日志。
11. 执行日志不记录敏感密钥。
12. 执行日志默认不展示完整 prompt 和完整模型响应。
13. 工具失败不能被记录为成功。
14. 底部执行日志区可查看节点、工具、状态、耗时、摘要和错误。

## 任务 4：实现 NDA Risk Playbook 和规则检索

### 目标

建立本地 YAML/JSON Risk Playbook，覆盖第一版 8 类 NDA 核心风险，并通过 `retrieve_playbook_rules` 注册工具按合同类型、条款类型、关键字段和审查立场检索命中规则。

### 涉及文件

1. `backend/app/playbooks/nda.*`
2. `backend/app/models/playbook.*`
3. `backend/app/services/playbook_service.*`
4. `backend/app/services/review_service.*`
5. `frontend/src/components/RuleDetail.*`
6. `backend/tests/`

### 验收标准

1. Playbook 使用本地 YAML 或 JSON 文件维护。
2. Playbook 只覆盖 NDA 第一版 8 类核心风险。
3. 每条规则包含 `rule_id`、`contract_type`、`clause_type`、适用审查立场、`risk_type`、`check_point`、`risk_criteria`、`severity_default`、`revision_template`。
4. 能基于合同类型、条款类型、关键字段和审查立场检索规则。
5. 命中规则能在风险详情中展示。
6. 未命中规则时，不能强行生成正式风险。
7. Playbook 规则不被 Memory 覆盖。
8. Playbook 检索必须通过 `tool_registry["retrieve_playbook_rules"]` 调用。

## 任务 5：实现相关条款向量检索、rerank 和上下文构建

### 目标

实现当前合同内的相关条款检索，采用向量召回 + 轻量 rerank，并通过 `retrieve_related_clauses`、`retrieve_memory` 等注册工具把当前条款、命中规则、相关条款、Memory 和输出约束组装为风险分析上下文。

### 涉及文件

1. `backend/app/models/retrieval.*`
2. `backend/app/services/embedding_service.*`
3. `backend/app/services/clause_index_service.*`
4. `backend/app/services/rerank_service.*`
5. `backend/app/services/context_builder.*`
6. `backend/app/services/memory_service.*`
7. `backend/app/services/review_service.*`
8. `frontend/src/components/ContextTrace.*`
9. `backend/tests/`

### 验收标准

1. 相关条款检索对象只来自当前合同内条款。
2. 不检索互联网内容。
3. 不做外部法务知识库。
4. 向量召回输入包含当前条款正文、条款类型、风险类型、Playbook 检查点。
5. rerank 至少结合向量相似度、条款类型相关性、定义/例外/期限/责任/违约等关键条款权重、风险类型固定关联、关键字段命中情况。
6. rerank 不依赖独立训练模型。
7. 能选出少量相关条款进入上下文。
8. 上下文包含当前条款、条款类型、关键字段、审查立场、命中 Playbook Rule、相关条款、相关 Memory、输出结构约束、证据约束。
9. 超限时优先减少 Memory，再减少低相关条款，不删除当前条款、命中规则、输出格式和证据约束。
10. 执行日志能记录相关条款召回和 rerank 摘要。
11. 相关条款检索必须通过 `tool_registry["retrieve_related_clauses"]` 调用。
12. Memory 召回必须通过 `tool_registry["retrieve_memory"]` 调用。

## 任务 6：实现 LLM 风险分析、结构化输出校验和证据验证

### 目标

通过 `analyze_risk`、`verify_evidence` 和 `generate_revision` 注册工具完成 NDA 条款风险分析、结构化输出校验、证据验证和修改建议生成。

### 涉及文件

1. `.env.example`
2. `backend/app/config.*`
3. `backend/app/models/risk.*`
4. `backend/app/services/llm_service.*`
5. `backend/app/services/risk_analyzer.*`
6. `backend/app/services/evidence_service.*`
7. `backend/app/services/review_service.*`
8. `frontend/src/components/RiskList.*`
9. `frontend/src/components/RiskDetail.*`
10. `backend/tests/`

### 验收标准

1. LLM 风险分析输出结构化结果，不能只返回自然语言。
2. 风险结果包含 `risk_type`、`severity`、`confidence`、`risk_reason`、`clause_id`、`evidence_text`、`matched_rule_ids`、`revision_suggestion`、`review_status`。
3. 风险类型只能属于第一版 8 类核心风险。
4. 风险等级只能是高 / 中 / 低。
5. 输出结构不合法时自动重试一次。
6. 重试仍失败时进入 `LLM_OUTPUT_INVALID` 或 `NEED_MANUAL_REVIEW`，不进入正式风险列表。
7. 证据验证确认 `clause_id` 有效，`evidence_text` 能在对应条款正文中找到。
8. 风险类型与命中规则匹配。
9. 证据缺失进入 `EVIDENCE_MISSING` 或人工复核。
10. 无证据风险不能展示为正式风险。
11. 风险分析必须通过 `tool_registry["analyze_risk"]` 调用。
12. 证据验证必须通过 `tool_registry["verify_evidence"]` 调用。
13. 修改建议生成必须通过 `tool_registry["generate_revision"]` 调用。

## 任务 7：实现 Web 风险展示、原文高亮、跳转和局部审查

### 目标

完成 Workbench 中用户可见的风险审查体验：风险列表、规则详情、证据文本、修改建议、原文定位、高亮、框选局部审查。

### 涉及文件

1. `frontend/src/components/WorkbenchLayout.*`
2. `frontend/src/components/ContractViewer.*`
3. `frontend/src/components/RiskList.*`
4. `frontend/src/components/RiskDetail.*`
5. `frontend/src/components/RuleDetail.*`
6. `frontend/src/components/LocalReviewPanel.*`
7. `backend/app/services/local_review_service.*`
8. `backend/app/main.*`
9. `backend/tests/`

### 验收标准

1. 顶部展示合同名称、审查状态、Playbook 版本、报告导出入口。
2. 左侧展示合同原文和条款编号。
3. 右侧展示风险列表、命中规则、证据文本、修改建议和人工反馈入口。
4. 点击风险能跳转到对应条款。
5. 正式风险能定位或高亮对应证据文本。
6. 没有证据的判断不能显示为确定风险。
7. 用户可以框选原文片段发起局部审查。
8. 局部审查结果与正式风险区分展示。
9. 错误提示对用户友好。

## 任务 8：实现人工复核、反馈动作和 SQLite Memory

### 目标

支持人工复核闭环，记录采纳、忽略、修改等级、修改建议、局部重审、加入报告、写入偏好记忆，并让后续相似审查能检索 Memory。

### 涉及文件

1. `backend/app/db/`
2. `backend/app/models/feedback.*`
3. `backend/app/models/memory.*`
4. `backend/app/services/feedback_service.*`
5. `backend/app/services/memory_service.*`
6. `backend/app/services/review_service.*`
7. `frontend/src/components/ReviewActions.*`
8. `frontend/src/components/RiskEditor.*`
9. `frontend/src/components/MemoryTrace.*`
10. `backend/tests/`

### 验收标准

1. 高风险进入人工复核。
2. 低置信度进入人工复核。
3. 证据缺失进入人工复核或异常状态。
4. 用户可以采纳风险。
5. 用户可以忽略风险。
6. 用户可以修改风险等级和修改建议。
7. 用户可以要求局部重审。
8. 用户可以选择是否加入报告。
9. 所有采纳、忽略、修改动作写入 SQLite Memory。
10. Memory 记录包含合同类型、条款类型、风险类型、用户动作、修改前后等级或建议、忽略原因、来源风险 ID、创建时间。
11. 后续相似审查能按合同类型、条款类型、风险类型检索相关 Memory。
12. 当 Memory 影响建议时，风险详情能说明参考了历史反馈。
13. Memory 不直接生成正式风险，不覆盖 Playbook 和原文证据。
14. Memory 写入必须通过 `tool_registry["write_memory"]` 调用。

## 任务 9：实现 Markdown 报告导出、10 份 NDA 样本和基础评测摘要

### 目标

完成第一版闭环交付物：Markdown 审查报告、10 份 NDA 样本、手动触发基础评测和评测摘要。

### 涉及文件

1. `backend/app/services/report_service.*`
2. `backend/app/services/evaluation_service.*`
3. `backend/app/reports/`
4. `samples/`
5. `samples/README.md`
6. `evaluation/`
7. `frontend/src/components/ReportExport.*`
8. `frontend/src/components/EvaluationPanel.*`
9. `README.md`
10. `backend/tests/`

### 验收标准

1. 用户可以导出 Markdown 审查报告。
2. 报告包含合同名称、审查立场、Playbook 版本、风险清单、风险等级、风险原因、原文证据、修改建议、人工反馈状态。
3. 报告只导出用户允许进入报告的风险。
4. 被忽略且未加入报告的风险不导出。
5. 待复核但未确认的风险不作为确定风险导出。
6. 报告不包含完整执行日志和技术实现细节。
7. 准备 10 份 NDA 样本，样本来源必须可说明，不使用未授权真实客户合同。
8. 支持手动触发基础评测。
9. 评测摘要包含样本名称、任务是否跑通、文档是否解析成功、条款是否结构化成功、Playbook 是否命中、风险是否绑定原文证据、人工反馈是否能写入 Memory、报告是否导出成功、失败原因。
10. 基础评测不声称生产级准确率。
11. 至少一次完整演示能覆盖上传、解析、条款结构化、Playbook 检索、相关条款检索、上下文构建、风险分析、证据验证、人工反馈、Memory、报告导出、执行日志、基础评测。
12. 报告生成必须通过 `tool_registry["generate_report"]` 调用。
