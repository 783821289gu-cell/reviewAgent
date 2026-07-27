# Agent 现成框架迁移计划

## 1. 文档状态

- 本文档是独立迭代计划，不覆盖此前的 `PLAN.md`、`NEXT_PLAN.md` 或 `AGENT_EVOLUTION_PLAN.md`。
- 当前状态：已完成（Docker 与外部 Langfuse/OTLP 服务按用户指令后置）。
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

### 任务 7：已完成

- RQ Worker 路径接入 `langgraph.store.postgres.PostgresStore`，原始人工反馈保存为不可变 Episode，聚合偏好按合同类型和审查立场隔离保存。现有相似反馈聚合、冲突、过期、置信度衰减、Playbook 优先级和证据约束规则保持不变。
- Memory Store 使用 PostgreSQL `app` schema 中由 LangGraph 包管理的 `store`、`store_vectors` 等表；Alembic 显式排除这些表，避免未来迁移误删包管理数据。业务表 `memory_items`、`semantic_preferences` 只作为一次迁移来源，不维护双写。
- Memory 向量使用与条款检索共享的 `BAAI/bge-m3` Provider、1024 维归一化向量、余弦距离和 HNSW `m=16`、`ef_construction=64`、`ef_search=80`。同一 Worker 内只创建一份 BGE Embedding Provider，避免同一任务重复加载模型。
- 人工反馈先写入 Episode 和聚合偏好，偏好向量在下一次 Worker 检索时按内容哈希惰性建立或刷新；反馈接口不因 BGE 冷启动被阻塞。检索先按合同类型和审查立场硬隔离，再使用向量相似度 0.7、条款类型精确命中 0.15、风险类型精确命中 0.15 排序。
- 稳定反馈幂等键生成稳定 Memory ID；同一幂等键重复提交不会写入第二条 Episode。SQLite/PostgreSQL 迁移保留反馈、聚合偏好、生命周期和置信度，并使用迁移标记保证重复启动不会重复迁移。
- Code Review 修复 Worker 构建失败时资源关闭异常覆盖原始依赖错误的问题；现在逐个尽力关闭检索器、Memory Store 和 Checkpointer，并保留最初的启动异常。
- 32 个后端测试模块在每模块 180 秒限制下全部通过，总耗时约 196 秒。真实 PostgreSQL 16 + pgvector、Redis 验收覆盖 Memory 向量写入/召回、非空旧数据迁移、RQ 事件和 Checkpoint，共 25 项通过；`alembic check` 返回 `No new upgrade operations detected`。
- 当前只有 PostgreSQL/RQ Agent 路径使用 LangGraph Postgres Store。默认 FastAPI/SQLite 入口继续使用原 Memory Repository，直到任务 10 一次性切换；本轮结果不代表 Memory 已证明改善 DeepSeek 风险判断效果。

### 任务 8 完成记录

- PDF 已切换到 Docling `2.115.0` Standard Pipeline，启用按需 RapidOCR 和 TableFormer accurate 表格结构识别，不启用 VLM、远程服务、外部插件、图片描述或图表提取。DOCX 使用同一 Docling 文档模型读取段落与表格。
- 解析结果继续映射为现有 `ContractDocument` / `TextBlock`，保留稳定块 ID、原文顺序、页码、top-left `bbox`、`page_map` 和条款/证据位置契约。Docling 将编号标题识别为 `list_item` 时，适配层恢复其结构化 marker，避免条款边界静默合并。
- Layout 固定 revision `8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`，TableFormer 固定 `v2.3.0`，模型准备脚本只下载实际使用的 accurate 权重与 RapidOCR torch/chinese。模型、Hugging Face 缓存和临时目录均位于 `D:\demo-runtime`；Layout 和 TableFormer 权重分别按官方 SHA-256 验证，解析启动前还会拒绝缺失或零字节的固定模型文件。
- PDF 解析内部时限为 300 秒；活动 Agent 仅对 PDF 解析节点使用 330 秒边界，其他工具继续使用 90 秒节点边界。应用层累计任务计时器没有恢复。
- 回归覆盖中英文文本 PDF、扫描 OCR、PDF 表格、空白、加密、复杂字体、损坏文件、合法 DOCX 段落/表格以及条款与证据位置。扫描 PDF 浏览器流程已验证进入合同类型判断而非伪造 `PARSE_FAILED`。
- 用户提供的 `Confidentiality Agreement.pdf` 由真实 Docling 模型在 12.269 秒内解析为 5 页、133 个正文块，133 个块均带页码和四元 `bbox`；文件未复制到仓库，也未在本任务调用 DeepSeek。
- 32 个后端测试模块在每模块 300 秒限制下全部通过，审查修复后的最终一轮耗时 547.6 秒；直接受影响的 Python 编译、JS 语法、依赖检查、PDF/文档/配置/节点超时测试和 1 个 Chromium 扫描 OCR 用例均通过。外部 PostgreSQL/Redis opt-in 测试本轮未运行，因为 PostgreSQL 与 Worker 当时处于停止状态；没有将其记录为通过。

### 任务 9 完成记录

