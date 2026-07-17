# ContractReviewAgent

当前实现范围：`TASKS.md` 的任务 9 和 `TASKS_2.md` 的任务 10。系统支持 AgentState、显式 Tool Registry、流式状态事件、执行日志、本地 NDA Playbook 规则检索、可配置 Embedding 的当前合同内相关条款检索、SQLite Memory 召回、风险分析上下文构建、结构化风险分析、证据验证、修改建议生成、Web 风险展示、原文高亮、风险跳转、局部审查、人工反馈动作、Memory 写入、Markdown 报告导出、流程评测、人工标注效果评测和文本型 PDF 页码/文本块定位；HTTP 传输层使用 FastAPI / Uvicorn，并由同一服务托管前端。任务、上传文件、文档、条款、风险、日志和事件已接入 SQLite 持久化，并由 Playwright 覆盖浏览器主流程和错误流。

## 当前已实现

1. FastAPI / Uvicorn 后端服务启动入口。
2. 后端健康检查接口。
3. 审查状态模型：`START`、`UPLOAD_RECEIVED`、`DOCUMENT_PARSED`、`CONTRACT_TYPE_CLASSIFIED`、`CLAUSES_STRUCTURED`、`PLAYBOOK_RETRIEVED`、`CONTEXT_BUILT`、`RISK_ANALYZED`、`EVIDENCE_VERIFIED`、`HUMAN_REVIEW_PENDING`、`MEMORY_UPDATED`、`REPORT_READY`、`UNSUPPORTED_CONTRACT_TYPE`、`PARSE_FAILED`、`RETRIEVAL_FAILED`、`LLM_OUTPUT_INVALID`、`EVIDENCE_MISSING`、`NEED_MANUAL_REVIEW`、`TASK_ERROR`。
4. FastAPI 同源托管前端页面和静态资源。
5. 首页项目说明、合同上传入口、甲方 / 乙方审查立场选择。
6. 未选择合同文件或审查立场时不能上传解析。
7. Workbench 布局：顶部、左侧原文区、右侧结果区、底部执行记录区。
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
18. `retrieve_related_clauses` 已通过显式 `tool_registry` 调用；支持 `local_sparse` 离线测试模式和 `openai_compatible` 真实 Embedding 模式，在当前合同条款内执行余弦召回和规则 rerank。
19. `retrieve_memory` 已通过显式 `tool_registry` 调用；支持从调用方提供的 `memory_items` 中过滤召回，也支持从 SQLite Memory 中按合同类型、条款类型、风险类型和审查立场检索历史反馈。
20. 后端构建风险分析上下文，包含当前条款、命中规则、相关条款、相关 Memory 字段、输出约束和证据约束。
21. `analyze_risk` 已通过显式 `tool_registry` 调用；`REVIEW_AGENT_LLM_MODE` 支持 `local_structured` 和 `openai_compatible`，本地模式不会记录为真实外部 LLM 调用。
22. `verify_evidence` 已通过显式 `tool_registry` 调用，校验 `clause_id`、`evidence_text`、风险原因与证据文本相关性，以及命中规则一致性。
23. `generate_revision` 已通过显式 `tool_registry` 调用，并与风险分析共用结构化 LLM Provider 边界。
24. 前端展示上下文 Trace、已验证风险列表、规则详情、证据文本、风险原因和修改建议。
25. 点击风险可定位到左侧对应条款，并高亮正式风险证据文本。
26. 用户可以在合同原文中框选文本并发起局部审查，局部审查结果与正式风险列表分开展示。
27. 局部审查只返回当前框选文本的复核结果，不写入正式风险列表，不写入 Memory。
28. 用户可以对正式风险执行采纳、忽略、修改等级、修改建议，并选择是否加入报告。
29. 所有采纳、忽略和修改动作通过 `tool_registry["write_memory"]` 写入 SQLite Memory。
30. 后续相似审查会通过 `retrieve_memory` 召回相关 Memory；当 Memory 影响修改建议时，风险详情展示历史反馈引用。
31. 用户可以基于正式风险证据触发局部重审，局部结果仍不写入正式风险列表。
32. 用户可以导出 Markdown 审查报告，报告生成通过 `tool_registry["generate_report"]` 调用。
33. 报告包含合同名称、审查立场、Playbook 版本、风险等级、风险原因、原文证据、修改建议和人工反馈状态。
34. 报告只导出用户明确允许进入报告且不处于待人工复核状态的风险，不包含完整执行日志和技术实现细节。
35. `samples/` 提供 10 份项目内合成 NDA 样本，样本来源已在 `samples/README.md` 说明。
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
52. `samples/annotations/related_clauses.json` 提供人工相关条款标注；离线测试使用通用法律同义词确定性 Stub 计算 Recall@1，并与旧词频稀疏基线比较。
53. `samples/annotations/` 提供 `effect-v1` 人工标注 schema，覆盖合同类型、条款边界和类型、预期规则、风险、可接受等级、证据 span、相关条款、人工复核和报告选择；当前数据全部为项目内合成夹具。
54. `POST /api/evaluation/effect/run` 独立运行效果评测，不改写现有 `POST /api/evaluation/run` 流程评测响应。
55. 效果评测分别计算 NDA 分类准确率、非 NDA 拒绝率、条款切分和类型准确率、Playbook/相关条款 Recall@K、风险 Precision/Recall/F1、证据 span、人工复核、报告过滤、工具调用和端到端指标。
56. 每个效果指标记录样本数、通过数、失败数、阈值和失败样本；版本化 JSON/Markdown 摘要记录 Git、Playbook、标注、LLM、Embedding 和关键参数。
57. Web 评测面板使用“流程摘要”和“效果摘要”两个独立视图；未达阈值指标按实际失败状态展示。
58. 扫描件、空文本、加密、复杂字体映射失败和损坏 PDF 会进入真实 `PARSE_FAILED` 状态，不生成伪造正文或正式风险。
59. 每个任务持久化 `trace_id`，每次工具调用持久化唯一 `step_id`；旧 SQLite 日志在初始化时生成确定性 Step 标识。
60. 任务恢复次数和恢复起点进入任务快照、API 响应和 Web 执行记录，服务重启后保留。
61. LLM/Embedding 执行摘要展示模型、供应商请求 ID、token 或输入量、耗时、成本状态和错误类型；日志摘要屏蔽密钥、Authorization、完整 prompt 和完整任务正文。
62. SSE 在终态关闭但浏览器未消费最后一帧时，前端使用任务查询接口做一次真实状态对账，不自行构造成功状态。
63. Playwright Chromium E2E 覆盖首页、上传、SSE、风险定位和高亮、框选局部审查、人工反馈、Memory、报告过滤下载、流程评测、非 NDA、超限、解析失败、扫描 PDF 和服务错误提示。

