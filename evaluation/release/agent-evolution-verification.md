# Agent Evolution 任务 9 验收记录

## 结论

- 验收时间：2026-07-21（Asia/Shanghai）
- 验收状态：`completed_with_failures`
- 结论：评测能力、扩展标注集和两次真实 DeepSeek 稳定性运行均已落地，但效果指标未全部达标，不能声明 Agent 效果验收通过。
- 数据声明：所有新增合同、Memory 和 Prompt Injection 样本均为项目内合成数据，不包含真实客户合同。
- 保密声明：本记录不包含 API Key、Authorization、完整合同、完整 Prompt 或原始 Provider 请求 ID。

## 版本与范围

| 项目 | 记录值 |
| --- | --- |
| 标注版本 | `effect-v2` |
| 代码版本 | `historical-commit` |
| 工作区状态 | `git_dirty=true`，任务 9 的未提交改动参与评测 |
| Playbook | `nda-v1` |
| Prompt | `contract-review-agent.prompt.v1` |
| LLM 模式 | `openai_compatible` |
| 模型 | `deepseek-v4-pro` |
| Embedding | `local_sparse_hash_v1` |
| 真实评测子集 | `nda-01`、`nda-11`，固定两份合成 NDA |

完整标注集包含 23 份合同标注，其中 20 份 NDA 和 3 份非 NDA/模糊类型边界样本；包含 174 条条款标注、65 条唯一风险条款、109 条普通条款、20 组跨条款检索、20 组 Memory 对照和 10 组 Prompt Injection 样本。完整标注集已通过运行时 Pydantic Schema、引用完整性、唯一性、证据 span 和最低数量校验。

真实 DeepSeek 运行采用固定的两份 NDA 子集，用于验证完整 NDA 主流程和两次波动。未对 23 份完整标注集执行两次外部模型调用，因此本记录不把子集结果表述为完整数据集效果。

## 运行摘要

| 项目 | 稳定性运行 1 | 稳定性运行 2 | 波动 |
| --- | ---: | ---: | ---: |
| 评测 ID | `effect_2bae9f3769e6` | `effect_4eb9be30c911` | - |
| 运行标签 | `deepseek-stability-1` | `deepseek-stability-2` | - |
| 创建时间（UTC） | `2026-07-21T08:46:17Z` | `2026-07-21T08:50:23Z` | - |
| Provider 调用/成功 | 11 / 9 | 20 / 20 | 调用 +9，成功 +11 |
| Prompt tokens | 28,139 | 33,303 | +5,164 |
| Completion tokens | 10,676 | 13,185 | +2,509 |
| Total tokens | 38,815 | 46,488 | +7,673（+19.77%） |
| Provider P95 | 40,209 ms | 20,154 ms | -20,055 ms（-49.88%） |
| 估算成本 | `null` | `null` | 无法计算 |
| 成本状态 | 未配置 | 未配置 | 无变化 |
| 运行状态 | `completed_with_failures` | `completed_with_failures` | 无变化 |

成本没有配置模型输入/输出单价，因此只能保留 token 和“未配置”状态，不推测金额。

请求 ID 只保留 SHA-256 截断摘要：

- 运行 1（11 个）：`55502782c863`、`5da2b7a07e8a`、`62cd96ea4fa5`、`6540976e123d`、`691ca5d03511`、`9c7a475265a4`、`9e4400857c63`、`c146246b2b92`、`c8cf4026e84b`、`ca57e1afc53b`、`ea5e705893aa`
- 运行 2（20 个）：`03a8e7388f59`、`173b6921e154`、`1c1bbbe58199`、`4e4b6df99907`、`6e6dd197165a`、`75183ad2d731`、`78f0fff73664`、`8063402e5363`、`8b5c3f78034e`、`8f11bd9efd62`、`91bbde4e4656`、`94599848db22`、`a47c35494af9`、`b3b810d55149`、`b69e6f87004a`、`c1360721a9ba`、`c58c235ff2bb`、`dbf7d28179b9`、`e88d00797f19`、`f04cbd698eb2`

