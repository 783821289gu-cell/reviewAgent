# 模型 Embedding、向量化 Memory 与 Agent 量化评估计划

## 1. 背景与问题

当前相关条款 RAG 已有 `openai_compatible` Embedding Provider，但默认运行配置和既有完整评测仍使用 `local_sparse`。该模式适合离线回归，不足以证明真实语义模型效果。

当前 Memory 已实现人工反馈聚合、冲突检测、过期和置信度更新，但检索依赖合同类型、条款类型、风险类型和审查立场的精确字段过滤。SQLite 中没有 Memory 向量，语义相近但字段命名不同的历史偏好无法召回，也无法量化检索质量。

后端已由 FastAPI 和 Uvicorn 提供服务。本轮需要确认可执行代码中不再存在 `http.server`、`HTTPServer` 或 `BaseHTTPRequestHandler` 路径，历史文档只保留迁移记录。

## 2. 本轮目标

1. 将生产 RAG 路径明确为真实模型 Embedding；`local_sparse` 只作为显式离线测试模式保留。
2. 复用同一个 Embedding Provider 向量化语义偏好 Memory，并将向量和版本信息持久化到 SQLite。
3. Memory 检索先执行合同类型和审查立场硬隔离，再按向量相似度及可解释的精确字段加权排序。
4. Embedding 配置或调用失败时明确失败，不静默降级为本地稀疏向量或纯关键词结果。
5. 在现有 Agent 效果评测中增加 Memory 检索和 Embedding 运行指标，输出样本数、分数、阈值、是否达标及失败样本。
6. 保持 FastAPI 为唯一 HTTP 服务实现，并用代码搜索、API 测试和启动验证提供证据。

## 3. 明确边界

1. DeepSeek 继续承担结构化 LLM 推理，不将聊天模型响应伪装成 Embedding。
2. 模型 Embedding 使用独立的 OpenAI-compatible `/embeddings` 服务配置。DeepSeek 官方当前没有在本项目采用的公开 Embedding 模型接口，因此 Embedding Base URL、Key 和模型名必须单独配置。
3. 不引入向量数据库。当前 Memory 数据量小，SQLite 保存向量并由服务层执行余弦排序即可满足第一版需求。
4. 不修改风险规则、Playbook、证据判定和人工复核业务语义。
5. 不把本地 Stub 或 Mock 结果写成真实外部模型评测结果。

## 4. 技术方案

### 4.1 模型 Embedding 配置

- 生产示例配置使用 `REVIEW_AGENT_EMBEDDING_MODE=openai_compatible`。
- 必填：`REVIEW_AGENT_EMBEDDING_BASE_URL`、`REVIEW_AGENT_EMBEDDING_API_KEY`、`REVIEW_AGENT_EMBEDDING_MODEL`。
- `local_sparse` 保留给无网络单元测试和可复现离线基线；运行日志必须明确标记其不是外部模型。
- RAG 与 Memory 共用 Provider、超时、调用元数据和脱敏规则。

### 4.2 Memory 向量存储

新增 `semantic_preference_embeddings` 表，以 `preference_id + embedding_mode + embedding_model` 唯一标识向量版本，保存：

- 内容哈希；
- 向量维度；
- JSON 向量；
- 更新时间。

语义偏好发生变化后，内容哈希随之变化。下次检索发现哈希不一致时惰性重算并覆盖对应模型版本，不在应用启动时触发无界外部调用。

### 4.3 Memory 检索

1. 先按 `contract_type` 和 `review_position` 过滤候选，禁止跨合同类型或跨立场注入。
2. 查询文本由当前条款标题、正文、条款类型、关键字段、风险类型和审查立场组成。
3. Memory 文本由偏好所属类型、风险类型、立场、支持/反对状态、严重程度和人工建议组成。
4. 对查询和候选执行同模型 Embedding，持久化缺失或过期的候选向量。
5. 最终分数由余弦相似度、风险类型精确命中和条款类型精确命中组成，并在结果中返回各因子。
6. 只有精确语义组或达到最低向量相似度的 Memory 才有资格影响建议；冲突、过期、纯反对偏好仍按既有规则阻断。

### 4.4 日志与失败语义

- `retrieve_memory` 与 `retrieve_related_clauses` 都记录 Embedding 模式、模型、请求 ID、输入数、维度、延迟和错误类型。
- 日志不记录 Key、合同原文、完整查询文本或原始向量。
- 外部 Embedding 配置错误、鉴权错误、超时、限流或响应 Schema 错误继续映射为检索失败，禁止静默回退。

### 4.5 Agent 评估指标

保留现有指标，并新增：

1. `memory_retrieval_recall_at_k`：标注 Memory 是否进入 Top-K。
2. `memory_retrieval_mrr`：标注 Memory 首次命中的平均倒数排名。
3. `memory_embedding_coverage`：Memory 检索结果是否带有有效模型、维度、相似度和向量版本信息。
4. `embedding_call_success_rate`：RAG 与 Memory 的 Embedding Provider 调用成功率。

所有指标必须配置阈值；零样本不得视为达标。评测摘要同时记录 Embedding 模式和模型，真实外部模型评测必须能提供脱敏请求 ID 和调用次数证据。

## 5. 验收标准

1. SQLite 可自动迁移已有数据库，旧 Memory 不丢失；首次检索后对应语义偏好向量可查询。
2. 相同模型和内容不会重复计算候选向量；偏好内容变化后会按内容哈希重新计算。
3. 语义相近但条款类型或风险类型名称不完全相同的 Memory 可被向量召回。
4. 任何候选都不能跨 `contract_type` 或 `review_position` 返回。
5. 外部 Embedding 失败时工具日志包含真实错误类型，结果不出现 `local_sparse` 回退。
6. 效果评测输出新增四项指标及失败样本，并继续输出原有指标。
7. Python 全量测试和前端回归通过；FastAPI 健康检查通过。
8. 可执行源代码中不存在标准库 HTTP Server 实现。
9. 使用真实模型 Embedding 的完整或明确限定子集评测必须单独记录；未配置独立 Embedding Key 时只报告“实现已验证、真实模型效果未验证”。

## 6. 执行顺序

1. 增加 SQLite 向量表和 Memory Repository API。
2. 实现 Memory 向量构建、缓存、召回、排序和安全门槛。
3. 扩展工具契约、Embedding 调用日志和单元测试。
4. 扩展标注参数、评估指标、API 契约夹具和评估测试。
5. 更新配置示例、README、开发决策记录和验证记录。
6. 执行局部测试、Python 全量测试、前端测试、FastAPI 启动与健康检查。

## 7. 配置位置

项目根目录 `.env`：

```dotenv
REVIEW_AGENT_EMBEDDING_MODE=openai_compatible
REVIEW_AGENT_EMBEDDING_BASE_URL=https://your-embedding-provider.example/v1
REVIEW_AGENT_EMBEDDING_API_KEY=your-embedding-api-key
REVIEW_AGENT_EMBEDDING_MODEL=your-embedding-model
REVIEW_AGENT_EMBEDDING_TIMEOUT_SECONDS=60
```

该 Key 与 `REVIEW_AGENT_LLM_API_KEY` 分开配置。没有独立 Embedding Provider 凭据时，不能声称已完成真实模型 Embedding 效果验证。