## 当前未实现

后续待做包括：扩大人工标注样本规模，以及使用实际外部 LLM/Embedding 在标注集上复测。OCR 尚未实现，是否在下一轮引入必须单独评估和决策，不默认引入本地大型 OCR 模型。

迭代技术方案、实施顺序和验收口径见 `NEXT_PLAN.md` 和 `TASKS_2.md`；这些文件保留计划形成过程，当前完成状态以代码、测试和本 README 为准。

当前基础评测只验证流程跑通，不声明生产级准确率。人工相关条款集目前只验证确定性测试 Stub 相对词频基线的受控提升，不代表实际外部 Embedding 模型质量；因此默认正式配置仍为 `local_sparse`。真实外部 LLM 和 Embedding 是否可用及是否有实际效果提升，仍取决于调用方提供的有效服务配置、凭据和后续实测结果。

当前 `effect-v1` 只有 6 份合同类型样本、6 条条款、3 条风险和 3 组相关条款标注。默认本地模式的已运行结果中，相关条款 Recall@1 为 `1/3`，低于清单阈值 `0.8`，效果摘要按 `completed_with_failures` 展示；其他指标的当前结果不能外推为生产准确率。

当前 12 个工具均已在 `tool_registry` 中注册契约。`classify_contract_type`、`extract_key_fields`、`analyze_risk` 和 `generate_revision` 具备 LLM Provider 调用边界；默认 `local_structured` 模式不记录为真实外部 LLM 调用。

