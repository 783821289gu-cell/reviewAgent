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

### 下一个任务

任务 3：接入 Redis、RQ Worker 和基于 PostgreSQL 查询游标的跨进程 SSE 事件流。