## 指标结果

“达标”依据 `samples/manifest.json` 的阈值；样本数为 0 时不视为达标。

| 指标 | 运行 1 样本 | 运行 1 分数 | 达标 | 运行 2 样本 | 运行 2 分数 | 达标 | 分数波动 |
| --- | ---: | ---: | --- | ---: | ---: | --- | ---: |
| `nda_classification_accuracy` | 2 | 1.0000 | 是 | 2 | 1.0000 | 是 | +0.0000 |
| `non_nda_rejection_rate` | 0 | 0.0000 | 否 | 0 | 0.0000 | 否 | +0.0000 |
| `clause_boundary_accuracy` | 17 | 1.0000 | 是 | 17 | 1.0000 | 是 | +0.0000 |
| `clause_type_accuracy` | 17 | 1.0000 | 是 | 17 | 1.0000 | 是 | +0.0000 |
| `playbook_recall_at_k` | 7 | 1.0000 | 是 | 7 | 1.0000 | 是 | +0.0000 |
| `related_clause_recall_at_k` | 20 | 1.0000 | 是 | 20 | 1.0000 | 是 | +0.0000 |
| `risk_precision` | 1 | 1.0000 | 是 | 6 | 1.0000 | 是 | +0.0000 |
| `risk_recall` | 6 | 0.1667 | 否 | 6 | 1.0000 | 是 | +0.8333 |
| `risk_f1` | 6 | 0.2858 | 否 | 6 | 1.0000 | 是 | +0.7142 |
| `evidence_span_hit_rate` | 6 | 0.1667 | 否 | 6 | 0.8333 | 否 | +0.6666 |
| `manual_review_trigger_rate` | 2 | 0.0000 | 否 | 2 | 1.0000 | 是 | +1.0000 |
| `report_filter_accuracy` | 2 | 0.5000 | 否 | 2 | 1.0000 | 是 | +0.5000 |
| `tool_call_success_rate` | 89 | 0.9663 | 是 | 108 | 1.0000 | 是 | +0.0337 |
| `end_to_end_success_rate` | 2 | 0.5000 | 否 | 2 | 1.0000 | 是 | +0.5000 |
| `planner_action_accuracy` | 1 | 1.0000 | 是 | 1 | 1.0000 | 是 | +0.0000 |
| `invalid_tool_action_rate` | 1 | 0.0000 | 是 | 1 | 0.0000 | 是 | +0.0000 |
| `llm_json_valid_rate` | 10 | 0.8000 | 否 | 19 | 1.0000 | 是 | +0.2000 |
| `llm_schema_repair_rate` | 2 | 0.0000 | 否 | 0 | 0.0000 | 否 | +0.0000 |
| `retrieval_repair_success_rate` | 0 | 0.0000 | 否 | 0 | 0.0000 | 否 | +0.0000 |
| `retry_recovery_rate` | 0 | 0.0000 | 否 | 0 | 0.0000 | 否 | +0.0000 |
| `unsupported_finding_rate` | 1 | 0.0000 | 是 | 6 | 0.0000 | 是 | +0.0000 |
| `memory_preference_consistency` | 20 | 0.5500 | 否 | 20 | 0.5500 | 否 | +0.0000 |
| `prompt_injection_block_rate` | 10 | 1.0000 | 是 | 10 | 1.0000 | 是 | +0.0000 |

## 失败与限制

