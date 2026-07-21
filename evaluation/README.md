# 评测输出

本目录记录基础流程评测、人工标注效果评测和版本化发布验收。运行时生成的 JSON、Markdown、SQLite、报告和浏览器产物由 `.gitignore` 排除；只提交经过脱敏的发布记录。

既有全量交付验证保留在 `release/task10-verification.md`；本轮 Agent 效果验收记录位于 `release/agent-evolution-verification.md`。

## 基础流程评测

`POST /api/evaluation/run` 固定使用 `samples/manifest.json` 的前 10 份合成 NDA，验证解析、条款、Playbook、风险证据、反馈、Memory 和报告闭环。该入口继续保持原有 10 样本 API 契约。

## Agent 效果评测

`POST /api/evaluation/effect/run` 默认运行 `effect-v2` 完整标注集，也接受以下可选请求体：

```json
{
  "contract_ids": ["nda-01", "nda-11"],
  "run_label": "deepseek-stability-1"
}
```

评测保留原 14 项指标，并增加：

1. Planner 动作准确率和非法工具动作执行率。
2. LLM JSON 合法率和 Schema 修复率。
3. 检索修复成功率和重试恢复率。
4. 无证据正式风险率。
5. Memory 偏好一致率。
6. Prompt Injection 阻断率。

每次摘要还记录 Prompt/Completion/总 token、估算成本、成本配置状态、Provider P95 延迟、Prompt 版本和经过 SHA-256 截断的请求 ID 摘要。任一调用缺少成本时，汇总成本必须为 `null` 并保留未配置或部分不可用状态，不允许把部分金额写成总成本。

## 运行方式

本地结构化回归：

```powershell
python -m unittest backend.tests.test_effect_evaluation -v
```

真实 DeepSeek 运行前，必须把本机 `.env` 的值注入启动进程；项目配置模块不会自动加载 `.env`。Key、Authorization、完整合同、完整 Prompt、数据库和原始运行产物不得进入 Git。

发布验收记录位于 `evaluation/release/`。记录必须同时保留达标项、未达标项、失败样本和两次运行波动，不能只保留最好结果。
