# ContractReviewAgent V2：Agent 研发岗导向的简历项目版方案设计

## 1. 项目定位

### 1.1 项目名称

**ContractReviewAgent V2：基于 Agentic RAG 与上下文工程的合同审查 Agent**

### 1.2 一句话定位

一个面向合同初审场景的可追踪 Agent 系统：将合同审查拆解为文档解析、条款结构化、Risk Playbook 检索、上下文构建、风险推理、证据定位、人工反馈和报告生成等步骤，并通过工具调用、记忆回流和评估指标证明 Agent 的稳定性。

### 1.3 简历项目目标

这个项目不是主打“做了一个合同审查网站”，而是主打“做了一个能落地业务场景的 Agent 应用系统”。

核心展示能力：

- Agent 工作流编排：把复杂合同审查任务拆成可执行、可恢复、可观测的 Agent 状态流。
- Tool Calling：将文档解析、条款切分、规则检索、证据定位、报告导出封装为可控工具。
- Agentic RAG：不是固定 chunk 问答，而是条款级结构化检索、Playbook 召回、相关条款召回和证据验证。
- Memory：把人工复核、历史风险判断、常用修改偏好沉淀为可检索记忆。
- Context Engineering：按任务动态组装上下文，控制 token 预算，减少无关上下文干扰。
- Evaluation：用可量化指标评估工具调用、RAG 命中、风险识别、证据定位和人工采纳率。

### 1.4 面向岗位

项目主要服务于 **AI Agent 研发工程师 / 大模型应用开发工程师 / AI 应用研发工程师 / Agent 平台方向** 等岗位，尤其针对杭州大厂常见要求：

- 熟悉 Agent 架构、流程编排、工具调用；
- 熟悉 Prompt、Function Calling、RAG、Memory、上下文工程；
- 能把 LLM 能力落到真实业务流程；
- 具备 Python/Java/Go 后端工程能力；
- 能做效果评估、日志追踪、问题定位和持续优化；
- 能快速做出可演示 MVP，而不是停留在概念设计。

---

## 2. 项目边界

### 2.1 第一版必须落地

第一版围绕“可演示、可解释、可追问”的闭环实现：

```text
上传合同
→ 文档解析
→ 条款级结构化
→ Risk Playbook 检索
→ Review Agent 风险分析
→ Evidence Verifier 证据定位
→ Web 原文高亮
→ 人工反馈
→ Memory 写入
→ 报告导出
```

必须实现：

- DOCX/PDF 上传解析；
- 条款边界识别和 `clause_id` 生成；
- 条款类型分类和关键字段抽取；
- 结构化 Risk Playbook；
- 条款级规则检索和相关条款检索；
- Agent 动态上下文构建；
- 风险识别、风险等级、风险原因、修改建议；
- 风险结论绑定原文证据；
- 人工采纳、忽略、修改反馈；
- 简单记忆回流；
- 审查报告导出；
- Agent 执行日志和基础评测。

### 2.2 第一版不做

简历项目不追求企业级大而全：

- 不做复杂在线协同编辑；
- 不做复杂企业权限、多租户、审批流；
- 不做完整法务知识图谱；
- 不做 Agentic RL 或模型微调；
- 不堆十几个虚名 Agent；
- 不做多种导出格式；
- 不做全量生产级可观测平台；
- 不做大量合同类型，优先聚焦 NDA、采购合同或服务合同中的一种。

---

## 3. 总体架构

### 3.1 架构原则

采用 **单主控 Agent + 工具注册表 + 上下文构建器 + 结构化状态**。

第一版不盲目做复杂 Multi-Agent。更适合简历和面试的方案是：

- 一个 `ReviewOrchestratorAgent` 负责规划、调度、状态流转；
- 多个工具负责确定性任务；
- 少量专用子能力负责风险推理、证据验证和报告生成；
- 所有中间结果写入 `AgentState`；
- 每一步都能追踪输入、输出、耗时、错误和 token 成本。

### 3.2 核心组件

