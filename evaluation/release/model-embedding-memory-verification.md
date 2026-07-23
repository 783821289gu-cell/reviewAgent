# 模型 Embedding 与向量化 Memory 验证记录

## 验证范围

- 计划：`MODEL_EMBEDDING_MEMORY_EVALUATION_PLAN.md`
- 标注版本：`effect-v3`
- 合同样本：23 份项目内合成合同
- 效果指标：27 项
- 离线基线模式：`local_structured + local_sparse`
- 真实评测模式：`local_structured + openai_compatible`
- 真实 Embedding 模型：`BAAI/bge-m3`，向量维度 `1024`

本记录区分实现验证、离线量化和真实外部模型验证。DeepSeek LLM Key 不作为 Embedding 凭据，也不把 Mock Provider 或 `local_sparse` 结果写成真实模型结果。

## 已验证实现

1. RAG 和 Memory 共用显式 EmbeddingProvider；生产默认配置为 `openai_compatible`。
2. SQLite 自动创建 `semantic_preference_embeddings`，保存偏好 ID、模式、模型、内容哈希、维度、向量和更新时间。
3. Memory 候选先按合同类型和审查立场硬过滤，再执行向量排序；跨立场和跨合同类型样本不会返回。
4. 相同内容和模型复用持久化候选向量；偏好内容变化后按内容哈希刷新。
5. 外部 Embedding 限流测试记录真实错误类型和请求 ID，不出现 `local_sparse` 静默回退。
6. Trace 中的 Embedding 模式和模型来自本次实际 Provider 调用，不再只读取静态 Settings。
7. 可执行 Python、JavaScript 和 TypeScript 源码中未发现 `http.server`、`HTTPServer`、`BaseHTTPRequestHandler` 或 `SimpleHTTPRequestHandler`；HTTP 服务入口保持 FastAPI / Uvicorn。

## 离线量化结果

评测 ID：`effect_2fa5b45b9ba6`

| 指标 | 样本 | 分数 | 阈值 | 达标 |
| --- | ---: | ---: | ---: | --- |
| 相关条款 Recall@1 | 20 | 1.0000 | 0.8000 | 是 |
| Memory Recall@3 | 20 | 1.0000 | 0.8000 | 是 |
| Memory MRR | 20 | 1.0000 | 0.8000 | 是 |
| Memory 向量覆盖率 | 20 | 1.0000 | 1.0000 | 是 |
| Embedding 调用成功率 | 40 | 1.0000 | 0.9500 | 是 |
| Memory 偏好一致性 | 20 | 0.5500 | 0.8000 | 否 |
| 风险 Recall | 65 | 0.5231 | 0.8000 | 否 |
| Evidence span 命中率 | 65 | 0.5231 | 0.9000 | 否 |

完整摘要状态为 `completed_with_failures`。新增检索指标达标不改变风险、证据和 Memory 建议一致性仍未达标的事实。

## 真实外部 Embedding 量化结果

2026-07-23 使用固定 `effect-v3` 全量合成数据连续运行两次。评测进程显式设置 `LLM_MODE=local_structured`，因此外部 Provider 记录只来自 Embedding，不混入 DeepSeek LLM 调用。

| 项目 | 第一轮 | 第二轮 |
| --- | ---: | ---: |
| 评测 ID | `effect_140a298d64d4` | `effect_8d12e8852d9a` |
| 外部 Provider 成功/总调用 | 164/164 | 164/164 |
| Provider P95 延迟 | 390 ms | 352 ms |
| 相关条款 Recall@1 | 1.0000 | 1.0000 |
| Memory Recall@3 | 1.0000 | 1.0000 |
| Memory MRR | 1.0000 | 1.0000 |
| Memory 向量覆盖率 | 1.0000 | 1.0000 |
| Embedding 调用成功率 | 1.0000 | 1.0000 |
| Memory 偏好一致性 | 0.5500 | 0.5500 |
| 风险 Recall | 0.5231 | 0.5231 |
| Evidence span 命中率 | 0.5231 | 0.5231 |
| 摘要状态 | `completed_with_failures` | `completed_with_failures` |

每轮 164 次是完整评测的外部 Embedding HTTP 调用总数，覆盖 23 份合同主流程和专门标注检索检查。`embedding_call_success_rate` 的 40 个样本只统计 20 个相关条款和 20 个 Memory 专门检索日志，不等同于总网络调用数。

两轮输出中均未发现 `local_sparse` 回退或 Embedding Key。供应商没有返回 `x-request-id` 或响应 `id`，因此请求 ID 摘要数如实为 0；当前也没有可用的成本单价或供应商成本字段。完整脱敏对比见 `model-embedding-real-comparison.json`，原始运行文件保存在本机已忽略目录 `evaluation/model_embedding_runs/`。

真实 Embedding 检索指标两轮稳定达标，但整体 Agent 仍未验收通过。风险 Recall、Evidence span 命中率和 Memory 偏好一致性没有因切换 Embedding 自动改善，仍是后续 Agent 效果工作的明确缺口。

## 自动化与运行验证

- 后端全量：`235/235` 通过。
- 前端 Playwright：`15/15` 通过；测试临时服务使用 `127.0.0.1:8010`，结束后端口已释放。
- 标注 Schema：`AnnotationBundle.model_json_schema()` 与 `samples/annotations/schema.json` 完全一致。
- 服务实现审计：Python、JavaScript 和 TypeScript 可执行源码未发现标准库 HTTP Server。
- 持久服务：2026-07-23 重新启动后由最新代码在 `127.0.0.1:8000` 单实例运行，健康检查为 `ok`；启动前已脱敏确认其 `.env` 使用 `openai_compatible / BAAI/bge-m3`。
