# NDA 样本与人工标注

本目录只包含项目内合成、公开授权或脱敏授权的评测输入。当前 `effect-v3` 全部为项目内合成夹具，不包含客户合同或真实交易文件，也不能作为生产级准确率数据集。

## 数据范围

- `nda_sample_01.txt` 至 `nda_sample_20.txt`：20 份合成 NDA。
- `contract_type/`：采购、服务和低信息量文本等合同类型边界夹具。
- `annotations/contracts.json`：23 份效果评测合同标注，其中 20 份 NDA、3 份非 NDA。
- `annotations/clauses.json`：174 条条款标注。
- `annotations/risks.json`：65 条风险条款标注。
- 普通条款：109 条，由全部条款扣除风险条款后计算。
- `annotations/related_clauses.json`：20 组跨条款检索标注，覆盖 8 类 NDA 风险。
- `annotations/memory.json`：20 组有/无 Memory 对照样本。
- `prompt_injection/cases.json`：10 组 Prompt Injection、伪造消息、工具指令、输出覆盖和超长输入样本。

`manifest.json` 中 `basic_evaluation_sample_count` 固定为 10，因此基础流程评测继续使用前 10 份 NDA，保持既有 API 和基线可比性。效果评测默认使用全部 23 份合同标注，也可以通过 API 请求体中的 `contract_ids` 运行明确子集。

## 标注约束

1. 每个正式风险必须引用同一合同内的 `clause_id` 和原文 evidence span。
2. Planner 期望动作必须绑定当前合同内的目标条款。
3. 合同、Memory、相关条款和恶意输入都必须提供允许的来源类型。
4. Schema、引用完整性和数量门槛由运行时模型校验；缺失或越界时评测状态为 `annotation_failed`。
5. Memory 对照结果不预设改善；改善、持平或退化均按实际结果记录。

这些样本仅用于验证当前 NDA Agent 的分类、条款、Playbook、检索、风险、证据、Planner、Memory、安全和恢复路径，不替代人工法律审查。