1. 运行 1 的 `nda-11` 在风险分析阶段生成了与 `position_config` 不一致的 `risk_focus`，两次结构化输出均未通过 Schema，任务进入明确的 `LLM_OUTPUT_INVALID` 失败状态；没有把无效输出伪装成成功结果。
2. 两轮结果波动较大。风险召回从 `0.1667` 变为 `1.0`，端到端成功率从 `0.5` 变为 `1.0`，说明当前两样本结果不足以证明模型稳定。
3. Evidence span 命中率两轮均未达到 `0.9`；运行 2 为 `0.8333`。
4. Memory 偏好一致率两轮均为 `0.55`，低于 `0.8`，没有观察到 Memory 改善。
5. 运行 1 的 JSON 合法率为 `0.8`，一次修复成功率为 `0`，未满足“合法率 98%、修复后 100%”。运行 2 没有产生修复样本，不能据此证明修复率达标。
6. 两轮均没有触发检索修复和通用重试恢复样本，两个指标样本数为 0，不能声明达标。
7. 真实运行子集不含非 NDA，因此 `non_nda_rejection_rate` 样本数为 0；完整数据集中存在 3 份边界样本，但本次没有对它们执行外部模型稳定性运行。
8. 两轮真实调用均使用工作区未提交代码；提交后如需发布结论，应在提交版本上重跑。
9. Code Review 后收紧了检索修复成功定义、成本完整性校验和标注配置校验。由于两次真实运行的检索修复样本数均为 0、成本均未配置，这些修复不改变已记录分数；本轮没有再次消耗 DeepSeek，Review 后代码的外部模型结果状态为“未重跑”。

## 完整标注集本地复核

Code Review 使用 `local_structured + local_sparse` 对全部 23 份合同执行了一次不产生外部费用的完整评测，状态为 `completed_with_failures`。关键结果如下：

| 指标 | 样本 | 分数 | 达标 |
| --- | ---: | ---: | --- |
| NDA 分类准确率 | 20 | 1.0000 | 是 |
| 非 NDA 拒绝率 | 2 | 1.0000 | 是 |
| 条款边界准确率 | 174 | 1.0000 | 是 |
| 条款类型准确率 | 174 | 1.0000 | 是 |
| Playbook Recall@3 | 81 | 1.0000 | 是 |
| 相关条款 Recall@1 | 20 | 1.0000 | 是 |
| 风险 Recall | 65 | 0.5231 | 否 |
| Evidence span 命中率 | 65 | 0.5231 | 否 |
| Planner 动作准确率 | 10 | 1.0000 | 是 |
| 非法动作执行率 | 10 | 0.0000 | 是 |
| Memory 偏好一致率 | 20 | 0.5500 | 否 |
| Prompt Injection 阻断率 | 10 | 1.0000 | 是 |

本地模式没有外部 Provider 调用，因此 LLM JSON、Schema 修复、Provider 重试和检索修复指标样本数均为 0，不能据此声明这些指标达标。

## 已满足项

- Planner 只执行白名单动作，两轮非法动作执行率均为 `0`。
- Prompt Injection 10 个合成样本两轮阻断率均为 `1.0`。
- 相关条款 Recall@1 在 20 组标注上两轮均为 `1.0`。
- 无证据正式风险率两轮均为 `0`。
- 两次运行都保留了模型、Prompt、Playbook、标注、代码版本、token、P95、成本状态和脱敏请求 ID 摘要，没有只保留表现更好的一轮。

## 任务 10 Web 与交付验证

### Code Review 结论

- 验证时间：2026-07-22（Asia/Shanghai）
- 独立 Code Review、必要修复和全量验证已完成；没有把开发者自验直接当作发布验收。
- 本轮未调用 DeepSeek，也未产生新的外部模型效果结论；任务 9 的两次真实子集运行及其失败限制保持不变。
- 发布目标已确认：`origin` 指向项目 GitHub 仓库。提交、推送和最终工作树状态以实际 Git 命令结果为准，本记录不预先声明发布成功。

### Review 发现与修复

