# Agent 模块化与安全并行优化计划

## 1. 背景

框架迁移完成后，默认链路已经使用 FastAPI、RQ、LangGraph、PostgreSQL、pgvector 和 LangGraph Store，但 `ReviewOrchestratorAgent` 仍同时承担图节点控制、批处理执行、Context 工具编排、进度更新和持久化协调。关键字段与风险子图已有有界并发，Playbook 检索和 Context 构建仍按工作项串行执行。

这会产生两个实际问题：

1. 不同层级的职责集中在一个文件中，修改批处理策略容易影响状态推进和恢复逻辑。
2. DeepSeek 任务中，彼此独立的 Playbook 和 Context 工作项没有复用已有并发预算，长合同会累积不必要的串行等待。

## 2. 本轮范围

本轮只优化 Agent 内部职责边界和已确认独立工作项的执行方式：

- 抽离无领域依赖的有序批处理执行器；
- 抽离 Context 工作项构造、相关条款检索、Memory 检索和 Context 组装；
- 让关键字段、Playbook、Context 和风险分支统一使用任务级有界并发；
- 保留 Agent 对状态转换、进度、持久化、恢复和错误分类的唯一控制权；
- 保持前端、REST/SSE、LLM Provider、检索算法参数和业务输出契约不变。

本轮不把所有 LangGraph 节点拆成独立服务，也不追求文件行数形式上的平均分配。节点之间仍存在明确状态依赖，机械搬迁会扩大恢复和持久化回归面。

## 3. 设计边界

### 3.1 `OrderedBatchExecutor`

- 只接收工作项和函数，不依赖合同、数据库或 AgentState。
- 使用固定最大并发执行独立工作项。
- 复制 `ContextVar`，保证线程中的任务级 LLM 模式和 Trace 上下文不丢失。
- 无论实际完成顺序如何，结果始终按输入顺序返回。
- 任一未捕获异常出现时取消尚未开始的 Future，不伪造部分成功。

### 3.2 `ReviewContextPipeline`

- 只负责一个 `clause + rule` 工作项的相关条款、Memory 和 Context 构建。
- 不更新任务状态，不发布进度，不写业务检查点。
- 每个并发工作项使用独立的进程内临时 Embedding cache，避免普通字典并发写；生产 PostgreSQL/BGE 路径继续使用模型锁和 Redis/数据库缓存。
- 每个工作项独立保留工具日志和错误，由 Agent 主线程统一合并。
- 每个并发分支根 Step 指向进入批次前的同一个父 Step，分支内部工具继续形成自己的父子链，不把并发兄弟伪装成串行调用。

### 3.3 `ReviewOrchestratorAgent`

- 继续拥有 LangGraph 节点、条件边、状态转换、进度事件、持久化和恢复。
- 只在主线程按源顺序采纳批处理结果并写入 PostgreSQL。
- 同一批次先合并所有已产生的工具日志，再抛出源顺序中的首个错误，避免并发兄弟调用的真实记录丢失。

## 4. 并发规则

- `local_structured` 宽度固定为 1。
- DeepSeek 默认宽度为 2，配置上限为 4；本机默认仍为 2。
- 并发范围：关键字段提取、Playbook 检索、Context 工作项和风险子图中的独立分支。
- 串行范围：文档解析、分类、条款切分、同一风险内的 Analyzer -> Critic -> Evidence、状态写入、进度写入和人工恢复。
- BGE Embedding 与 reranker 的本地推理锁保持不变。本轮并行主要重叠独立数据库、Redis、工具和外部 LLM 等待，不能在没有真实基准前宣称固定倍数加速。

## 5. 验收标准

1. 并发执行期间实际活动数不超过配置值。
2. 延迟不同的工作项仍按输入顺序聚合。
3. 子线程能读取与父任务一致的 `ContextVar`。
4. Context Pipeline 不直接调用任务持久化或状态更新。
5. 并发工作项失败时，所有已经产生的 Step Log 均被保留。
6. 关键字段、Playbook、Context、风险和长任务恢复回归通过。
7. 后端全量测试、Python 编译、依赖检查、Alembic 差异检查和 Git 空白检查通过后才能标记完成。

## 6. 当前状态

- 已完成通用有序批处理执行器和 Context Pipeline 抽离。
- 已完成 Playbook 与 Context 工作项的 DeepSeek 有界并发接入。
- 已完成并发上限、顺序、`ContextVar`、失败日志、安全连续前缀和并发 Trace 父子链测试。
- 受影响的最终 30 项回归已通过。
- 第一轮全量测试真实发现 Context 分支根 Step 缺少父 Step；修复后定向回归通过。
- 最终 35 个后端模块、314 项测试全部通过且无模块超时；`pip check`、Python 编译、Alembic 差异检查和 `git diff --check` 均通过。
- 本轮没有执行真实 DeepSeek 前后耗时对比，因此只确认模块边界和并发行为正确，不声明生产延迟提升比例。

本计划已完成。后续如果继续拆分文档节点或风险节点，应建立新的独立 Plan，并先证明拆分不会复制状态或破坏恢复语义。
