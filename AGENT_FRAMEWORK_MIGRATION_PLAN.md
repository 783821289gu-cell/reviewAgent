# Agent 现成框架迁移计划

## 1. 文档状态

- 本文档是独立迭代计划，不覆盖此前的 `PLAN.md`、`NEXT_PLAN.md` 或 `AGENT_EVOLUTION_PLAN.md`。
- 当前状态：执行中。
- 核心约束：遵守 `constitution.md`；每个阶段独立实现、审查、验证和提交。

## 2. 目标

将当前手写线程编排、SQLite 仓储、进程内事件、手写检索与解析逐步替换为：

`FastAPI + LangGraph + PostgreSQL/pgvector + Redis/RQ + Docling + BGE + MCP + OpenTelemetry/Langfuse`

迁移完成后只保留一套运行路径，不长期维护新旧双实现；现有前端、REST API、SSE 事件和人工复核流程保持兼容。

## 3. 数据与执行边界

1. PostgreSQL `app` schema 是任务、文档、风险、事件、日志和审计数据的业务真相源。
2. LangGraph Checkpointer 只保存执行游标、待处理 ID、重试和中断信息，不复制完整合同业务数据。
3. Redis 只用于 RQ、缓存、锁、限流和事件通知，不保存不可恢复的任务状态。
4. `thread_id` 固定等于 `task_id`。
5. 工具调用使用稳定幂等键；业务写入成功但 Checkpoint 未推进时，节点重跑不得产生重复结果。
6. 本地上传文件目录保持现状，不迁移到对象存储。

## 4. 固定流程

主图：

`解析 -> 分类 -> 条款结构化 -> Playbook 检索 -> 上下文构建 -> 风险子图并行 -> 聚合 -> 人工复核 -> Memory 更新`

风险子图：

`相关条款/Memory -> Risk Analyzer -> Critic -> Evidence Verifier -> 必要时仅重新检索一次`

Planner 只能使用现有白名单动作，不能自由执行任意工具。

## 5. 实施顺序

1. 固定依赖、可重复测试入口和现有运行基线。
2. PostgreSQL、SQLAlchemy/Alembic 和 SQLite 幂等迁移。
3. Redis/RQ 和跨进程持久事件流。
4. LangGraph 主工作流。
5. Postgres Checkpoint、恢复、取消与 HITL。
6. pgvector 混合检索和 BGE rerank。
7. LangGraph Postgres Store Memory。
8. Docling PDF/DOCX 解析。
9. Pydantic Tool、MCP 和 OpenTelemetry/Langfuse。
10. 运行环境验收、直接切换、旧实现清理和全量验收。

Docker 相关工作按用户最新指令后置，当前不安装、不维护 Compose，也不作为任务 1 的完成条件。PostgreSQL、Redis 等外部依赖的代码可以按顺序实现和执行不依赖 Docker 的验证；需要真实服务的集成验收必须保留为未验证，直到任务 10 明确选择并准备运行环境后再执行，不得以 Mock 或静态配置代替真实验收。

本机后续项目依赖、模型缓存和运行数据统一使用专用根目录 `D:\demo-runtime`。当前目录划分为 `venv`、`data`、`models`、`cache`、`logs` 和 `temp`；新增工具不得默认安装或下载到 C 盘。代码和公共配置仍使用环境变量表达运行根目录，不把个人用户目录写入 Git。

## 6. 固定运行参数

- RQ 本地默认 1 个 Worker。
- 单任务 DeepSeek 并发上限为 2。
- RQ 不做整任务自动重试；节点只对明确的临时错误重试 2 次，退避为 1 秒、2 秒。
- 移除按任务累计运行时长触发失败的应用层计时器。
- DeepSeek HTTP 超时保持 60 秒。
- RQ 最终硬超时为 7200 秒，并预留 120 秒状态落库窗口。
- Embedding 缓存 7 天、检索缓存 1 小时、Playbook 缓存 24 小时。
- BGE Embedding：`BAAI/bge-m3`，1024 维归一化向量。
- 混合召回：向量 Top 20、`pg_trgm` Top 20、RRF `k=60`、BGE rerank Top 20、最终最多 5 条。
- HNSW：`m=16`、`ef_construction=64`、`ef_search=80`。
- BGE Reranker：`BAAI/bge-reranker-base`，`max_length=512`。

## 7. 接口兼容

