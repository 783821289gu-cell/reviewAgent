"""LangGraph 图结构定义。

建议先读本文件，再读 ``langgraph_review_agent.py`` 中同名的节点方法：

1. ``build_review_graph`` 定义一次合同审查的业务主流程。
2. ``_build_risk_graph`` 定义单个风险上下文的分析子图。
3. ``build_review_control_graph`` 只负责 Checkpoint 和人工中断/恢复。

这里刻意把“图的拓扑”与“节点的业务实现”分开。拓扑回答下一步去哪里，
节点方法回答这一步具体做什么。修改流程顺序或路由时应优先检查本文件；修改
解析、检索或风险分析行为时，应修改节点实现而不是在图构建代码中塞业务逻辑。
"""

from operator import add
from typing import Annotated, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send


class ReviewGraphState(TypedDict, total=False):
    """主图和风险子图之间传递的短生命周期执行状态。

    这不是数据库中的完整任务对象。合同、条款、日志和风险等业务真相由
    ``AgentState``/``ReviewEventStore`` 持久化；本状态只保存图路由和并行归并
    所需的最小数据，因此主业务图无需 Checkpointer。

    ``total=False`` 允许不同节点只返回自己修改的字段。LangGraph 会把节点返回
    的字典合并回当前状态，而不要求每个节点重复返回所有字段。
    """

    # 一次图执行的稳定关联键，也是控制图使用的 thread_id。
    task_id: str
    # 前置节点发生业务终止时置 True，后续 guarded edge 会直接走 END。
    terminal: bool
    # build_context 产生的全部待分析项，供 Send 动态扇出。
    risk_items: list[dict]
    # Send 给单个风险分支的输入；每个分支只处理一个 risk_item。
    risk_item: dict
    # ``add`` 是 reducer：并行分支各返回一个列表，LangGraph 将列表相加归并。
    # 若没有 reducer，多个并行分支同时写同一字段会产生并发更新冲突。
    risk_results: Annotated[list[dict], add]
    # 风险子图节点计算出的下一跳，由 _branch_route 读取。
    branch_route: str
    # 单个风险分支的工作区，保存 finding、证据、重试次数和分支日志。
    branch_payload: dict


class ReviewControlState(TypedDict, total=False):
    """可被 Checkpointer 保存的紧凑控制状态。

    控制图只需要知道任务处于执行、人工复核还是完成阶段。完整合同正文和风险
    结果不进入 Checkpoint，避免 PostgreSQL 业务表与 LangGraph Checkpoint 各保存
    一份可相互漂移的业务数据。
    """

    task_id: str
    phase: str
    status: str
    pending_risk_ids: list[str]
    recovery_count: int


CONTROL_STATE_KEYS = frozenset(ReviewControlState.__annotations__)


class ReviewWorkflowHandlers(Protocol):
    def bootstrap(self, state: ReviewGraphState, runtime) -> dict: ...

    def parse_document(self, state: ReviewGraphState, runtime) -> dict: ...

    def classify_contract(self, state: ReviewGraphState, runtime) -> dict: ...

    def structure_clauses(self, state: ReviewGraphState, runtime) -> dict: ...

    def retrieve_playbook(self, state: ReviewGraphState, runtime) -> dict: ...

    def build_context(self, state: ReviewGraphState, runtime) -> dict: ...

    def prepare_risk(self, state: ReviewGraphState, runtime) -> dict: ...

    def analyze_risk(self, state: ReviewGraphState, runtime) -> dict: ...

    def criticize_risk(self, state: ReviewGraphState, runtime) -> dict: ...

    def verify_evidence(self, state: ReviewGraphState, runtime) -> dict: ...

    def repair_retrieval(self, state: ReviewGraphState, runtime) -> dict: ...

    def finalize_risk(self, state: ReviewGraphState, runtime) -> dict: ...

    def aggregate_risks(self, state: ReviewGraphState, runtime) -> dict: ...


