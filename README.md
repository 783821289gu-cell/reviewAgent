# ContractReviewAgent

当前实现范围：`TASKS.md` 的任务 9、`TASKS_2.md` 的任务 10、`UI_REDESIGN_PLAN.md` 的工作台界面迭代，以及 `TASKS_AGENT.md` 的任务 10。系统支持 AgentState、显式 Tool Registry、DeepSeek OpenAI-compatible Provider、受控 Planner/Router、一次检索修复、受控 Critic、确定性 Evidence 最终准入、Prompt Injection 阻断、真实 token 预算、Memory 生命周期、可恢复执行、流式状态事件和脱敏 Agent Trace。HTTP 传输层使用 FastAPI / Uvicorn，并由同一服务托管前端；任务、上传文件、文档、条款、风险、日志和事件接入 SQLite 持久化。浏览器测试覆盖主流程、错误流、Agent 状态展示、三视口布局和基础可访问性。

## 当前已实现

1. FastAPI / Uvicorn 后端服务启动入口。
2. 后端健康检查接口。
3. 审查状态模型：`START`、`UPLOAD_RECEIVED`、`DOCUMENT_PARSED`、`CONTRACT_TYPE_CLASSIFIED`、`CLAUSES_STRUCTURED`、`PLAYBOOK_RETRIEVED`、`CONTEXT_BUILT`、`RISK_ANALYZED`、`EVIDENCE_VERIFIED`、`HUMAN_REVIEW_PENDING`、`MEMORY_UPDATED`、`REPORT_READY`、`UNSUPPORTED_CONTRACT_TYPE`、`PARSE_FAILED`、`RETRIEVAL_FAILED`、`LLM_OUTPUT_INVALID`、`EVIDENCE_MISSING`、`NEED_MANUAL_REVIEW`、`CANCEL_REQUESTED`、`CANCELLED`、`NODE_TIMEOUT`、`TASK_TIMEOUT`、`TASK_ERROR`。
4. FastAPI 同源托管前端页面和静态资源。
5. 独立的新建合同审查入口、合同文件上传和甲方 / 乙方审查立场选择。
6. 未选择合同文件或审查立场时不能上传解析。
7. Workbench 布局：全局工作区导航、顶部任务命令栏、左侧上下文与进度、中间合同原文与证据、右侧风险检查器、底部执行记录抽屉。
8. DOCX 段落和表格文本解析。
9. 基于 `pdfplumber` 的中英文文本型 PDF 解析，保留页码、文本块顺序和边界坐标，并将条款位置回溯到对应 PDF 页和块。
10. 条款编号、条款正文、条款类型、关键字段和原文位置结构化。
11. 完整显式 `tool_registry` 字典和工具输入输出契约。
12. 当前解析链路通过 `ReviewOrchestratorAgent` 调度，并只通过 `tool_registry` 调用工具。
13. 后端通过 Server-Sent Events 输出任务状态事件。
14. 前端实时展示状态进度和执行日志。
15. 本地 JSON NDA Risk Playbook，覆盖第一版 8 类核心风险。
16. `retrieve_playbook_rules` 已通过显式 `tool_registry` 调用，并按合同类型、条款类型、关键字段和审查立场检索规则。
17. 前端展示命中的 Playbook 规则；未命中规则时不会生成正式风险。
18. `retrieve_related_clauses` 已通过显式 `tool_registry` 调用；支持 `local_sparse` 离线测试模式和 `openai_compatible` 真实 Embedding 模式，在当前合同条款内合并关键词与 Embedding 候选、去重，并执行动态 Top-K 和可解释 rerank。
19. `retrieve_memory` 已通过显式 `tool_registry` 调用；支持从调用方提供的 `memory_items` 中过滤召回，也支持从 SQLite Memory 中按合同类型、条款类型、风险类型和审查立场检索历史反馈。
20. 后端构建风险分析上下文，包含当前条款、命中规则、相关条款、相关 Memory 字段、输出约束和证据约束。
21. `analyze_risk` 已通过显式 `tool_registry` 调用；新建审查可按任务选择 `local_structured` 或 `openai_compatible`，本地模式不会记录为真实外部 LLM 调用。
22. `verify_evidence` 已通过显式 `tool_registry` 调用，校验 `clause_id`、`evidence_text`、风险原因与证据文本相关性，以及命中规则一致性。
23. `generate_revision` 已通过显式 `tool_registry` 调用，并与风险分析共用结构化 LLM Provider 边界。
24. 前端展示上下文 Trace、已验证风险列表、规则详情、证据文本、风险原因和修改建议。
25. 点击风险可定位到中间合同原文的对应证据；定位只滚动文档区，左右栏保持原位，目标已可见时不重复跳动。
26. 用户可以在合同原文中框选文本并发起局部审查，局部审查结果与正式风险列表分开展示。
27. 局部审查只返回当前框选文本的复核结果，不写入正式风险列表，不写入 Memory。
28. 用户可以对正式风险执行采纳、忽略、修改等级、修改建议，并选择是否加入报告。
29. 所有采纳、忽略和修改动作通过 `tool_registry["write_memory"]` 写入 SQLite Memory。
30. 后续相似审查会通过 `retrieve_memory` 召回相关 Memory；当 Memory 影响修改建议时，风险详情展示历史反馈引用。
31. 用户可以基于正式风险证据触发局部重审，局部结果仍不写入正式风险列表。
32. 用户可以导出 Markdown 审查报告，报告生成通过 `tool_registry["generate_report"]` 调用。
33. 报告包含合同名称、审查立场、Playbook 版本、风险等级、风险原因、原文证据、修改建议和人工反馈状态。
34. 报告只导出用户明确允许进入报告且不处于待人工复核状态的风险，不包含完整执行日志和技术实现细节。
35. `samples/` 提供 23 份项目内合成合同标注，其中基础流程评测固定使用 10 份 NDA；样本来源已在 `samples/README.md` 说明。
36. `evaluation/` 支持手动触发基础评测摘要，覆盖任务是否跑通、文档解析、条款结构化、Playbook 命中、风险证据、Memory 写入、报告导出和失败原因。
37. 全部 HTTP API 已迁移到 FastAPI，请求模型使用 Pydantic 校验，文件上传使用 `UploadFile` 分块读取并校验大小、扩展名、MIME、文件签名、空文件和安全文件名。
38. SSE 使用 `StreamingResponse`，保留 `review_event` 事件格式，并在终态、客户端断开或服务关闭时结束。
39. 默认 CORS 不使用通配符；允许源由 `REVIEW_AGENT_ALLOWED_ORIGINS` 配置。
40. SQLite 使用轻量 Repository 持久化任务、文档、条款、风险、执行日志和 SSE 事件；状态、结果和事件在同一事务中提交。
41. 上传文件保存到 `REVIEW_AGENT_UPLOAD_DIR` 受控目录，数据库只记录任务生成的相对文件名和 SHA-256，不保存客户端原始路径。
42. 服务启动时加载历史任务；非终态任务校验上传文件和检查点后从最后完整节点继续，已成功节点不重复执行。
43. 文件缺失、SHA-256 不一致或检查点数据缺失时，任务进入 `NEED_MANUAL_REVIEW` 并保留实际原因。
44. 人工反馈 Memory 和报告生成使用稳定幂等键，重复请求不会重复写入 Memory 或生成多份同版本报告。
45. 合同类型先执行确定性分类，仅在低置信度时调用 LLM；非 NDA 在 Playbook 检索前停止。
46. 甲方、乙方审查立场会影响 Playbook 规则、风险重点、默认等级和修改建议。
47. OpenAI-compatible Provider 通过环境变量配置 Base URL、API Key、模型和超时；超时、限流、临时错误和结构错误最多重试一次。
48. 外部 LLM 日志记录模型、供应商请求 ID、prompt/completion token、调用耗时、错误类型和成本状态；未知价格显示“未配置”，本地模式显示 `no_external_llm`。
49. Embedding 查询包含当前条款正文、条款类型、风险类型、Playbook 检查点和关键字段；合同条款向量在单次任务上下文构建期间缓存，恢复时使用相同模型和输入重建。
50. 相关条款结果包含 Embedding 模式、模型、向量维度、余弦相似度、rerank 因子和最终分数；前端 Context Trace 展示模型、相似度和最终分数。
51. 外部 Embedding 调用日志记录模型、供应商请求 ID、输入数量、向量维度、耗时和错误类型，不记录 API Key 或完整合同文本；失败时进入 `RETRIEVAL_FAILED`，未启用静默词频降级。
52. `samples/annotations/related_clauses.json` 提供 20 组人工相关条款标注，覆盖 8 类 NDA 风险；离线测试直接运行本地混合检索并计算 Recall@1，不使用语义命中 Stub 代替实际检索结果。
53. `samples/annotations/` 提供 `effect-v2` 人工标注 schema，覆盖 23 份合同、174 条条款、65 条唯一风险条款、20 组 Memory 对照和 10 组 Prompt Injection；当前数据全部为项目内合成夹具。
54. `POST /api/evaluation/effect/run` 独立运行效果评测，不改写现有 `POST /api/evaluation/run` 流程评测响应。
55. 效果评测计算 23 项指标，包括 NDA 分类、非 NDA 拒绝、条款、Playbook/相关条款、风险、证据、人工复核、报告、工具、端到端、Planner、结构化输出、修复、恢复、无证据风险、Memory 和 Injection；token、成本状态和 P95 延迟作为运行测量单独记录。
56. 每个效果指标记录样本数、通过数、失败数、阈值和失败样本；版本化 JSON/Markdown 摘要记录 Git、Playbook、标注、LLM、Embedding 和关键参数。
57. Web 评测面板使用“流程摘要”和“效果摘要”两个独立视图；未达阈值指标按实际失败状态展示。
58. 扫描件、空文本、加密、复杂字体映射失败和损坏 PDF 会进入真实 `PARSE_FAILED` 状态，不生成伪造正文或正式风险。
59. 每个任务持久化 `trace_id`，每次工具调用持久化唯一 `step_id`；旧 SQLite 日志在初始化时生成确定性 Step 标识。
60. 任务恢复次数和恢复起点进入任务快照、API 响应和 Web 执行记录，服务重启后保留。
61. LLM/Embedding 执行摘要展示模型、供应商请求 ID、token 或输入量、耗时、成本状态和错误类型；日志摘要屏蔽密钥、Authorization、完整 prompt 和完整任务正文。
62. SSE 在终态关闭但浏览器未消费最后一帧时，前端使用任务查询接口做一次真实状态对账，不自行构造成功状态。
63. Playwright Chromium E2E 覆盖首页、上传、SSE、风险定位和高亮、框选局部审查、人工反馈、Memory、报告过滤下载、流程评测、非 NDA、超限、解析失败、扫描 PDF 和服务错误提示。
64. Web 前端提供审查、Memory、评测三个真实数据工作区；风险筛选、条款导航、风险证据和人工操作状态在重渲染后保持一致。
65. 桌面端使用独立滚动的三栏工作台，移动端提供风险、合同、执行记录三种模式和固定人工操作栏；本地 Lucide SVG 图标不依赖在线 CDN。
66. Playwright 额外验证 1440×960、1280×800、390×844 三个视口无横向溢出、面板不重叠、图标资源可访问、键盘焦点和减少动态效果设置生效。
67. `plan_review_action` 作为第 13 个显式工具，只能返回四种白名单动作；Orchestrator 校验原因码、当前状态、当前合同条款和全任务一次重试预算后，才允许执行受限检索改写与重新分析。首次 Evidence 失败最多修复一次，再次失败进入 `EVIDENCE_MISSING`；正常确定性成功路径不调用 Planner。
68. `criticize_risk` 作为独立显式工具位于 Analyzer 与 Evidence Verifier 之间，只能返回 `PASS`、`REJECT` 或 `REQUEST_HUMAN_REVIEW` 及固定原因码；它不能返回新证据、条款、规则或风险等级。Critic 冲突进入人工复核，只有确定性 Evidence Verifier 通过的候选才可能进入正式风险列表。
69. Prompt 组装区分系统策略、任务、Playbook、合同数据、相关条款、Memory、证据约束和输出 Schema；合同与 Memory 作为不可信数据处理，注入信号不会触发额外工具或生成未绑定证据的正式风险。
70. Context Budget 使用项目内固定的 DeepSeek tokenizer 计算 token，并记录各上下文类别占用、裁剪原因、最终预算和 Prompt 版本；当前条款、Playbook、Schema 和证据约束不可被预算裁剪。
71. Memory 保留不可变人工反馈，同时聚合可追溯偏好；冲突并存、置信度可衰减、过期只影响派生偏好，不删除审计记录，也不能覆盖 Playbook 或合同证据。
72. 任务支持取消、节点/任务超时、固定错误重试预算、人工恢复和同任务并发保护；恢复历史、父 Step、重试序号、幂等键和决策摘要进入脱敏 Trace。
73. 已使用用户本机 Key 对固定的 2 份合成 NDA 子集完成两次真实 DeepSeek 稳定性运行。两轮均为 `completed_with_failures`；该结果只证明真实调用链已跑通，不代表 23 份完整标注集效果达标。
74. Web Workbench 的执行记录可区分本地模式、DeepSeek 真实调用、Planner、一次检索修复、Critic、Evidence、Memory、Injection 阻断、取消、超时和人工恢复；高风险、证据失败和冲突结果持续显示人工复核提示。
75. 新建审查入口提供任务级 DeepSeek 开关；任务选择、实际调用成功和调用失败分别显示，不把“已选择”伪装成“已调用”。
76. 完整任务状态继续由 SQLite 保存；浏览器只保存当前任务 ID，刷新后通过任务查询接口恢复文档、条款、风险、反馈、日志和事件，后端重启后仍可读取。