- 保持任务创建、查询、取消、恢复、反馈、报告和 SSE 的现有 URL 与响应字段。
- SSE `event_id` 在单任务内严格递增。
- PostgreSQL、Redis 或 Worker 不可用时不得接受无法执行的新任务。
- MCP 默认关闭，只监听 `127.0.0.1`。
- MCP 不暴露 `parse_document`、`write_memory` 和 `generate_report`。
- `tool_registry` 仍是唯一显式工具清单。

## 8. 验收

1. SQLite 迁移支持 `dry-run` 和 `apply`，重复执行不重复写入。
2. 迁移校验任务 ID、行数、事件最大序号和规范化 JSON 哈希。
3. 服务重启后任务可从持久 Checkpoint 恢复。
4. 取消、人工恢复、证据补充、采纳和忽略均驱动真实状态变化。
5. 过程事件持续更新，不因累计运行时间误报超时。
6. 使用项目 PDF/DOCX fixture 完成回归。
7. 使用用户提供的 `Confidentiality Agreement.pdf` 完成真实 DeepSeek 验收，但不将文件提交到仓库。
8. 每个任务经过 Code Review、必要修复和重新验证后单独提交。

## 9. 明确不做

- 不修改 DeepSeek/OpenAI-compatible LLM Provider 的接口和行为。
- 不新增 Agent 路径评测框架。
- 不扩展 Prompt 攻击测试。
- 不实现敏感信息识别功能。
- 不替换本地上传文件存储。
- 不新增合同类型。
- 不进行 UI 重设计。

## 10. 当前基线

- Python：3.13.5。
- Git：开始实施前工作区干净。
- Docker：不属于当前优先事项；本轮已按用户指令移除本机 Docker Desktop 和尚未提交的容器配置。是否重新采用 Docker 在任务 10 运行环境验收前另行确认。
- 后端全量测试：开始实施时运行超过 10 分钟未结束；任务 1 新增逐模块超时入口后，26 个后端测试模块已在每模块 60 秒限制下全部通过，总耗时约 183 秒。

## 11. 执行进度

### 任务 1：已完成

- 新增 `scripts/run_backend_tests.ps1`，每个后端测试模块使用独立 Python 进程运行，并将失败或超时作为非零退出。
- 测试进程显式禁用 `.env`，固定使用本地 LLM 和本地 Embedding，避免开发机真实 Provider 配置污染基线。
- 26 个后端测试模块在每模块 60 秒限制下全部通过，没有失败或超时模块。
- Docker Desktop 软件包已卸载，未提交的 Dockerfile、Compose 和相关配置说明已移除。
- Docker 用户数据残留目录受当前命令安全策略限制未能自动递归删除，不能记录为“残留已全部清理”。

### 任务 2：已完成

- 使用 SQLAlchemy 2.0 描述 PostgreSQL `app` schema 的 10 张现有业务表，并使用 Alembic `20260724_01` 创建初始结构。
- SQLite 迁移器只以 `mode=ro` 打开源库，支持 `dry-run` 和 `apply`；PostgreSQL 写入使用 `ON CONFLICT DO NOTHING`。
- 迁移校验每表行数、任务 ID、每任务最大事件序号和规范化 JSON SHA-256，任何差异都会回滚当前事务。
- 在 D 盘便携 PostgreSQL 17.10 上完成两次真实 apply。两次均验证 19 个任务、551 条条款、2058 条 Step Log、137 个持久事件及全部表哈希一致。
- 现有应用尚未切换 PostgreSQL 运行仓储；一次性切换仍在任务 10，当前不维护双写路径。

### 任务 3：已完成