| 组件 | 职责 | 简历体现能力 |
|---|---|---|
| ReviewOrchestratorAgent | 管理合同审查状态图，决定下一步工具调用 | Agent 编排、任务拆解 |
| Tool Registry | 注册和统一调用解析、检索、定位、导出等工具 | Function Calling、工具治理 |
| AgentState | 保存任务状态、合同结构、检索结果、风险结论和日志 | 状态管理、长任务工程 |
| ReviewContextBuilder | 为每次 LLM 调用动态拼装最小必要上下文 | 上下文工程、token 控制 |
| Clause-level RAG | 基于条款和规则进行检索增强 | RAG 优化、结构化检索 |
| Memory Store | 保存人工反馈和历史审查经验 | Agent Memory、反馈闭环 |
| Evidence Verifier | 校验风险结论能否回到合同原文 | Grounding、引用验证 |
| Web Workbench | 展示原文、风险、证据和人工反馈入口 | 产品化落地 |

### 3.3 审查状态图

```text
START
  ↓
UPLOAD_RECEIVED
  ↓
DOCUMENT_PARSED
  ↓
CLAUSES_STRUCTURED
  ↓
PLAYBOOK_RETRIEVED
  ↓
CONTEXT_BUILT
  ↓
RISK_ANALYZED
  ↓
EVIDENCE_VERIFIED
  ↓
HUMAN_REVIEW_PENDING
  ↓
MEMORY_UPDATED
  ↓
REPORT_READY
```

异常状态：

```text
PARSE_FAILED
RETRIEVAL_FAILED
LLM_OUTPUT_INVALID
EVIDENCE_MISSING
NEED_MANUAL_REVIEW
```

---

## 4. Agent 设计

### 4.1 ReviewOrchestratorAgent

主控 Agent 不直接“读完整合同然后自由回答”，而是根据状态图调度工具和子任务。

职责：

- 接收 `task_id` 和合同文件；
- 判断当前任务状态；
- 调用对应工具；
- 维护 `AgentState`；
- 在低置信度、规则冲突、证据缺失时进入人工复核；
- 将人工反馈写入 Memory；
- 触发报告生成。

核心伪代码：

```python
class ReviewOrchestratorAgent:
    def run(self, task_id: str):
        state = load_agent_state(task_id)

        if state.status == "UPLOAD_RECEIVED":
            state.document = parse_document_tool(state.file_id)

        if state.status == "DOCUMENT_PARSED":
            state.clauses = extract_clauses_tool(state.document)

        for clause in state.pending_clauses:
            rules = retrieve_playbook_tool(clause)
            memories = retrieve_memory_tool(clause)
            context = build_review_context(clause, rules, memories, state)
            finding = analyze_risk_tool(context)
            verified = verify_evidence_tool(finding, clause)
            state.add_finding(verified)

        if state.has_uncertain_findings():
            state.status = "HUMAN_REVIEW_PENDING"
        else:
            generate_report_tool(state.task_id)
```

### 4.2 AgentState

`AgentState` 是项目里最重要的数据结构之一。它让 Agent 能处理长任务，而不是一次性聊天。

```json
{
  "task_id": "task_001",
  "contract_id": "contract_001",
  "status": "RISK_ANALYZED",
  "playbook_version": "nda_v1",
  "current_step": "verify_evidence",
  "document": {
    "file_type": "docx",
    "content_hash": "sha256..."
  },
  "clauses": [],
  "matched_rules": [],
  "retrieved_memories": [],
  "risk_findings": [],
  "human_feedback": [],
  "step_logs": []
}
```

### 4.3 Agent 设计取舍

第一版不建议写成很多独立 Agent 互相聊天。更推荐：

```text
一个主控 Agent
多个确定性工具
少量 LLM 推理节点
结构化状态传递
人工复核断点
```

这样更符合简历项目：

- 复杂度可控；
- 代码能落地；
- 面试时能讲清楚每一步；
- 能体现 Agent 架构能力，而不是概念堆砌。

---

## 5. 工具调用设计

### 5.1 Tool Registry

所有工具统一注册，主控 Agent 只能通过工具接口访问外部能力。

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

### 5.2 核心工具