## 当前未实现

仍未完成或未达标的事项如下：

1. 未使用 DeepSeek 对 23 份完整标注集完成两轮外部模型稳定性评测；当前真实运行只覆盖固定的 2 份合成 NDA 子集。
2. 真实 DeepSeek 两轮的 Evidence span、Memory 一致性和结构化修复指标未全部达标，且两轮波动明显；不能声明 Agent 效果验收通过。
3. 任务 9 Code Review 后的代码没有再次消耗 DeepSeek 复跑；现有外部结果对应 Review 前的未提交工作区，限制详见 `evaluation/release/agent-evolution-verification.md`。
4. 尚未使用真实外部 Embedding 在完整标注集上复测，默认仍为 `local_sparse`。
5. OCR 尚未实现；扫描件会进入明确的 `PARSE_FAILED`，是否引入 OCR 必须另立迭代计划。

迭代技术方案、实施顺序和验收口径见 `NEXT_PLAN.md`、`TASKS_2.md`、`UI_REDESIGN_PLAN.md`、`AGENT_EVOLUTION_PLAN.md` 和 `TASKS_AGENT.md`；这些文件保留计划形成过程，当前完成状态以代码、测试和本 README 为准。

当前基础评测只验证流程跑通，不声明生产级准确率。人工相关条款集直接运行本地混合检索，不使用语义命中 Stub 代替结果，但仍不代表实际外部 Embedding 模型质量；因此默认正式配置仍为 `local_sparse`。真实 DeepSeek 子集运行验证了外部调用链，不等于证明模型效果稳定或达到生产要求。

