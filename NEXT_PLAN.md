# 后续技术与实施计划

## 1. 文档状态

本文件记录 ContractReviewAgent 当前未实现能力的后续技术方案和实施顺序。

本文件中的内容全部是待做计划，不代表已经实现。现有第一版范围仍以 `SPEC.md`、`PLAN.md` 和 `TASKS.md` 为准；实施每个阶段前，仍需按 `constitution.md` 单独确认当前任务范围。

## 2. 后续目标

后续开发目标是在保留现有可演示闭环的前提下，补齐以下缺口：

1. 用 FastAPI 和 Uvicorn 替换后端 `ThreadingHTTPServer`。
2. 由 FastAPI 统一托管前端静态文件，移除前端 `python -m http.server` 启动方式。
3. 识别并拒绝非 NDA / 保密协议文件。
4. 接入真实外部 LLM，同时保留可测试的本地结构化模式。
5. 让甲方、乙方审查立场实质影响规则、等级和修改建议。
6. 持久化任务、条款、风险、事件、日志和上传文件，支持服务重启后的任务恢复。
7. 将当前词频稀疏向量升级为真实语义 Embedding 召回。
8. 建立带人工标注的效果型评测，不再只验证流程跑通。
9. 增强 PDF 解析边界，并明确扫描件和 OCR 的处理方式。
10. 增加真实 token/cost 计量、浏览器端到端验证和可追踪 Git 交付。

## 3. 总体技术选择

| 领域 | 后续选择 | 选择原因 |
|---|---|---|
| Web 框架 | FastAPI | 提供结构化请求校验、文件上传、异常处理、OpenAPI 和 ASGI 能力 |
| 运行服务器 | Uvicorn | 作为 FastAPI 的 ASGI 运行入口，支持流式响应 |
| 请求模型 | Pydantic | 约束反馈、局部审查、报告和恢复请求，不再手工解析 JSON 字段 |
| 文件上传 | FastAPI `UploadFile` + `python-multipart` | 替换手写 multipart boundary 解析，并支持分块读取和大小限制 |
| SSE | `StreamingResponse` | 保留现有 `EventSource` 协议和 `review_event` 事件格式 |
| 前端托管 | FastAPI `StaticFiles` | 前后端同源启动，移除独立 `http.server` |
| API 测试 | FastAPI `TestClient` + `httpx` | 在不占用真实端口的情况下验证完整 HTTP 契约 |
| 业务持久化 | SQLite + 现有 `sqlite3` | 复用当前 Memory 技术，不为本地 MVP 引入数据库服务器或 ORM |
| LLM 接入 | 单一 `LLMProvider` 接口 + OpenAI-compatible HTTP 适配器 | 隔离供应商差异，并保留本地测试模式 |
| 结构化输出 | Pydantic 模型校验 | 保留一次重试，仍失败则进入明确异常或人工复核 |
| 语义检索 | Embedding API + 当前合同内内存索引 + 规则 rerank | 合同规模小，不需要独立向量数据库 |
| 评测 | JSON 标注集 + 可重复评测脚本 | 同时验证流程和风险识别效果 |
| 浏览器验收 | Playwright | 验证上传、SSE、风险跳转、反馈、报告和评测主流程 |

本阶段明确不引入 SQLAlchemy、Celery、Redis、PostgreSQL、独立向量数据库、Kubernetes 和多租户权限系统。只有出现经验证的容量或并发需求后再评估。

## 4. 目标运行形态

```text
Browser
  |
  | HTTP / SSE / Static Files
  v
FastAPI + Uvicorn
  |-- API Routers
  |-- SSE StreamingResponse
  |-- Frontend StaticFiles
  |-- ReviewOrchestratorAgent
  |-- Explicit Tool Registry
  |-- LLMProvider / EmbeddingProvider
  |-- SQLite Repositories
  `-- Local uploads / reports / evaluation artifacts