class ReviewControlHandlers(Protocol):
    def checkpoint_phase(self, state: ReviewControlState, runtime) -> dict: ...

    def await_human_review(self, state: ReviewControlState, runtime) -> dict: ...


MAIN_NODE_ORDER = (
    "bootstrap",
    "parse_document",
    "classify_contract",
    "structure_clauses",
    "retrieve_playbook",
    "build_context",
    "risk_subgraph",
    "aggregate_risks",
)

RISK_NODE_ORDER = (
    "prepare_risk",
    "analyze_risk",
    "criticize_risk",
    "verify_evidence",
    "repair_retrieval",
    "finalize_risk",
)


def build_review_graph(handlers: ReviewWorkflowHandlers, context_schema: type):
    """编译业务主图，并把单风险子图作为一个主图节点接入。

    ``handlers`` 通常就是 ``ReviewOrchestratorAgent``；因此图中注册的是该对象的
    绑定方法。``context_schema`` 是 ``_ReviewRuntime``，通过 ``runtime.context``
    向节点提供数据库、合同对象和执行控制等依赖，它与可归并的 Graph State 是
    两个概念。
    """

    risk_graph = _build_risk_graph(handlers, context_schema)

    def run_risk_subgraph(state: ReviewGraphState, runtime) -> dict:
        # 每次 Send 都会以一个 risk_item 调用这里。子图完成后只把可归并的
        # risk_results 交还主图，不把 branch_payload 泄漏到其他并行分支。
        result = risk_graph.invoke(state, context=runtime.context)
        return {"risk_results": list(result.get("risk_results") or [])}

    graph = StateGraph(ReviewGraphState, context_schema=context_schema)
    # Node 名称会出现在 LangGraph 调试信息中；处理函数是真正的业务实现。
    graph.add_node("bootstrap", handlers.bootstrap)
    graph.add_node("parse_document", handlers.parse_document)
    graph.add_node("classify_contract", handlers.classify_contract)
    graph.add_node("structure_clauses", handlers.structure_clauses)
    graph.add_node("retrieve_playbook", handlers.retrieve_playbook)
    graph.add_node("build_context", handlers.build_context)
    graph.add_node("risk_subgraph", run_risk_subgraph)
    graph.add_node("aggregate_risks", handlers.aggregate_risks)

    graph.add_edge(START, "bootstrap")
    # 线性阶段也使用条件边，因为任一节点都可能返回 terminal=True。这样错误
    # 状态一旦已真实落库，图会立即结束，不会继续执行依赖无效输入的后续节点。
    _add_guarded_edge(graph, "bootstrap", "parse_document")
    _add_guarded_edge(graph, "parse_document", "classify_contract")
    _add_guarded_edge(graph, "classify_contract", "structure_clauses")
    _add_guarded_edge(graph, "structure_clauses", "retrieve_playbook")
    _add_guarded_edge(graph, "retrieve_playbook", "build_context")
    graph.add_conditional_edges(
        "build_context",
        _dispatch_risk_items,
        ["risk_subgraph", "aggregate_risks", END],
    )
    # 所有 Send 分支完成并通过 reducer 合并后，LangGraph 才进入统一聚合节点。
    graph.add_edge("risk_subgraph", "aggregate_risks")
    graph.add_edge("aggregate_risks", END)
    # 主业务图不传 checkpointer。恢复业务进度依靠各节点写入的 AgentState；
    # 只有下方控制图需要保存 interrupt 游标。
    return graph.compile()