当前 `effect-v2` 包含 23 份合同、174 条条款、65 条唯一风险条款、20 组相关条款、20 组 Memory 对照和 10 组 Prompt Injection。默认本地模式完整评测中相关条款 Recall@1 为 `20/20`，但风险 Recall 和 Evidence span 均为 `0.5231`、Memory 一致性为 `0.55`，摘要为 `completed_with_failures`；这些合成样本结果不能外推为生产准确率或真实外部模型效果。

当前 14 个工具均已在 `tool_registry` 中注册契约。`classify_contract_type`、`extract_key_fields`、`analyze_risk`、`criticize_risk`、`plan_review_action` 和 `generate_revision` 具备 LLM Provider 调用边界；默认 `local_structured` 模式下 Critic 和 Planner 使用确定性策略，不记录为真实外部 LLM 调用。

## Agent 架构

```mermaid
flowchart LR
    Web["Web Workbench"] --> API["FastAPI + SSE"]
    API --> Agent["ReviewOrchestratorAgent"]
    Agent --> Registry["显式 tool_registry"]
    Registry --> Parse["Parser / Classifier"]
    Registry --> Retrieve["Playbook + Hybrid Retrieval + rerank"]
    Registry --> Memory["Memory Recall / Lifecycle"]
    Registry --> Analyze["DeepSeek or Local Structured Analyzer"]
    Registry --> Planner["Controlled Planner"]
    Registry --> Critic["Controlled Critic"]
    Registry --> Evidence["Deterministic Evidence Verifier"]
    Planner -->|"最多一次 RETRIEVE_AGAIN"| Retrieve
    Analyze --> Critic --> Evidence
    Evidence -->|"失败或冲突"| Human["Human Review"]
    Evidence -->|"通过"| Result["Formal Risks / Report"]
    Agent --> Trace["Redacted Step Log / Trace"]
    Trace --> Web
```