```

FastAPI 只替换传输层和应用启动方式。`ReviewOrchestratorAgent`、`tool_registry`、工具契约、风险验证规则和报告过滤逻辑继续作为业务层，不迁入路由函数。

## 5. FastAPI 迁移方案

### 5.1 迁移范围

移除以下现有实现：

1. `BaseHTTPRequestHandler` 路由分发。
2. `ThreadingHTTPServer` 启动入口。
3. 手写 JSON body 读取。
4. 手写 multipart boundary 解析。
5. 手写 CORS header 和 OPTIONS 响应。
6. 前端 `python -m http.server` 启动方式。

保留以下外部契约：

| 方法 | 路径 | 迁移要求 |
|---|---|---|
| GET | `/health` | 状态码和现有字段保持兼容 |
| GET | `/api/statuses` | 状态列表保持兼容 |
| GET | `/api/tools` | 继续返回 11 个显式工具契约 |
| POST | `/api/tasks` | 保留 multipart 文件和 `review_position` 字段 |
| GET | `/api/tasks/{task_id}` | 保留任务 JSON 结构 |
| GET | `/api/tasks/{task_id}/events` | 保留 SSE `review_event` 名称和数据结构 |
| POST | `/api/tasks/{task_id}/feedback` | 保留反馈动作和响应结构 |
| POST | `/api/tasks/{task_id}/report` | 保留报告过滤和状态转换 |
| POST | `/api/local-review` | 保持局部结果不污染正式风险和 Memory |
| POST | `/api/evaluation/run` | 保留手动评测入口 |

### 5.2 后端模块边界

计划新增或调整：

```text
backend/app/
  main.py                 FastAPI app 和 lifespan
  api/
    dependencies.py       Settings、Repository、EventStore 依赖
    errors.py             统一异常到 HTTP 响应映射
    schemas.py            API 请求和响应模型
    routes/
      health.py
      tasks.py
      feedback.py
      reports.py
      local_review.py
      evaluation.py
  services/               保留现有业务服务
  tools/                  保留显式 Tool Registry
```

路由只负责参数读取、调用应用服务和映射状态码，不直接实现审查规则，也不绕过 `tool_registry` 调用底层工具。

### 5.3 上传处理

1. 使用 `UploadFile` 获取文件名和文件流。
2. 以固定大小分块读取，累计字节数超过 `REVIEW_AGENT_MAX_UPLOAD_BYTES` 时立即拒绝。
3. 使用 `ntpath.basename` 或等价方式清理客户端文件名。
4. 同时校验扩展名、MIME 和基础文件签名，不只相信客户端 `content_type`。
5. 上传内容在创建任务前写入受控目录，并记录 SHA-256。
6. 空文件、超限文件、类型不支持和解析失败返回稳定、用户可理解的错误结构。

### 5.4 SSE 处理

1. 使用异步生成器包装现有事件等待逻辑。
2. `StreamingResponse` 返回 `text/event-stream`。
3. 保留 `event: review_event`、`id` 和 JSON `data`。
4. 空闲时发送 keep-alive 注释。
5. 检测客户端断开，停止对应响应生成器，不能让断开的连接永久占用线程。
6. 任务进入终态后发送完最后事件并关闭流。

### 5.5 前端静态托管

1. API 路由注册完成后，再挂载 `frontend/` 静态目录。
2. `/` 返回 `frontend/index.html`。
3. 前端 `API_BASE_URL` 改为同源地址，不再硬编码 `http://127.0.0.1:8000`。
4. 默认只启动一个 Uvicorn 进程即可访问 API 和页面。
5. 如保留独立前端开发服务器，CORS 只允许配置中的开发源，不使用生产环境通配符。

### 5.6 启动和配置

开发启动命令目标为：

```powershell
python -m uvicorn main:app --app-dir backend/app --host 127.0.0.1 --port 8000 --reload
```

普通运行命令目标为：

```powershell
python -m uvicorn main:app --app-dir backend/app --host 127.0.0.1 --port 8000
```

通过 FastAPI lifespan 初始化数据库表、任务仓储和运行中任务注册表；关闭时停止新任务并完成必要资源释放。

### 5.7 FastAPI 迁移验收

