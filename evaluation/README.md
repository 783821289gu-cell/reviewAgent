# 评测输出

本目录保存两类相互独立的评测输出。

任务 10 的全量验证命令、真实结果、失败修复过程和剩余限制记录在 `release/task10-verification.md`；运行生成的评测 JSON、Markdown、数据库和浏览器产物继续按 `.gitignore` 排除。

## 流程评测

`POST /api/evaluation/run` 使用 `samples/` 下 10 份合成 NDA 样本，只验证任务执行、解析、条款、Playbook、风险证据、Memory 和报告导出是否跑通。输出为 `evaluation_summary.json` 和 `evaluation_summary.md`。

## 效果评测

`POST /api/evaluation/effect/run` 使用 `samples/annotations/` 下 `effect-v1` 人工标注，计算分类、条款、Playbook、相关条款、风险、证据、人工复核、报告过滤、工具调用和端到端指标。每次运行在 `effect/` 下生成以评测 ID 命名的 JSON 和 Markdown，不覆盖历史摘要。

每项指标都包含样本数、通过数、失败数、阈值、达标状态和失败样本。摘要记录 Git 代码版本及脏状态、Playbook 版本、标注版本、LLM/Embedding 模式和模型、Recall@K 与阈值参数。

当前标注全部是项目内合成夹具。流程跑通率和小样本效果指标都不代表生产级准确率，不替代人工法律审查；低于阈值时输出必须保留真实失败状态。