Planner 只能选择白名单动作，Orchestrator 才能执行工具；Critic 不能创建证据；Evidence Verifier 拥有正式风险最终准入权。合同正文、Memory 和工具返回均按不可信数据处理，DeepSeek Key、Authorization、完整 Prompt 和完整合同不进入 Trace。

## 已验证失败案例

1. DeepSeek Key、Base URL 或模型配置缺失时，创建外部模式任务会返回明确配置错误，不静默回退为本地成功。
2. Planner 非法动作、未知条款、越权状态或耗尽重试预算会被拒绝，不执行对应工具。
3. Evidence 第一次失败最多执行一次受限检索修复；再次失败进入人工复核或 `EVIDENCE_MISSING`。
4. Critic 冲突、无效结构化输出和 Prompt Injection 信号不会生成可确认的正式风险。
5. 取消、节点超时和任务超时保留已完成日志；可恢复状态必须由人工提供原因后恢复，重试预算不会因重启重置。

## 项目表述边界

可以表述为：已实现显式 Tool Registry、受控 Planner/Critic、确定性 Evidence 准入、混合检索、Memory 生命周期、可恢复执行和脱敏 Trace，并用真实 DeepSeek Key 在 2 份合成 NDA 子集上完成两次稳定性运行。

不得表述为：完整标注集已通过 DeepSeek 效果验收、达到生产级准确率、Memory 已证明提升效果、真实外部 Embedding 已验证，或所有指标均已达标。