1. 现有 API 路径和成功响应字段保持兼容。
2. 原有 40 个测试全部通过，或只因测试传输层变化做等价迁移，不能弱化断言。
3. 新增 FastAPI TestClient 契约测试，覆盖所有路由。
4. 上传、SSE、反馈、报告和评测的主流程通过。
5. 不再存在手写 multipart 解析代码。
6. 不再导入或启动 `ThreadingHTTPServer`。
7. README 不再要求运行 `python -m http.server`。
8. 单个 Uvicorn 服务能同时提供 Web Workbench 和 API。

## 6. NDA 类型识别方案

### 6.1 技术方案

在文档解析后、条款审查前增加 `classify_contract_type` 工具，并注册到 `tool_registry`。

结构化输出至少包含：

```json
{
  "contract_type": "NDA",
  "confidence": 0.91,
  "evidence": ["保密信息", "披露方", "接收方"],
  "decision": "SUPPORTED"
}
```

识别采用两级策略：

1. 确定性特征识别标题、主体角色和保密义务结构。
2. 特征置信度不足时调用真实 LLM 分类器，但只能返回受约束枚举。

非 NDA 返回 `UNSUPPORTED_CONTRACT_TYPE`，停止 Playbook 检索，不生成正式风险。低置信度返回 `NEED_MANUAL_REVIEW`，不默认按 NDA 继续。

### 6.2 验收

1. 标准 NDA 能进入审查。
2. 采购、服务、劳动等非 NDA 文件不会生成 NDA 正式风险。
3. 类型不明确时进入人工复核。
4. 分类结论包含可显示的依据和置信度。
5. 新工具调用进入 StepLog。

## 7. 真实 LLM 接入方案

### 7.1 Provider 边界

定义最小 `LLMProvider` 接口，只承载结构化生成所需能力：

1. 模型名称。
2. 超时。
3. 结构化输出 schema。
4. 请求 ID。
5. prompt/completion token。
6. 调用耗时和费用计算输入。

第一版实现两个模式：

1. `local_structured`：仅用于本地开发和确定性测试，不能记录为外部 LLM。
2. `openai_compatible`：通过配置的 Base URL、API Key 和 Model 调用真实服务。

不在代码中硬编码 API Key、供应商 URL 或模型名称。

### 7.2 调用位置

真实 LLM 只用于已确认的工具：

1. `analyze_risk`。
2. `generate_revision`。
3. `extract_key_fields`，在规则抽取不足时使用。
4. `classify_contract_type`，只作为低置信度补充。

文档解析、Playbook 检索、Memory、报告和证据原文包含关系仍保持确定性。

### 7.3 失败处理

1. 超时、限流和临时服务错误可重试一次。
2. JSON 或 schema 不合法可带校验错误重试一次。
3. 重试后失败进入 `LLM_OUTPUT_INVALID` 或 `NEED_MANUAL_REVIEW`。
4. 失败结果不能进入正式风险列表。
5. 未配置外部 LLM 时，界面必须显示当前为本地模式，不能伪装成真实模型审查。

### 7.4 验收

1. 能通过环境变量切换本地和真实 LLM 模式。
2. 外部调用返回结构化结果并通过现有 RiskFinding 校验。
3. 日志记录真实模型、请求 ID、token、耗时和错误，不记录 API Key 或完整敏感 prompt。
4. 结构化输出失败重试一次后进入明确状态。
5. 本地模式和外部模式测试互不依赖。

## 8. 审查立场差异化方案

### 8.1 Playbook 数据结构

将同一风险规则的立场差异放入规则内的 `positions` 配置，不复制整套规则：

```json
{
  "positions": {
    "甲方": {
      "severity_default": "中",
      "risk_focus": "保护披露信息和追责能力",
      "revision_template": "..."
    },
    "乙方": {
      "severity_default": "高",
      "risk_focus": "限制接收方义务和赔偿边界",
      "revision_template": "..."
    }
  }
}
```

上下文、风险分析和修改建议都必须使用已解析的立场配置，不能只传递一个未被消费的字符串。

### 8.2 验收

1. 同一合同以甲方和乙方审查时，至少在适用风险、等级、理由或建议之一出现可解释差异。
2. 差异能追溯到 Playbook 立场配置和合同证据。
3. 没有立场配置的规则不能静默使用任意默认值。
4. 报告写明审查立场。

