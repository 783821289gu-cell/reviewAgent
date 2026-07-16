# NDA 样本说明

本目录包含 10 份用于第一版流程评测的 NDA 合成样本，以及 `annotations/` 下独立的人工标注效果评测集。

这些样本和标注由项目内人工编写，只覆盖 `SPEC.md`、`PLAN.md` 与 `NEXT_PLAN.md` 中确认的第一版 NDA 审查验证点；它们不是客户合同，不来源于真实交易文件，也不代表生产级准确率评测集。

样本用途：

1. 验证上传、解析、条款结构化、Playbook 命中、上下文构建、风险分析、证据验证、人工反馈、Memory 写入和 Markdown 报告导出能闭环跑通。
2. 为 `evaluation/` 中的基础评测摘要提供可解释输入。
3. 仅作为流程样本，不作为法律意见或准确率声明依据。

人工标注文件：

1. `annotations/contracts.json`：合同类型、预期决策、终态和人工复核预期。
2. `annotations/clauses.json`：条款边界、条款类型和预期 Playbook Rule。
3. `annotations/risks.json`：预期风险、可接受等级、证据 span、人工复核和报告选择。
4. `annotations/related_clauses.json`：当前合同内相关条款标注。
5. `annotations/schema.json`：上述标注组合后的 JSON Schema。

来源策略：只允许 `synthetic`、`public_authorized` 或 `deidentified_authorized`；当前 `effect-v1` 全部为项目内合成夹具，不使用未授权客户合同。效果评测必须报告真实指标和失败样本，不能把流程跑通率当成准确率。