| 工具 | 输入 | 输出 | 是否调用 LLM |
|---|---|---|---|
| `parse_document` | file_id | paragraphs, tables, page_map | 否 |
| `extract_clauses` | document | clauses | 可选 |
| `extract_key_fields` | clause | key_fields | 是 |
| `retrieve_playbook_rules` | clause_type, key_fields | matched_rules | 否 |
| `retrieve_related_clauses` | clause_id | related_clauses | 否 |
| `retrieve_memory` | clause, risk_type | memories | 否 |
| `analyze_risk` | review_context | risk_finding | 是 |
| `verify_evidence` | finding, source_text | citation_status | 可选 |
| `generate_revision` | finding, preferred_position | revision_suggestion | 是 |
| `write_memory` | human_feedback | memory_item | 可选 |
| `generate_report` | task_id | report_file | 可选 |

### 5.3 工具输出约束

LLM 工具必须返回结构化 JSON，不能只返回自然语言。

```json
{
  "risk_type": "liability_unlimited",
  "severity": "high",
  "confidence": 0.86,
  "reason": "责任上限缺失，可能导致乙方承担无限责任。",
  "evidence_text": "乙方应赔偿甲方因此遭受的全部损失。",
  "source_location": {
    "clause_id": "C-008",
    "paragraph_id": "P-041",
    "start_offset": 12,
    "end_offset": 31
  },
  "matched_rule_ids": ["NDA-LIABILITY-001"],
  "revision_suggestion": "建议增加责任上限，例如不超过本合同项下已支付费用总额。"
}
```

---

## 6. Agentic RAG 设计

### 6.1 为什么不用普通 Chunk RAG

合同审查不适合简单按固定长度切 chunk：

- 条款边界被切断会导致责任主体、条件和例外条款丢失；
- 风险判断依赖条款类型、相关条款和合同角色；
- 法务结论必须能回到原文证据；
- 规则库不是普通知识文档，而是结构化 Risk Playbook。

因此第一版采用 **条款级结构化 RAG**。

### 6.2 检索对象

系统维护三类可检索对象：

| 对象 | 用途 |
|---|---|
| Clause Index | 检索合同内相关条款 |
| Risk Playbook Index | 检索审查规则和修改模板 |
| Memory Index | 检索历史人工反馈和项目偏好 |

### 6.3 检索流程

```text
当前 clause
  ↓
clause_type 预筛选
  ↓
Playbook Rule 召回
  ↓
相关条款召回
  ↓
历史 Memory 召回
  ↓
rerank
  ↓
ReviewContextBuilder 组装上下文
  ↓
Risk Analyzer 生成结论
  ↓
Evidence Verifier 校验原文依据
```

### 6.4 Risk Playbook Rule

```json
{
  "rule_id": "NDA-CONF-001",
  "playbook_version": "nda_v1",
  "contract_type": "NDA",
  "clause_type": "confidentiality",
  "risk_type": "overbroad_confidentiality",
  "check_point": "保密信息范围是否过宽，是否缺少例外情形。",
  "risk_criteria": "若保密信息包含所有口头、书面、历史和未来信息，且无公开信息、已知信息、独立开发等例外，则标记为中高风险。",
  "severity_default": "medium",
  "preferred_position": "recipient",
  "revision_template": "建议补充保密信息例外，包括已公开、接收方已知、第三方合法取得、独立开发的信息。"
}
```

### 6.5 RAG 优化点

简历中可以突出这些优化：

- clause-boundary segmentation：条款边界优先，不用固定 chunk；
- hybrid retrieval：`clause_type` 过滤 + 向量召回 + 关键词召回；
- rerank：按规则适用性、条款类型、证据覆盖度排序；
- citation verification：风险结论必须绑定 `clause_id + evidence_text + span_offset`；
- context compression：只把当前风险判断需要的信息放进上下文。

---

## 7. Memory 设计

### 7.1 Memory 类型

第一版只做能落地的三类记忆：

| Memory | 内容 | 示例 |
|---|---|---|
| Working Memory | 当前审查任务状态 | 当前合同、当前条款、已命中规则 |
| Episodic Memory | 历史人工反馈 | 某条风险被用户确认或忽略 |
| Semantic Memory | 沉淀后的审查偏好 | “该客户通常要求责任上限不超过合同金额” |

