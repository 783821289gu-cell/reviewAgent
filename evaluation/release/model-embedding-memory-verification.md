# 模型 Embedding 与向量化 Memory 验证记录

## 验证范围

- 计划：`MODEL_EMBEDDING_MEMORY_EVALUATION_PLAN.md`
- 标注版本：`effect-v3`
- 合同样本：23 份项目内合成合同
- 效果指标：27 项
- 本轮量化模式：`local_structured + local_sparse`
- 真实外部 Embedding：未运行，原因是项目本机尚未配置独立 Embedding Provider 的 Base URL、Key 和模型名

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

## 尚未验证

真实外部 Embedding 的 Recall@K、MRR、延迟、限流稳定性和调用成本尚未验证。配置完成后应使用固定 `effect-v3` 数据集运行至少两次，并保存：

- 实际 Embedding 模型名；
- 脱敏请求 ID；
- 成功/总调用数；
- Recall@K、MRR、向量覆盖率；
- P95 延迟和供应商可提供的成本数据；
- 两轮差异及失败样本。

配置位置为项目根目录 `.env`：

```dotenv
REVIEW_AGENT_EMBEDDING_MODE=openai_compatible
REVIEW_AGENT_EMBEDDING_BASE_URL=<provider-v1-base-url>
REVIEW_AGENT_EMBEDDING_API_KEY=<provider-key>
REVIEW_AGENT_EMBEDDING_MODEL=<embedding-model>
```

## 自动化与运行验证

- 后端全量：`235/235` 通过。
- 前端 Playwright：`15/15` 通过；测试临时服务使用 `127.0.0.1:8010`，结束后端口已释放。
- 标注 Schema：`AnnotationBundle.model_json_schema()` 与 `samples/annotations/schema.json` 完全一致。
- 服务实现审计：Python、JavaScript 和 TypeScript 可执行源码未发现标准库 HTTP Server。
- 持久服务：提交前由最新代码在 `127.0.0.1:8000` 单实例启动，并执行健康检查及 SQLite 向量表迁移检查。