## PDF 支持边界

当前只支持带有可复制文本层的普通中文和英文 PDF。解析结果按页输出文本块，并为每个块记录 `page_number` 和 `bbox`；条款通过 `source_location.pdf_blocks` 回溯到原 PDF 页码、块 ID 和坐标。证据验证完成后，正式风险的 `evidence_location.pdf_blocks` 会记录证据实际命中的页码、块 ID、`bbox` 和块内字符范围，不只依赖 `clause_id` 间接定位整条条款。

扫描件和无有效文本文件不会进入合同类型识别或正式风险分析。扫描件返回“需要 OCR”，空文本文件说明当前未启用 OCR；加密 PDF 不提供密码输入或解密流程，复杂字体无法可靠映射 Unicode、文件损坏或结构不完整时均返回对应解析失败原因。OCR 不在当前任务范围内，后续是否实施需新增独立迭代计划。

`samples/pdf/` 和 `backend/tests/fixtures/pdf/` 中的文件均为项目内合成测试材料，来源和用途见各目录 README，不包含真实客户或未经授权的合同文本。

## 运行依赖

1. `fastapi`：提供 ASGI 应用、路由、Pydantic 请求校验和静态文件集成。
2. `uvicorn`：运行 FastAPI ASGI 应用。
3. `python-multipart`：解析 `multipart/form-data` 合同上传请求。
4. `python-dotenv`：应用启动时读取项目根目录 `.env`，已存在的进程环境变量保持优先。
5. `httpx`：供 FastAPI `TestClient` 执行 API 契约测试，并执行 OpenAI-compatible HTTP 调用。
6. `pdfplumber`：提取 PDF 页、文本词块和边界坐标，底层使用 `pdfminer.six`；项目固定为 `0.11.10`，采用 MIT 许可证。该依赖会同时安装 `pdfminer.six`、Pillow 和 `pypdfium2`，不包含 OCR 模型，也不会将合同发送到网络服务。
7. Node.js、pnpm 和 `playwright@1.60.0`：只用于 Chromium 浏览器 E2E，不属于应用运行依赖。