### 7.2 写入时机

以下情况写入 Memory：

- 用户采纳某条风险；
- 用户忽略某条风险并填写原因；
- 用户修改 Agent 给出的建议；
- 高风险条款经人工确认；
- 同一类合同中多次出现相同处理方式。

### 7.3 Memory Item

```json
{
  "memory_id": "mem_001",
  "memory_type": "episodic",
  "contract_type": "NDA",
  "clause_type": "liability",
  "risk_type": "liability_unlimited",
  "content": "用户在供应商立场下通常要求增加责任上限，不超过过去12个月已支付费用。",
  "source": "human_feedback",
  "confidence": 0.9,
  "created_at": "2026-07-08T10:00:00"
}
```

### 7.4 使用方式

在审查相似条款时，`retrieve_memory` 工具根据 `contract_type + clause_type + risk_type` 召回历史反馈，交给 `ReviewContextBuilder` 选择是否注入上下文。

注意：Memory 不能直接覆盖 Playbook 规则。它只作为偏好和历史经验，最终风险判断仍以合同原文和 Playbook 为主。

---

## 8. 上下文工程设计

### 8.1 ReviewContextBuilder

`ReviewContextBuilder` 是本项目最值得深入讲的模块之一。

目标：每次调用 LLM 前，只放入当前判断所需的高价值信息，避免把整份合同、所有规则和所有历史对话塞进上下文。

### 8.2 上下文模板

```text
[Role & Policy]
你是合同审查 Agent。必须基于合同原文、Playbook 规则和证据输出结构化 JSON。

[Task]
判断当前条款是否存在指定风险，并给出可定位证据。

[Current Clause]
clause_id, clause_type, text, key_fields

[Related Clauses]
与当前条款有关的定义、责任、期限、争议解决等条款。

[Matched Playbook Rules]
命中的规则、检查点、风险标准、修改模板。

[Retrieved Memories]
历史人工反馈和偏好，仅作为辅助参考。

[Evidence Constraint]
每个风险必须绑定 evidence_text 和 span_offset。

[Output Schema]
返回 risk_type, severity, confidence, evidence, revision_suggestion。
```

### 8.3 上下文选择优先级

| 优先级 | 内容 |
|---|---|
| P0 | 当前条款原文、命中规则、输出格式 |
| P1 | 相关条款、关键字段、合同角色 |
| P2 | 历史人工反馈、相似案例 |
| P3 | 合同整体摘要、用户历史对话 |

### 8.4 Token 预算策略

```text
总预算：8000 tokens
当前条款：1000
Playbook 规则：2000
相关条款：2000
Memory：1000
输出格式与约束：1000
保留余量：1000
```

超限处理：

- 先减少低相似度 Memory；
- 再压缩相关条款；
- 保留当前条款和 Playbook；
- 禁止删除 evidence 约束和输出 schema。

---

## 9. Human-in-the-loop 设计

### 9.1 触发人工复核

以下情况进入人工复核：

- `confidence < 0.7`；
- 规则命中但证据定位失败；
- Risk Agent 与 Evidence Verifier 结论冲突；
- 高风险条款；
- 用户框选条款主动追问；
- Agent 输出 JSON 校验失败后仍无法恢复。

### 9.2 人工反馈动作

用户可以：

- 采纳风险；
- 忽略风险；
- 修改风险等级；
- 修改建议文本；
- 要求局部重审；
- 加入报告；
- 写入偏好记忆。

### 9.3 反馈回流

人工反馈进入两条链路：

```text
短期：更新当前 task 的 risk_finding 状态
长期：写入 Memory Store，后续相似条款可检索
```

---

## 10. Web Workbench 设计

Web 只是 Agent 的交互和可解释展示层，不是项目主线。

### 10.1 页面布局