1. 修复已成功检索重试后仍把历史 Evidence 失败显示为当前失败的问题；当前提示只依据任务最终状态。
2. 修复历史 Critic 冲突在后续 `PASS` 或人工处理后仍持续显示的问题；当前提示依据每个上下文的最后一次 Critic 决策和未解决风险。
3. `CANCEL_REQUESTED` 与 `CANCELLED` 改为分别显示“取消请求已提交”和“任务已取消”，不再把请求阶段伪装成终态。
4. 检索修复和 Memory 只统计成功工具调用；失败调用单独显示失败数，不再计为成功读写或成功修复。
5. Provider 全部失败时显示“DeepSeek 调用失败”，不再显示“真实调用成功”；真实调用文案要求至少一次 Provider 成功。
6. README 补充真实上下文预算、节点/任务超时和错误分类重试上限，避免只描述能力而省略运行边界。

### 实际运行结果

| 验证项 | 命令/范围 | 结果 |
| --- | --- | --- |
| JavaScript 与夹具静态检查 | 3 个组件、2 个 E2E 文件 `node --check`，JSON 夹具解析，`git diff --check` | 通过 |
| 后端全量 | `python -m unittest discover -s backend\tests -p "test_*.py" -v` | 207 项，207 项通过，72.117 秒 |
| FastAPI API 契约 | `python -m unittest backend.tests.test_api_fastapi -v` | 10 项，10 项通过，16.794 秒 |
| 基础流程评测 | 固定 10 份合成 NDA 的单项回归 | 1 项通过，2.504 秒；仍只代表流程跑通 |
| 完整本地效果评测 | `local_structured + local_sparse`，运行标签 `task10-code-review-local-full` | 23 份合同、23 项指标，`completed_with_failures` |
| Playwright Chromium | 原有主流程与错误流 + Agent 浏览器验收 | 13 项，13 项通过，21.9 秒 |
| 视觉复核 | 1440×960、1280×800、390×844 | 自动几何/溢出断言通过；人工检查桌面与移动截图未见重叠或关键操作不可达 |

完整本地效果评测 ID 为 `effect_6cb71545267b`，运行产物位于已忽略的 `test-results/`，未加入 Git。关键结果：

| 指标 | 样本 | 分数 | 达标 |
| --- | ---: | ---: | --- |
| 相关条款 Recall@1 | 20 | 1.0000 | 是 |
| 风险 Recall | 65 | 0.5231 | 否 |
| Evidence span 命中率 | 65 | 0.5231 | 否 |
| Memory 偏好一致率 | 20 | 0.5500 | 否 |
| Prompt Injection 阻断率 | 10 | 1.0000 | 是 |

本地效果评测没有外部 Provider 调用，`provider_call_count=0`。因此它不能替代真实 DeepSeek 评测，也不能验证 token、成本或外部 Provider 延迟。

### Web 验收覆盖

1. 执行记录从任务日志和 Trace 区分本地模式、DeepSeek 真实调用、外部调用失败、Planner、一次检索修复、Critic、Evidence、Memory、Injection 阻断、取消、超时和人工恢复。
2. 效果面板展示 Provider 成功/调用数、脱敏请求 ID 摘要数量、token、成本状态、P95、运行标签和零样本状态；零样本不会显示为达标。
3. 高风险、Evidence 失败、Critic 冲突和明确人工状态显示独立人工复核提示，不将候选结论描述为确定结论。
4. 新增浏览器夹具仅直接渲染组件，不进入生产任务 API；夹具中的 Provider ID 和结果均为合成数据。
5. 原上传、SSE、风险定位、高亮、局部审查、反馈、Memory、报告、流程评测和错误流断言均保留并继续通过。

### 发布边界

1. 任务 10 以独立提交交付，提交说明必须同时包含行为范围和实际验证结果。
2. 提交前检查范围和忽略规则，DeepSeek Key、数据库、上传、报告、完整评测产物、截图、Trace、缓存和本机路径不得进入 Git。
3. 推送后必须确认本地 `HEAD` 与 `origin/main` 一致且工作树干净；若命令失败，保留实际错误并停止声明发布完成。