- 14 个显式 Tool Contract 均绑定 Pydantic 输入/输出模型，`/api/tools` 返回对应 JSON Schema；14 个 LangChain `StructuredTool` 直接按现有 `tool_registry` 生成，没有新增第二份工具清单。
- 外部适配层统一处理 Pydantic 输入校验、领域 dataclass/列表 JSON 序列化、必要的输出对象包装和输出模型校验；内部 Agent 仍调用原 Registry 函数，不改变 LLM Provider 或领域流程。
- MCP 固定 `mcp==1.28.1`，使用 Streamable HTTP 挂载 `/mcp`，默认关闭。启用时要求 `REVIEW_AGENT_HOST=127.0.0.1`，并执行请求 IP 回环校验；只暴露 11 个工具，排除 `parse_document`、`write_memory` 和 `generate_report`。
- OpenTelemetry 固定 1.44.0，通过 OTLP HTTP Exporter 可选发送工具 Span。默认关闭；Span 只记录任务/Trace/Step 标识、工具、重试、状态和耗时，异常只记录类型，不发送合同、Prompt、证据、Memory、输入输出或凭据。Langfuse 使用原生 OTLP endpoint，不增加 Langfuse SDK。
- 依赖解析实际出现 MCP 将 Starlette 升级到与 FastAPI 不兼容的问题；最终固定 `starlette==0.47.3` 与 `sse-starlette==3.0.3`，`pip check` 已恢复通过。
- Code Review 补齐 FastAPI 生命周期中的 MCP Session Manager，并把 Agent、OpenTelemetry 与轮转日志按顺序纳入同一清理回调；即使 Agent 关闭失败，OTel flush 和日志句柄关闭也继续执行。Pydantic 模型不再全局裁剪字符串，避免合同原文和证据在外部工具边界被静默改写。
- 34 个后端测试模块、305 项测试在每模块 300 秒限制下全部通过，总耗时 525.2 秒；Python 编译、`pip check`、差异空白检查、MCP `initialize` / `tools/list` 协议调用、14 个 StructuredTool 生成和 OTel 脱敏 Span 均已验证。没有连接外部 OTLP/Langfuse 服务；Docker 仍按用户指令后置，该运行环境验收保留到任务 10。

### 任务 10 完成记录

- 默认 FastAPI 入口已一次性切换为 `PostgreSQL -> Redis/RQ -> LangGraph -> PostgreSQL`。API 只负责持久化任务并入队，RQ Worker 复用预热后的 BGE、Checkpoint、Memory Store 和 LangGraph Agent；SSE 从 PostgreSQL 读取严格递增事件，并以 Redis 通知减少轮询延迟。
- PostgreSQL `app` schema 已成为任务、文档、条款、风险、日志、事件和审计数据的唯一生产真相源。应用启动时会同时检查 PostgreSQL、Redis 和 RQ Worker；运行中 PostgreSQL 失联时 `/health` 与新任务入口返回真实 `503`，不会创建无法执行的假成功任务。
- 切换前建立 PostgreSQL custom-format 备份 `D:\demo-runtime\backups\pre-cutover-review-agent-20260727-201704.dump`，SHA-256 为 `F4C1A625E396BA0CD195A897E50E9396993D64DE74B715D1DB5F18D7AE1E433C`；上传文件归档 SHA-256 为 `E76753E838349456C10FA847D9AD0E7813557D32E59A5B57F7936344667CA044`。
- 一次性迁移合并了仓库 SQLite 的 170 条 Memory 与 D 盘运行 SQLite 的 100 条 Memory，共恢复 19 个历史任务、270 条原始反馈和 5 条聚合偏好；重复执行迁移保持任务 ID、事件序号、规范化 JSON 哈希和行数一致。19 份历史上传文件均按记录哈希恢复，原 SQLite 只读保留为归档。
- 旧的 2,081 行手写编排实现 `legacy_review_service.py` 已删除；LangGraph 继续复用抽离后的领域执行辅助，不再由第二套状态机控制流程。兼容 SQLite Repository 仅供旧数据迁移、隔离评估和依赖注入测试使用，生产 `main` 导入不会加载该包，也不会构造隐藏的全局 Agent。
- Code Review 修复了三类切换风险：每次进度事件全量重载大型任务状态导致的性能退化、API 仅在启动时检查依赖导致运行中 PostgreSQL 断开仍接收任务、以及兼容 Agent 在 import 阶段隐式创建 SQLite 状态。当前执行控制只读取轻量状态/租约/最新事件号，跨进程写入使用 PostgreSQL advisory lock，完整状态仅在外部事件变化时刷新。
- 切换前使用用户提供的 `Confidentiality Agreement.pdf` 完成真实 DeepSeek 验收：任务 `task_5f3f4586c979` 持续约 1,435.7 秒，解析 61 条条款、构建 29 个上下文、产生 195 个以上持久事件并进入人工复核，随后完成逐条反馈、Memory 更新和报告生成；长任务没有被累计运行时间误报超时。
- 切换后使用同一 PDF 完成本地结构化模式验收：任务 `task_bba1eb76fcbb` 持续约 380.6 秒，解析 61 条条款、构建 29 个上下文、形成 5 条证据验证风险并到达 `EVIDENCE_VERIFIED`；过程事件持续可见，重启后可从 PostgreSQL 恢复。
- 最终后端全量回归为 34 个模块、311 项测试，耗时 539.1 秒，全部通过且无模块超时。真实 PostgreSQL/Redis/RQ、Postgres Checkpointer 与 LangGraph Store 外部集成另有 29 项测试通过，耗时 25.2 秒；`alembic check`、`pip check`、Python 编译和 `git diff --check` 均通过。
- Docker、Compose 和外部 Langfuse/OTLP 服务没有部署，也不作为本轮完成条件。MCP 与 OTLP 继续默认关闭；是否引入 Docker 或部署独立可观测服务属于后续独立计划，不得把本地 Span 测试写成外部服务已上线。

### 迁移结论

本计划的 10 个任务均已完成并经过独立验证。当前没有下一项未完成的 Agent 框架迁移任务；后续新增范围应创建新的独立 Plan，不覆盖本文件。

迁移后的 Agent 模块化与安全并行优化独立记录在 `AGENT_MODULARITY_PERFORMANCE_PLAN.md`，本文件继续作为一次性框架迁移的历史事实保留。