- 在 D 盘部署便携 Redis 8.8.0，启停脚本已真实执行 `start -> status -> stop -> start -> PONG`；AOF、日志和运行数据均位于 `D:\demo-runtime`。
- 固定 `redis==7.4.1` 与 `rq==2.10.0`，新增单 Worker 管理脚本；稳定 Job ID 使用 `review-{task_id}`，整任务 RQ 重试为 0，硬超时 7200 秒并预留 120 秒状态落库窗口。
- 新增 PostgreSQL 运行仓储，当前 RQ Worker 能持久化任务、文档、条款、风险、Step Log 和全部进度事件；上传文件仍使用受控本地目录。
- Redis 仅在 PostgreSQL 事务提交后发布通知。跨进程 SSE 在查询前等待订阅确认，收到通知后重查 PostgreSQL；通知异常时回退数据库轮询。
- 新增任务创建前 Redis/RQ 注册/短 TTL Worker 心跳健康检查；Worker 预执行失败会写入真实 `TASK_ERROR`，不会让任务永久停留在等待状态。
- 真实 PostgreSQL/Redis 集成测试已通过。使用用户 PDF 走本地模式 RQ 完整链路，Worker 用时 76.8 秒，产生 212 个持久事件并到达 `EVIDENCE_VERIFIED`；测试任务和上传副本随后已精确清理。
- Code Review 修复订阅确认竞态、非法 RQ Job ID、同名 Worker 重启冲突和陈旧 Worker 注册误判后，28 个后端测试模块已在每模块 180 秒限制下全部通过，总耗时约 196 秒。
- FastAPI 默认业务入口仍使用 SQLite 和进程内编排；任务 3 只提供可验证的新运行组件和可选持久 SSE 源，直接切换仍在任务 10。

### 任务 4：已完成

- 固定 `langgraph==1.2.9`，使用 `StateGraph`、条件边和 `Send` 建立主图与风险子图。主图保持“解析 -> 分类 -> 条款结构化 -> Playbook 检索 -> 上下文构建 -> 风险子图 -> 聚合”的业务顺序。
- 风险子图保持“相关条款/Memory -> Risk Analyzer -> Critic -> Evidence Verifier -> 必要时仅重新检索一次”的边界；Planner 继续只能使用原白名单动作。
- DeepSeek 风险分支通过 LangGraph `max_concurrency=2` 限流，本地模式保持串行；并发结果按原 Review Context 序号稳定聚合，Step Log 按实际完成顺序只追加，不重排已持久化日志。
- 活动 `ReviewOrchestratorAgent` 已切换为 LangGraph。RQ Worker 直接调用图；当前 SQLite FastAPI 兼容入口使用共享执行器提交图任务，不再为每个任务创建裸 `Thread`。旧编排类只作为过渡期领域辅助实现保留，任务 10 切换后删除。
- 移除活动 Agent 的应用层累计任务计时器及对应配置。单节点超时仍为 90 秒，RQ 7200 秒硬超时和 120 秒状态落库窗口继续作为进程外最终边界。
- Code Review 修复了并行分支对标量状态的并发写冲突、终止分支误进入正式风险、并行进度覆盖、业务排序破坏 Step Log 追加索引，以及人工复核进度状态与现有 API 契约不一致等问题。
- 29 个后端测试模块在每模块 180 秒限制下全部通过，总耗时 203.2 秒；真实 PostgreSQL/Redis 事件集成测试单独通过。使用用户 PDF 走 PostgreSQL -> RQ -> LangGraph -> PostgreSQL 本地模式全链路，约 105.6 秒到达 `EVIDENCE_VERIFIED`，解析 61 条条款、形成 6 条风险并持久化 205 个严格递增事件。
- 真实 PDF 本轮只验证本地结构化 LLM 的编排和持久化链路，不冒充 DeepSeek 效果验收；测试任务、RQ Job、上传副本和 Worker 均已清理。

### 任务 5：已完成