## PDF 支持边界

当前只支持带有可复制文本层的普通中文和英文 PDF。解析结果按页输出文本块，并为每个块记录 `page_number` 和 `bbox`；条款通过 `source_location.pdf_blocks` 回溯到原 PDF 页码、块 ID 和坐标。证据验证完成后，正式风险的 `evidence_location.pdf_blocks` 会记录证据实际命中的页码、块 ID、`bbox` 和块内字符范围，不只依赖 `clause_id` 间接定位整条条款。

扫描件和无有效文本文件不会进入合同类型识别或正式风险分析。扫描件返回“需要 OCR”，空文本文件说明当前未启用 OCR；加密 PDF 不提供密码输入或解密流程，复杂字体无法可靠映射 Unicode、文件损坏或结构不完整时均返回对应解析失败原因。OCR 不在当前任务范围内，后续是否实施需新增独立迭代计划。

`samples/pdf/` 和 `backend/tests/fixtures/pdf/` 中的文件均为项目内合成测试材料，来源和用途见各目录 README，不包含真实客户或未经授权的合同文本。

## 运行依赖

1. `fastapi`：提供 ASGI 应用、路由、Pydantic 请求校验和静态文件集成。
2. `uvicorn`：运行 FastAPI ASGI 应用。
3. `python-multipart`：解析 `multipart/form-data` 合同上传请求。
4. `httpx`：供 FastAPI `TestClient` 执行 API 契约测试，并执行 OpenAI-compatible HTTP 调用。
5. `pdfplumber`：提取 PDF 页、文本词块和边界坐标，底层使用 `pdfminer.six`；项目固定为 `0.11.10`，采用 MIT 许可证。该依赖会同时安装 `pdfminer.six`、Pillow 和 `pypdfium2`，不包含 OCR 模型，也不会将合同发送到网络服务。
6. Node.js、pnpm 和 `playwright@1.60.0`：只用于 Chromium 浏览器 E2E，不属于应用运行依赖。

安装：