安装：

```powershell
python -m pip install -r requirements.txt
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
```

## DeepSeek 配置

Key 通过 `REVIEW_AGENT_LLM_API_KEY` 提供，不写入代码、README 或受版本控制的文件。应用启动时使用 `python-dotenv` 自动读取项目根目录的 `.env`；`.env` 已被 `.gitignore` 排除，`.env.example` 只提供变量名和非敏感示例。加载使用 `override=False`，因此 CI、容器或 PowerShell 中已经设置的进程环境变量优先于 `.env`。

本地 `.env` 可以直接配置：

```dotenv
REVIEW_AGENT_LLM_MODE=openai_compatible
REVIEW_AGENT_LLM_BASE_URL=https://api.deepseek.com
REVIEW_AGENT_LLM_API_KEY=<your-deepseek-key>
REVIEW_AGENT_LLM_MODEL=deepseek-v4-pro
```

配置完成后直接启动：

```powershell
python -m uvicorn main:app --app-dir backend/app --host 127.0.0.1 --port 8000
```

需要临时覆盖 `.env` 时，可在 PowerShell 启动前设置当前进程环境：

```powershell
$env:REVIEW_AGENT_LLM_MODE = "openai_compatible"
$env:REVIEW_AGENT_LLM_BASE_URL = "https://api.deepseek.com"
$env:REVIEW_AGENT_LLM_API_KEY = "<your-deepseek-key>"
$env:REVIEW_AGENT_LLM_MODEL = "deepseek-v4-pro"
python -m uvicorn main:app --app-dir backend/app --host 127.0.0.1 --port 8000
```

未配置 Key 时服务仍可启动；界面打开 DeepSeek 开关创建任务时会返回配置错误。默认代码配置是 `local_structured`，而 `.env.example` 有意展示 DeepSeek 外部模式。成本单价未配置时只记录 token 和“未配置”，不推测金额。

## 运行服务

```powershell
python -m uvicorn main:app --app-dir backend/app --host 127.0.0.1 --port 8000
```

页面：

```text
http://127.0.0.1:8000/
```

健康检查：

```text
http://127.0.0.1:8000/health
```

## 验证当前版本