## 9. 任务持久化和恢复方案

### 9.1 数据范围

在现有 SQLite 中增加以下持久化对象：

1. `review_tasks`：任务状态、文件元数据、审查立场、当前节点、错误。
2. `documents`：文件哈希、解析元数据、受控文件路径。
3. `clauses`：条款正文、类型、关键字段、原文位置。
4. `risk_findings`：分析结果、证据、复核状态、报告选择。
5. `step_logs`：节点、工具、输入输出摘要、耗时、token/cost、错误。
6. `task_events`：SSE 事件序号、状态、消息和时间。
7. 现有 `memory_items`：保持兼容。

复杂结构第一阶段可以使用 JSON TEXT 字段保存，但主键、任务 ID、状态、风险 ID、条款 ID 和时间字段必须独立可查询。

### 9.2 Repository 边界

引入轻量 Repository，不引入 ORM：

1. `TaskRepository`。
2. `DocumentRepository`。
3. `ReviewResultRepository`。
4. `EventRepository`。
5. 现有 `MemoryRepository` 行为由当前服务平滑迁入。

服务层不能直接散落 SQL；每次状态更新、结果保存和事件追加必须在明确事务中完成。

### 9.3 恢复策略

1. 上传文件按任务保存到受控目录并记录哈希。
2. 每个状态节点完成后再提交状态和结果。
3. 服务启动时扫描非终态任务。
4. 已完成节点不重复执行；从最后一个完整状态继续。
5. 工具步骤必须满足可重入或通过输入哈希防止重复副作用。
6. `write_memory` 和 `generate_report` 使用幂等键，避免恢复时重复写入。
7. 无法安全恢复的任务进入 `NEED_MANUAL_REVIEW` 并说明原因。

### 9.4 验收

1. 服务重启后仍能查询历史任务、风险、日志和事件。
2. 审查中断后可从最近成功节点恢复。
3. 恢复不会重复写 Memory 或重复生成多份报告。
4. 文件缺失、哈希不匹配时停止恢复并提示用户。
5. SQLite 写失败不能被记录为任务成功。

## 10. 语义检索方案

### 10.1 技术方案

保留当前合同内检索边界，将 `EmbeddingProvider` 与 rerank 解耦：

1. 当前条款、风险类型和 Playbook 检查点组成查询文本。
2. 调用真实 Embedding API 生成定长向量。
3. 当前合同条款向量在任务内缓存，并随任务持久化或可重建。
4. 使用余弦相似度召回候选。
5. 继续使用条款类型、风险关联、关键字段等规则 rerank。
6. 返回向量模型、相似度、rerank 因子和最终分数。

合同条款数量有限，第一阶段使用内存列表和标准余弦计算，不引入 pgvector、Qdrant 或 Chroma。

### 10.2 降级原则

1. 测试可使用确定性本地 Embedding stub。
2. 真实模式下 Embedding 失败进入 `RETRIEVAL_FAILED`，不能悄悄改用词频结果并声称语义检索成功。
3. 若产品需要显式降级，日志和界面必须显示 `lexical_fallback`。

### 10.3 验收

1. 日志显示真实 Embedding 模型和调用状态。
2. 相关条款结果包含向量相似度和 rerank 明细。
3. 使用人工标注相关条款集计算 Recall@K。
4. 语义召回相对当前词频基线有可测提升，否则不替换基线。

## 11. 效果型评测方案

### 11.1 数据集

在现有 10 份流程样本之外，增加人工标注 JSON：

1. 合同类型标签。
2. 条款边界和条款类型。
3. 预期命中规则。
4. 预期风险类型。
5. 可接受风险等级范围。
6. 标准证据文本或允许 span。
7. 相关条款 ID。
8. 是否应进入人工复核。

样本必须继续使用合成、公开授权或已脱敏且获得授权的文本，并记录来源。

### 11.2 指标

1. NDA 分类准确率和非 NDA 拒绝率。
2. 条款切分准确率。
3. 条款类型准确率。
4. Playbook Recall@K。
5. 相关条款 Recall@K。
6. 风险 Precision、Recall、F1。
7. 证据 span 命中率。
8. 高风险和低置信度人工复核触发率。
9. 报告过滤正确率。
10. 工具调用成功率和端到端任务成功率。