```text
顶部：合同名称 / 审查状态 / Playbook 版本 / 导出

左侧：合同原文
- 条款编号
- 风险高亮
- 点击跳转
- 框选局部审查

右侧：Agent 审查结果
- 风险列表
- 命中规则
- 证据文本
- 修改建议
- 人工反馈按钮

底部：Agent 执行记录
- 当前节点
- 工具调用
- token 成本
- 错误信息
```

### 10.2 必要交互

- 上传合同；
- 查看任务状态；
- 点击风险跳转原文；
- 查看命中 Playbook Rule；
- 框选文本发起局部审查；
- 采纳/忽略/修改建议；
- 导出审查报告；
- 查看 Agent 执行日志。

---

## 11. 数据结构

### 11.1 Clause

```json
{
  "clause_id": "C-005",
  "title": "保密义务",
  "clause_type": "confidentiality",
  "text": "双方应对本协议项下信息承担保密义务...",
  "key_fields": {
    "obligated_party": "双方",
    "duration": "5年"
  },
  "source_location": {
    "paragraph_id": "P-018",
    "page_number": 2,
    "start_offset": 0,
    "end_offset": 128
  }
}
```

### 11.2 RiskFinding

```json
{
  "finding_id": "RF-001",
  "clause_id": "C-005",
  "risk_type": "missing_exceptions",
  "severity": "medium",
  "confidence": 0.82,
  "risk_reason": "保密条款未排除已公开信息、接收方已知信息等常见例外。",
  "matched_rule_ids": ["NDA-CONF-001"],
  "evidence_text": "双方应对本协议项下信息承担保密义务",
  "source_location": {
    "paragraph_id": "P-018",
    "start_offset": 0,
    "end_offset": 22
  },
  "citation_status": "verified",
  "revision_suggestion": "建议补充保密信息例外条款。",
  "review_status": "pending"
}
```

### 11.3 StepLog

```json
{
  "task_id": "task_001",
  "step_name": "analyze_risk",
  "input_hash": "sha256...",
  "output_hash": "sha256...",
  "tool_name": "analyze_risk",
  "status": "success",
  "latency_ms": 1860,
  "prompt_tokens": 3120,
  "completion_tokens": 640,
  "error_message": null
}
```

---

## 12. 技术栈

### 12.1 Agent / LLM

- Python；
- LangGraph 或自定义轻量状态机；
- OpenAI / Qwen / DeepSeek API；
- Pydantic 结构化输出校验；
- Tool Registry；
- JSON Schema 输出约束。

### 12.2 RAG / Memory

- PostgreSQL：业务数据；
- pgvector / Qdrant / Chroma：向量检索；
- BM25：关键词召回；
- rerank 模型或规则排序；
- YAML/JSON：维护 Risk Playbook；
- Redis：任务状态缓存。

### 12.3 文档解析

- python-docx；
- PyMuPDF；
- mammoth.js 或后端 HTML 转换；
- PDF.js 前端渲染；
- 自定义 `paragraph_id` / `span_offset` 定位。

### 12.4 后端与前端

- FastAPI；
- Celery / RQ；
- React；
- TypeScript；
- Ant Design 或 shadcn/ui；
- Docker Compose。

---

## 13. 评估指标

### 13.1 Agent 指标

| 指标 | 含义 |
|---|---|
| task_success_rate | 审查任务完整跑通比例 |
| tool_call_success_rate | 工具调用成功率 |
| llm_json_valid_rate | LLM 结构化输出合法率 |
| retry_recovery_rate | 失败重试恢复率 |
| human_review_trigger_rate | 人工复核触发比例 |

### 13.2 RAG 指标

| 指标 | 含义 |
|---|---|
| clause_segmentation_accuracy | 条款切分准确率 |
| clause_classification_accuracy | 条款类型识别准确率 |
| playbook_retrieval_recall | Playbook 规则召回率 |
| related_clause_recall | 相关条款召回率 |
| citation_accuracy | 证据定位准确率 |

### 13.3 审查效果指标

| 指标 | 含义 |
|---|---|
| risk_detection_precision | 风险识别准确率 |
| risk_detection_recall | 风险识别召回率 |
| severity_consistency | 风险等级一致性 |
| revision_accept_rate | 修改建议采纳率 |
| report_export_success_rate | 报告导出成功率 |