1. 使用上述唯一启动命令启动服务。
2. 访问 `http://127.0.0.1:8000/health`，确认返回 `status: ok`。
3. 访问 `http://127.0.0.1:8000/`，确认页面和 `/src/` 静态资源由同一服务返回。
4. 确认浏览器请求使用同源 `/api/*` 地址，没有独立前端服务。
5. 确认首页有合同上传入口和甲方 / 乙方审查立场选择。
6. 不选择合同文件或审查立场时，确认按钮不可用。
7. 上传 `.docx` 或 `.pdf` 并选择审查立场。
8. 确认右侧流式进度展示 `START`、`UPLOAD_RECEIVED`、`DOCUMENT_PARSED`、`CLAUSES_STRUCTURED`、`PLAYBOOK_RETRIEVED`、`CONTEXT_BUILT`、`RISK_ANALYZED`、`EVIDENCE_VERIFIED`；如果存在高风险或低置信度风险，则最终进入 `HUMAN_REVIEW_PENDING`。
9. 确认 Workbench 左侧显示条款编号、条款正文、条款类型和关键字段。
10. 确认右侧展示命中的 Playbook 规则；未命中时提示不会生成正式风险。
11. 确认右侧展示上下文 Trace，包含当前条款、相关条款、相关 Memory 和约束信息。
12. 确认右侧展示已验证风险列表、证据文本和修改建议；无证据风险不得展示为正式风险。
13. 点击风险列表中的定位按钮，确认左侧跳转到对应条款，并能看到证据高亮。
14. 在左侧条款原文中框选文本，点击局部审查，确认右侧展示局部审查结果，且正式风险列表不被改写。
15. 框选不属于该条款的文本调用局部审查接口时，确认返回友好错误。
16. 在风险详情中执行采纳、忽略、修改等级、修改建议，确认任务状态更新为 `MEMORY_UPDATED`，风险复核状态、报告选择和 Memory ID 可见。
17. 对风险点击局部重审，确认以该风险证据文本发起局部审查，正式风险列表不被改写。
18. 确认底部执行记录显示 `parse_document`、`extract_clauses`、`extract_key_fields`、`retrieve_playbook_rules`、`retrieve_related_clauses`、`retrieve_memory`、`analyze_risk`、`criticize_risk`、`generate_revision`、`verify_evidence`、`write_memory` 的工具名、状态、耗时、摘要和真实 token/cost 摘要；异常修复路径还应显示 `plan_review_action`。本地结构化工具应显示 `*_no_external_llm`，不能显示外部 LLM 计量占位。
19. 对需要进入报告的风险提交人工反馈并勾选加入报告，点击导出报告，确认浏览器下载 Markdown 文件，且后端任务状态变为 `REPORT_READY`。
20. 确认报告只包含已允许进入报告的风险，不包含未加入报告的忽略风险、待人工复核但未确认风险、完整执行日志或技术实现细节。
21. 点击基础评测中的运行评测，确认返回 10 份合成 NDA 样本的摘要；摘要包含样本名称、任务跑通、文档解析、条款结构化、Playbook 命中、风险证据、Memory 写入、报告导出和失败原因。
22. 访问 `http://127.0.0.1:8000/api/tools`，确认 14 个工具都有输入输出契约、`runtime_calls_llm`、`runtime_llm_mode`；Planner 输出只包含白名单动作、固定原因码、当前条款、受限查询调整和置信度，Critic 输出只包含三种决策和固定原因码。
23. 上传相似合同后，确认相关 Memory 能进入上下文 Trace；当 Memory 影响建议时，风险详情展示历史反馈引用。
24. 上传不支持或不可解析文件时，确认状态进入错误提示，不显示伪造解析结果。
25. 服务重启后使用原任务 ID 查询，确认任务、条款、风险、日志、事件和反馈仍然存在。
26. 对非终态任务执行重启验证时，确认事件中出现 `recovery_started`，恢复记录包含恢复次数和起点，已完成工具没有重复日志。
27. 在默认 `local_sparse` 模式下完成审查，确认相关条款结果包含 `local_sparse_hash_v1`、固定 256 维向量、余弦相似度、rerank 因子和最终分数，且执行日志标记 `local_sparse_no_external_embedding`。
28. 配置 `openai_compatible` Embedding 后，确认日志显示实际模型、供应商请求 ID、输入数量、向量维度和耗时；让供应商返回错误时，确认任务进入 `RETRIEVAL_FAILED` 且没有 `lexical_fallback`。
29. 在评测面板切换“流程摘要”和“效果摘要”，确认两者结果和加载/失败状态彼此独立。
30. 运行效果评测，确认返回 23 个指标，且每项包含样本数、通过数、失败数、阈值、达标状态和失败样本；样本数为 0 时必须显示“无样本”且不得视为达标。当前默认本地模式下相关条款 Recall@1 应显示 `20/20` 且达到 `0.8` 阈值，但完整摘要仍为 `completed_with_failures`。
31. 确认效果摘要显示 Git、Playbook、标注、LLM 和 Embedding 版本，并在 `evaluation/effect/` 生成以评测 ID 命名的 JSON 和 Markdown 文件。
32. 上传 `samples/pdf/nda_text_zh.pdf` 和 `samples/pdf/nda_text_en.pdf`，确认正文按页解析，条款 `source_location` 包含页码、块 ID 和 `bbox`；正式风险的 `evidence_location` 精确到证据命中的 PDF 块和块内字符范围。
33. 上传 `backend/tests/fixtures/pdf/scanned_image.pdf`，确认任务进入 `PARSE_FAILED`、提示“需要 OCR”，且不生成文档、条款和风险。
34. 使用加密、复杂字体映射失败或损坏 PDF 时，确认界面展示具体解析原因，不显示文档解析成功。
35. 展开 Agent 执行记录，确认任务 Trace、工具 Step、恢复信息和 Provider 摘要可读；本地模式与 DeepSeek 真实调用明确区分，且不出现密钥、完整合同 Prompt 或前端堆栈。
36. 确认配置错误、Planner 非法动作、一次检索修复、Injection 阻断、取消、超时和恢复均有浏览器验收状态，且没有把夹具状态写入生产任务 API。
37. 运行 Playwright，确认 14 个 Chromium 主流程、错误流、Agent 状态、三视口和可访问性用例均实际通过；每次运行使用独立的数据库、上传、报告和评测目录，浏览器和测试数据写入本机缓存或已忽略目录，不进入 Git。