- 固定 `langgraph-checkpoint-postgres==3.1.0` 与 `psycopg-pool==3.3.1`。Postgres Checkpointer 使用独立 `langgraph` schema；每个 Agent 使用最小 1、最大 2 条连接的连接池，并在借出连接时执行健康检查。Alembic 只管理 `app` schema，不会把包管理的 Checkpoint 表识别为待删除对象。
- `thread_id` 固定等于 `task_id`。持久控制图只保存任务 ID、执行阶段、业务状态、待处理风险 ID 和恢复次数；合同正文、条款、风险详情和工具结果仍只由 PostgreSQL `app` schema 保存。
- 业务图与持久控制图分开执行，避免 LangGraph 运行上下文将完整业务状态带入 Checkpoint。业务执行前后记录紧凑游标，人工复核节点使用 `interrupt()` 暂停。
- 人工反馈继续沿用已有采纳、忽略、修改等级、修改建议和证据补充接口。业务反馈先幂等写入 PostgreSQL/Memory，再使用 `Command(resume=...)` 推进同一线程；仍有风险时再次中断，全部处理后进入 `MEMORY_UPDATED`。
- 人工反馈的控制图继续不再依赖原始上传文件。Checkpoint 恢复失败会保留已经成功提交的业务反馈并显式报错，重试可以继续推进游标，不会把业务状态覆盖成伪造的 `TASK_ERROR`。
- 取消仍由活动图的执行控制在节点和工具边界停止，人工恢复从 PostgreSQL 业务检查点启动新的受控执行阶段；两者均保留现有 REST 契约、恢复预算和稳定幂等键。
- Code Review 实际发现并修复了六类问题：完整业务通道意外进入 Checkpoint、在单节点内循环调用动态 `interrupt()` 导致重放位置不稳定、反馈继续错误依赖上传文件、控制恢复异常覆盖已提交反馈、长任务使用单条空闲 PostgreSQL 连接，以及 Alembic 误识别包管理表。最终采用业务/控制图分离、条件自环、业务真相优先、健康检查连接池和 schema 管理边界。
- 最终全量验证还复现了兼容线程 Future 晚于终态释放 SQLite 的竞态；Agent 关闭现在等待自身后台执行结束。对应恢复测试连续运行 5 次通过，随后 30 个后端测试模块在每模块 180 秒限制下全部通过，总耗时 203.6 秒。真实 PostgreSQL Checkpointer 重启恢复和 PostgreSQL/Redis 事件测试分别通过。
- 额外完成一次真实 PostgreSQL -> RQ Worker -> LangGraph interrupt -> 新 Agent/新连接 -> `Command(resume)` 验收：任务产生 24 个连续持久事件并到达 `MEMORY_UPDATED`，Checkpoint 仅出现控制通道；测试任务、Checkpoint、RQ Job、上传副本和 Worker 均已清理。
- 当前 FastAPI 默认业务入口仍使用 SQLite 和进程内 Checkpointer；PostgreSQL/RQ/PostgresSaver 的一次性默认切换仍保留到任务 10，不维护双写路径。

### 任务 6：已完成

- 固定 `pgvector==0.4.2` 与 `sentence-transformers==5.1.2`。Embedding 使用 `BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181`，输出 1024 维归一化向量；rerank 使用 `BAAI/bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70`，最大长度 512。
- PostgreSQL 增加 `vector`、`pg_trgm`、条款检索文本和版本化 `clause_embeddings`。向量与关键词各召回 Top 20，使用 RRF `k=60` 合并，BGE 重排 Top 20，最终最多返回 5 条；HNSW 固定 `m=16`、`ef_construction=64`、`ef_search=80`。
- RQ Worker 通过现有显式 `retrieve_related_clauses` 工具绑定 PostgreSQL 检索器；默认 FastAPI 兼容路径仍使用原进程内检索，直到任务 10 一次性切换，不维护业务双写。
- Redis Embedding 缓存固定 7 天，检索缓存固定 1 小时；缓存键包含模型、精确 revision、内容或查询哈希。Redis 异常或缓存负载不合法时记录日志并回源，不把缓存作为业务真相。
- SQLite 迁移把 `search_text` 作为 PostgreSQL 派生列，不改变源库快照哈希。Alembic 已在带 pgvector 0.8.3 的 PostgreSQL 16 实例完成空库升级、降级后重升和漂移检查；GIN `gin_trgm_ops` 与 HNSW `vector_cosine_ops` 索引参数已从真实数据库确认。
- Code Review 修复了 Core Connection 查询实体导致的行形状错误、全局 HNSW 先近邻后按任务过滤造成召回不足、损坏缓存被直接返回、缓存向量未校验、构造失败资源泄漏和关闭顺序问题。过滤式 HNSW 查询现在使用 `iterative_scan=strict_order`。
- 两个模型权重保存在 `D:\demo-runtime\models\huggingface` 并按固定 revision 校验。真实 CPU 测试中，首次 BGE Embedding 模型加载并编码 2 条文本约 56.9 秒；61 条款完整建索引、混合召回和重排约 25.9 秒，写入 61 条向量，未触发 90 秒节点超时。
- 任务 6 相关测试、配置/迁移/队列/Checkpoint 外部测试均通过；31 个后端测试模块在每模块 180 秒限制下全部通过，总耗时 250.9 秒。真实 PostgreSQL Alembic 漂移检查返回 `No new upgrade operations detected`。
- 以上真实模型结果验证本地 BGE 与检索基础设施，不代表 DeepSeek 风险分析效果或真实标注集召回率已经达标。默认应用入口和旧检索清理仍属于任务 10。

### 下一个任务

任务 7：实现 LangGraph Postgres Store Memory。