### 13.4 最小评测集

第一版准备 20-30 份样本即可：

- NDA 合同 10 份；
- 服务合同 5 份；
- 采购合同 5 份；
- 人工标注高风险条款 50 条；
- 人工标注普通条款 100 条；
- 框选局部审查样本 20 条；
- 人工反馈样本 20 条。

---

## 14. 项目阶段规划

### 14.1 阶段一：Agent 状态机和工具框架

交付：

- `ReviewOrchestratorAgent`；
- `AgentState`；
- `Tool Registry`；
- step log；
- 基础任务状态流转。

### 14.2 阶段二：文档解析和条款结构化

交付：

- DOCX/PDF 解析；
- `paragraph_id`；
- `clause_id`；
- `clause_type`；
- `key_fields`；
- 合同原文展示。

### 14.3 阶段三：Agentic RAG 和 Playbook

交付：

- Risk Playbook YAML/JSON；
- clause-level index；
- Playbook 检索；
- 相关条款检索；
- ContextBuilder。

### 14.4 阶段四：风险推理和证据验证

交付：

- Risk Analyzer；
- Evidence Verifier；
- 风险高亮；
- 点击跳转原文；
- 修改建议。

### 14.5 阶段五：Memory 和人工反馈

交付：

- 人工采纳/忽略；
- feedback log；
- Memory 写入；
- Memory 检索；
- 相似条款复用历史偏好。

### 14.6 阶段六：评估和简历展示

交付：

- 最小评测集；
- 指标统计脚本；
- README；
- 架构图；
- 演示截图；
- Docker Compose；
- 2-3 个完整案例。

---

## 15. 简历表达

### 15.1 简历项目标题

**ContractReviewAgent：基于 Agentic RAG 与上下文工程的合同审查 Agent**

### 15.2 简历项目描述

基于 FastAPI、React 和 LangGraph/自定义状态机构建合同审查 Agent，将合同审查拆解为文档解析、条款结构化、Risk Playbook 检索、上下文构建、风险推理、证据验证、人工反馈和报告生成等可追踪步骤。系统支持 DOCX/PDF 上传、条款级风险识别、原文证据高亮、框选局部审查、人工反馈记忆回流和审查报告导出。

### 15.3 简历技术亮点

- 设计 `ReviewOrchestratorAgent` 和 `AgentState`，实现合同审查长任务的状态流转、失败重试和节点级日志追踪。
- 将文档解析、条款抽取、规则检索、证据定位、报告生成封装为 Tool Calling 接口，降低 LLM 自由发挥带来的不可控性。
- 构建条款级 Agentic RAG，不使用固定 chunk 切分，基于 `clause_type`、Risk Playbook、相关条款和历史 Memory 动态召回审查上下文。
- 实现 `ReviewContextBuilder`，按当前条款、命中规则、相关条款、历史反馈和输出约束动态组装上下文，并进行 token 预算控制。
- 设计人工反馈 Memory，将用户采纳、忽略、修改建议等反馈沉淀为可检索经验，用于后续相似条款审查。
- 建立 Agent/RAG 评估指标，包括工具调用成功率、Playbook 召回率、风险召回率、证据定位准确率和修改建议采纳率。

### 15.4 面试可讲重点

面试时重点讲这几个问题：

- 为什么合同审查不能整份合同直接问模型？
- 为什么不用固定 chunk，而是条款级结构化 RAG？
- AgentState 如何支持长任务和失败恢复？
- Tool Calling 如何约束 LLM 行为？
- ContextBuilder 如何决定哪些信息进上下文？
- Memory 如何从人工反馈中形成长期经验？
- 风险结论如何绑定原文证据，减少幻觉？
- 如何评估这个 Agent 是否真的有效？

---

## 16. 最终定位

本项目最终定位：

```text
一个面向合同审查场景的 Agentic RAG 应用项目。

它不是普通合同问答，也不是单纯 Web CRUD，而是通过 Agent 状态机、工具调用、条款级 RAG、上下文工程、Memory 和评估体系，把合同审查做成可追踪、可解释、可复核、可迭代的智能体系统。
```