默认上传请求大小上限为 10 MB，可通过 `REVIEW_AGENT_MAX_UPLOAD_BYTES` 调整。
默认 CORS 不允许通配来源；可通过逗号分隔的 `REVIEW_AGENT_ALLOWED_ORIGINS` 配置明确来源。
默认 SQLite 业务与 Memory 数据库路径为 `backend/app/data/review_agent_memory.sqlite3`，可通过 `REVIEW_AGENT_MEMORY_DB_PATH` 调整。
默认上传目录为 `backend/app/data/uploads`，可通过 `REVIEW_AGENT_UPLOAD_DIR` 调整。
默认报告目录为 `backend/app/reports`，可通过 `REVIEW_AGENT_REPORT_DIR` 调整；默认评测输出目录为 `evaluation`，可通过 `REVIEW_AGENT_EVALUATION_OUTPUT_DIR` 调整。
新建审查默认选择 `local_structured`；界面打开 DeepSeek 开关后，该任务固定使用 `openai_compatible`。外部 Provider 的连接与凭据通过 `REVIEW_AGENT_LLM_BASE_URL`、`REVIEW_AGENT_LLM_API_KEY`、`REVIEW_AGENT_LLM_MODEL` 和 `REVIEW_AGENT_LLM_TIMEOUT_SECONDS` 配置，任务选择会随完整任务状态写入 SQLite。
默认 LLM 上下文预算为 6000 tokens，可通过 `REVIEW_AGENT_LLM_CONTEXT_BUDGET_TOKENS` 调整；值必须是正整数。
可通过 `REVIEW_AGENT_LLM_PROMPT_COST_PER_1M` 和 `REVIEW_AGENT_LLM_COMPLETION_COST_PER_1M` 配置每百万 token 单价；未配置时日志显示“未配置”。
默认 Embedding 模式为 `local_sparse`；外部模式通过 `REVIEW_AGENT_EMBEDDING_MODE=openai_compatible`、`REVIEW_AGENT_EMBEDDING_BASE_URL`、`REVIEW_AGENT_EMBEDDING_API_KEY`、`REVIEW_AGENT_EMBEDDING_MODEL` 和 `REVIEW_AGENT_EMBEDDING_TIMEOUT_SECONDS` 配置。
Orchestrator 当前固定单节点超时为 90 秒、全任务超时为 300 秒；这两个值不是环境变量。解析、检索、LLM 输出、Evidence、节点超时、任务超时和通用任务错误的人工恢复预算各为 2 次，服务重启不会重置。

## 运行测试

```powershell
$env:REVIEW_AGENT_LOAD_DOTENV = "0"
python -m unittest discover -s backend\tests -p "test_*.py"
pnpm test:e2e
Remove-Item Env:REVIEW_AGENT_LOAD_DOTENV
```

后端测试和 Playwright 测试服务显式设置 `REVIEW_AGENT_LOAD_DOTENV=0`，避免本机 `.env` 中的真实 Provider、数据库和输出目录污染隔离测试；应用正常启动时不设置该变量，会自动加载 `.env`。