def build_review_control_graph(
    handlers: ReviewControlHandlers,
    context_schema: type,
    checkpointer,
):
    """编译负责人工复核暂停/恢复的控制图。

    首次执行时，``checkpoint_phase`` 根据 phase 决定是否进入人工复核节点。
    ``await_human_review`` 中的 ``interrupt()`` 会让图暂停并由 checkpointer 保存
    游标。收到人工反馈后，调用方用 ``Command(resume=...)`` 从同一 thread_id
    继续；如果仍有未处理风险，则再次 interrupt，否则走 END。
    """

    graph = StateGraph(ReviewControlState, context_schema=context_schema)
    graph.add_node("checkpoint_phase", handlers.checkpoint_phase)
    graph.add_node("await_human_review", handlers.await_human_review)
    graph.add_edge(START, "checkpoint_phase")
    graph.add_conditional_edges(
        "checkpoint_phase",
        lambda state: (
            "await_human_review"
            if state.get("phase") == "human_review"
            else END
        ),
        ["await_human_review", END],
    )
    graph.add_conditional_edges(
        "await_human_review",
        lambda state: (
            "await_human_review"
            if state.get("phase") == "human_review"
            else END
        ),
        ["await_human_review", END],
    )
    # interrupt 只有在编译图时配置 Checkpointer 才能跨调用恢复。
    return graph.compile(checkpointer=checkpointer)


def _build_risk_graph(handlers: ReviewWorkflowHandlers, context_schema: type):
    """构建处理一个风险上下文的可循环子图。

    正常路径为：
    prepare -> analyze -> critic -> verify -> finalize。

    检索不足或证据无效时，条件边可进入 repair，再回到 analyze。是否允许修复
    由节点内的 Planner 白名单决策和重试计数约束，不是让 LLM 任意选择节点。
    """

    graph = StateGraph(ReviewGraphState, context_schema=context_schema)
    graph.add_node("prepare_risk", handlers.prepare_risk)
    graph.add_node("analyze_risk", handlers.analyze_risk)
    graph.add_node("criticize_risk", handlers.criticize_risk)
    graph.add_node("verify_evidence", handlers.verify_evidence)
    graph.add_node("repair_retrieval", handlers.repair_retrieval)
    graph.add_node("finalize_risk", handlers.finalize_risk)

    graph.add_edge(START, "prepare_risk")
    routes = {
        "prepare_risk": [
            "analyze_risk",
            "criticize_risk",
            "repair_retrieval",
            "finalize_risk",
        ],
        "analyze_risk": ["criticize_risk", "finalize_risk"],
        "criticize_risk": ["verify_evidence", "finalize_risk"],
        "verify_evidence": ["repair_retrieval", "finalize_risk"],
        "repair_retrieval": ["analyze_risk", "finalize_risk"],
    }
    # 每个节点把下一跳写入 branch_route；路由函数只读取这个受控值。
    for node_name, destinations in routes.items():
        graph.add_conditional_edges(
            node_name,
            _branch_route,
            destinations,
        )
    graph.add_edge("finalize_risk", END)
    return graph.compile()


def _add_guarded_edge(graph: StateGraph, source: str, target: str) -> None:
    """增加一条会尊重 terminal 标记的主图条件边。"""

    graph.add_conditional_edges(
        source,
        lambda state: END if state.get("terminal") else target,
        [target, END],
    )


def _dispatch_risk_items(state: ReviewGraphState):
    """把风险项动态扇出为多个 ``risk_subgraph`` 调用。

    ``Send`` 适合运行时才知道数量的 map 场景。并行度不在这里硬编码，而是在
    调用 ``graph.stream`` 时通过 ``max_concurrency`` 配置，因此 DeepSeek 可限制
    为 2，本地确定性模式可限制为 1。
    """

    if state.get("terminal"):
        return END
    risk_items = list(state.get("risk_items") or [])
    if not risk_items:
        return "aggregate_risks"
    return [
        Send(
            "risk_subgraph",
            {
                "task_id": state["task_id"],
                "risk_item": risk_item,
                "risk_results": [],
            },
        )
        for risk_item in risk_items
    ]


def _branch_route(state: ReviewGraphState) -> str:
    """返回单风险子图的受控下一跳；缺失时保守进入 finalize。"""

    return str(state.get("branch_route") or "finalize_risk")