### 11.3 验收

1. 流程评测与效果评测分开报告。
2. 每个指标包含样本数、通过数和失败样本，不只给总分。
3. 评测输出记录代码版本、Playbook 版本、模型和参数。
4. 没有达到预设阈值时只能报告实际结果，不能声明生产级准确率。

## 12. PDF 增强方案

### 12.1 范围

1. 使用成熟 PDF 文本解析库处理文本型 PDF。
2. 保留页码、文本块和条款位置映射。
3. 检测扫描件或无有效文本 PDF。
4. 扫描件第一阶段返回“需要 OCR”的明确错误，不伪造空文本解析成功。
5. OCR 作为独立可选阶段评估，不默认引入本地大型模型。

### 12.2 验收

1. 普通中文和英文文本型 PDF 能解析。
2. 页码和证据定位可回溯。
3. 扫描件被明确识别，不进入正式风险分析。
4. 复杂字体或加密 PDF 失败时返回用户可理解原因。

## 13. 日志、token/cost 和任务恢复可观测性

1. 外部 LLM 和 Embedding 调用记录供应商请求 ID、模型、token、耗时和错误类型。
2. 成本按配置的模型价格表计算，价格未知时显示“未配置”，不能写零成本。
3. 日志不保存 API Key、Authorization header 或完整敏感合同 prompt。
4. 每个任务记录 `trace_id`，每次工具调用记录 `step_id`。
5. 恢复任务记录原任务 ID、恢复次数和恢复起点。
6. 前端继续展示用户可理解的摘要，不直接暴露内部堆栈。

验收要求是真实外部调用的 token 汇总能与供应商返回字段对应；本地模式继续显示 `no_external_llm`。

## 14. 浏览器端到端和交付方案

### 14.1 E2E 覆盖

Playwright 至少覆盖：

1. 打开 FastAPI 托管的首页。
2. 未选择文件或立场时不能提交。
3. 上传 DOCX 并收到 SSE 终态。
4. 点击风险跳转和证据高亮。
5. 框选文本进行局部审查。
6. 采纳、忽略和修改动作写入 Memory。
7. 报告导出内容和过滤正确。
8. 基础评测结果可查看。
9. 非 NDA、文件超限、解析失败和服务错误提示可见。

### 14.2 Git 交付

1. 实施前先提交当前可运行基线，避免未跟踪代码继续累积。
2. 每个阶段独立提交，提交信息说明行为变化和验证结果。
3. 不把 SQLite 数据库、上传合同、报告、评测输出、密钥或本机路径提交到仓库。
4. 合并前运行全量单元、API 契约、评测和 E2E 测试。
5. README 只描述实际已完成能力。

## 15. 实施顺序

后续按以下顺序执行，每个阶段单独开发、Review、QA 和验收，不在一轮中混做：

### 阶段 1：建立可追踪基线

1. 整理当前 Git 未跟踪文件。
2. 运行并记录现有 40 个测试和 10 份流程评测。
3. 固化当前 API 响应样例，作为 FastAPI 迁移兼容基线。

阶段出口：当前版本可恢复、可对比，迁移失败时能回到明确基线。

### 阶段 2：FastAPI 传输层迁移

1. 引入 FastAPI、Uvicorn、python-multipart 和 httpx。
2. 迁移现有全部 API 和错误映射。
3. 迁移 SSE。
4. 统一托管前端静态文件。
5. 删除旧 `ThreadingHTTPServer` 和手写 multipart 实现。

阶段出口：单进程启动页面和 API，现有外部行为保持兼容。

### 阶段 3：任务持久化和恢复

1. 建立 SQLite Repository 和表结构。
2. 持久化上传、状态、条款、风险、日志和事件。
3. 实现幂等写入和断点恢复。

阶段出口：服务重启后任务仍可查询并按规则恢复。

### 阶段 4：NDA 类型识别

1. 增加显式分类工具。
2. 增加非 NDA 和低置信度状态处理。
3. 补充非 NDA 测试集。