```powershell
python -m pip install -r requirements.txt
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
```

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
18. 确认底部执行记录显示 `parse_document`、`extract_clauses`、`extract_key_fields`、`retrieve_playbook_rules`、`retrieve_related_clauses`、`retrieve_memory`、`analyze_risk`、`generate_revision`、`verify_evidence`、`write_memory` 的工具名、状态、耗时、摘要和真实 token/cost 摘要；本地结构化工具应显示 `*_no_external_llm`，不能显示外部 LLM 计量占位。
19. 对需要进入报告的风险提交人工反馈并勾选加入报告，点击导出报告，确认浏览器下载 Markdown 文件，且后端任务状态变为 `REPORT_READY`。
20. 确认报告只包含已允许进入报告的风险，不包含未加入报告的忽略风险、待人工复核但未确认风险、完整执行日志或技术实现细节。
21. 点击基础评测中的运行评测，确认返回 10 份合成 NDA 样本的摘要；摘要包含样本名称、任务跑通、文档解析、条款结构化、Playbook 命中、风险证据、Memory 写入、报告导出和失败原因。
22. 访问 `http://127.0.0.1:8000/api/tools`，确认 12 个工具都有输入输出契约、`runtime_calls_llm`、`runtime_llm_mode`，且 `write_memory` 输入包含 `human_feedback`、`generate_report` 输入包含 `task_id`。
23. 上传相似合同后，确认相关 Memory 能进入上下文 Trace；当 Memory 影响建议时，风险详情展示历史反馈引用。
24. 上传不支持或不可解析文件时，确认状态进入错误提示，不显示伪造解析结果。
25. 服务重启后使用原任务 ID 查询，确认任务、条款、风险、日志、事件和反馈仍然存在。
26. 对非终态任务执行重启验证时，确认事件中出现 `recovery_started`，恢复记录包含恢复次数和起点，已完成工具没有重复日志。
27. 在默认 `local_sparse` 模式下完成审查，确认相关条款结果包含 `local_sparse_hash_v1`、固定 256 维向量、余弦相似度、rerank 因子和最终分数，且执行日志标记 `local_sparse_no_external_embedding`。
28. 配置 `openai_compatible` Embedding 后，确认日志显示实际模型、供应商请求 ID、输入数量、向量维度和耗时；让供应商返回错误时，确认任务进入 `RETRIEVAL_FAILED` 且没有 `lexical_fallback`。
29. 在评测面板切换“流程摘要”和“效果摘要”，确认两者结果和加载/失败状态彼此独立。
30. 运行效果评测，确认返回 14 个指标，且每项包含样本数、通过数、失败数、阈值、达标状态和失败样本；当前默认本地模式下相关条款 Recall@1 应显示 `1/3` 且未达阈值。
31. 确认效果摘要显示 Git、Playbook、标注、LLM 和 Embedding 版本，并在 `evaluation/effect/` 生成以评测 ID 命名的 JSON 和 Markdown 文件。
32. 上传 `samples/pdf/nda_text_zh.pdf` 和 `samples/pdf/nda_text_en.pdf`，确认正文按页解析，条款 `source_location` 包含页码、块 ID 和 `bbox`；正式风险的 `evidence_location` 精确到证据命中的 PDF 块和块内字符范围。
33. 上传 `backend/tests/fixtures/pdf/scanned_image.pdf`，确认任务进入 `PARSE_FAILED`、提示“需要 OCR”，且不生成文档、条款和风险。
34. 使用加密、复杂字体映射失败或损坏 PDF 时，确认界面展示具体解析原因，不显示文档解析成功。
35. 展开 Agent 执行记录，确认任务 Trace、工具 Step、恢复信息和 Provider 摘要可读，且不出现密钥、完整合同 prompt 或前端堆栈。
36. 运行 Playwright，确认 8 个 Chromium 主流程和错误流用例均实际通过；每次运行使用独立的数据库、上传、报告和评测目录，浏览器和测试数据写入本机缓存或已忽略目录，不进入 Git。

默认上传请求大小上限为 10 MB，可通过 `REVIEW_AGENT_MAX_UPLOAD_BYTES` 调整。
默认 CORS 不允许通配来源；可通过逗号分隔的 `REVIEW_AGENT_ALLOWED_ORIGINS` 配置明确来源。
默认 SQLite 业务与 Memory 数据库路径为 `backend/app/data/review_agent_memory.sqlite3`，可通过 `REVIEW_AGENT_MEMORY_DB_PATH` 调整。
默认上传目录为 `backend/app/data/uploads`，可通过 `REVIEW_AGENT_UPLOAD_DIR` 调整。
默认报告目录为 `backend/app/reports`，可通过 `REVIEW_AGENT_REPORT_DIR` 调整；默认评测输出目录为 `evaluation`，可通过 `REVIEW_AGENT_EVALUATION_OUTPUT_DIR` 调整。
默认 LLM 模式为 `local_structured`；外部模式通过 `REVIEW_AGENT_LLM_BASE_URL`、`REVIEW_AGENT_LLM_API_KEY`、`REVIEW_AGENT_LLM_MODEL` 和 `REVIEW_AGENT_LLM_TIMEOUT_SECONDS` 配置。
可通过 `REVIEW_AGENT_LLM_PROMPT_COST_PER_1M` 和 `REVIEW_AGENT_LLM_COMPLETION_COST_PER_1M` 配置每百万 token 单价；未配置时日志显示“未配置”。
默认 Embedding 模式为 `local_sparse`；外部模式通过 `REVIEW_AGENT_EMBEDDING_MODE=openai_compatible`、`REVIEW_AGENT_EMBEDDING_BASE_URL`、`REVIEW_AGENT_EMBEDDING_API_KEY`、`REVIEW_AGENT_EMBEDDING_MODEL` 和 `REVIEW_AGENT_EMBEDDING_TIMEOUT_SECONDS` 配置。

## 运行测试

```powershell
python -m unittest discover -s backend\tests -p "test_*.py"
pnpm test:e2e
```