阶段出口：非 NDA 不会进入 NDA 正式风险链路。

### 阶段 5：立场差异化

1. 扩展 Playbook 立场字段。
2. 调整上下文和建议生成。
3. 增加同合同双立场对照测试。

阶段出口：甲乙方结果存在可追溯的业务差异。

### 阶段 6：真实 LLM

1. 实现 Provider 配置和真实结构化调用。
2. 接入风险分析和修改建议。
3. 接入 token/cost 日志。
4. 保留本地测试模式。

阶段出口：真实模式确实发起外部调用，失败不会伪造结果。

### 阶段 7：真实语义检索

1. 实现 EmbeddingProvider。
2. 建立任务内条款向量索引。
3. 保留并验证 rerank。
4. 与词频基线做 Recall@K 对比。

阶段出口：语义检索有真实模型和可量化效果。

### 阶段 8：效果型评测

1. 建立人工标注格式和样本。
2. 实现风险、证据、检索和人工复核指标。
3. 记录模型、规则和代码版本。

阶段出口：能够说明系统实际效果和失败样本，而不只是流程成功。

### 阶段 9：PDF 边界增强

1. 替换或增强文本型 PDF 解析。
2. 增加页码映射和扫描件识别。
3. 决定是否单独实施 OCR。

阶段出口：支持范围和失败边界可验证、可解释。

### 阶段 10：浏览器验收和发布

1. 完成 Playwright 主流程和错误流。
2. 全量测试、评测和浏览器验证。
3. 更新 README、提交 Git 并推送远端。

阶段出口：文档、代码、测试、评测和仓库状态一致。

## 16. 依赖与配置计划

计划新增最小运行依赖：

1. `fastapi`。
2. `uvicorn`。
3. `python-multipart`。
4. `httpx`。
5. 成熟 PDF 解析库，具体选择在 PDF 阶段单独确认。

计划新增环境变量：

```text
REVIEW_AGENT_LLM_MODE=local_structured|openai_compatible
REVIEW_AGENT_LLM_BASE_URL=
REVIEW_AGENT_LLM_API_KEY=
REVIEW_AGENT_LLM_MODEL=
REVIEW_AGENT_LLM_TIMEOUT_SECONDS=60
REVIEW_AGENT_EMBEDDING_MODE=local_sparse|openai_compatible
REVIEW_AGENT_EMBEDDING_MODEL=
REVIEW_AGENT_UPLOAD_DIR=backend/app/data/uploads
REVIEW_AGENT_ALLOWED_ORIGINS=http://127.0.0.1:8000
```

密钥只允许通过环境变量或本机未提交配置注入，`.env.example` 只能保留空值和说明。

## 17. 全量验证口径

后续版本只有同时满足以下条件，才允许声明完成：

1. FastAPI 和 Uvicorn 是唯一后端启动方式。
2. 页面由 FastAPI 提供，不再依赖 `python -m http.server`。
3. 现有 API 契约没有未经说明的破坏性变化。
4. 非 NDA 不生成 NDA 正式风险。
5. 甲乙方结果存在可解释差异。
6. 真实 LLM 和 Embedding 调用可追踪且失败状态真实。
7. 服务重启后任务、风险、日志和反馈不丢失。
8. 流程评测和效果评测都能运行并报告失败样本。
9. 文本型 PDF 和扫描件边界经过验证。
10. 单元测试、API 契约测试、评测和浏览器 E2E 全部运行并报告真实结果。
11. Git 工作区没有应提交但未跟踪的实现文件。
12. README、SPEC、PLAN、后续计划和实际代码状态一致。

## 18. 参考依据

1. FastAPI 文件上传：`https://fastapi.tiangolo.com/tutorial/request-files/`
2. FastAPI StreamingResponse：`https://fastapi.tiangolo.com/reference/responses/`
3. FastAPI 静态文件：`https://fastapi.tiangolo.com/tutorial/static-files/`
4. FastAPI TestClient：`https://fastapi.tiangolo.com/tutorial/testing/`
5. FastAPI lifespan 测试：`https://fastapi.tiangolo.com/advanced/testing-events/`
