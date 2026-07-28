"""先从这个文件学习 LangGraph，不需要先懂项目的其他代码。

把 LangGraph 想成一张合同审查流程图：

* State 是流程中传递的“工作单”，记录目前有哪些数据。
* Node 是一个处理步骤，例如“解析合同”。
* Edge 是箭头，规定一个步骤完成后去哪里。
* ``compile()`` 把画好的流程图变成可以执行的程序。

这个项目有三张图：

1. 主图：从上传合同一直走到汇总风险。
2. 风险子图：只处理一条风险，必要时可以重新检索。
3. 控制图：审查需要人工处理时暂停，处理后继续。

阅读顺序：先看 ``ReviewGraphState``，再看 ``build_review_graph``，最后看
``_dispatch_risk_items``。看懂这三处，就掌握了本项目最重要的 LangGraph 用法。
"""

from operator import add
from typing import Annotated, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send


class ReviewGraphState(TypedDict, total=False):
    """这张“工作单”跟着主图和风险子图一起流转。

    例如，合同命中 3 条规则后，工作单可能是：

    ``{"task_id": "task_123", "risk_items": [风险1, 风险2, 风险3]}``

    每个 Node 都会收到这张工作单。Node 只返回自己新增或修改的字段。
    ``total=False`` 的意思也是“不是每次都必须填写所有字段”。

    注意：这里不保存完整合同。完整任务保存在数据库的 ``AgentState`` 中。
    """

    # 当前审查任务的编号。它把图执行、数据库记录和日志关联起来。
    # 示例："task_123"。
    task_id: str
    # 是否应该立刻停止。解析失败或合同类型不支持时会变成 True。
    terminal: bool
    # 所有待分析风险。命中 3 条规则时，这里通常有 3 项。
    risk_items: list[dict]
    # 当前风险分支正在处理的那一项。每个并行分支看到的值不同。
    risk_item: dict
    # 每个分支完成后都返回一项结果。
    # ``add`` 告诉 LangGraph：不要互相覆盖，要把多个列表拼到一起。
    risk_results: Annotated[list[dict], add]
    # 当前风险下一步去哪。示例："criticize_risk" 或 "finalize_risk"。
    branch_route: str
    # 当前风险的临时处理记录，包括模型结论、证据和重试次数。
    branch_payload: dict


class ReviewControlState(TypedDict, total=False):
    """控制图保存的“书签”。

    人工复核可能持续几分钟甚至几天。程序只要记住任务编号、当前阶段和哪些
    风险还没处理，就能从暂停位置继续。合同正文仍然从业务数据库读取。
    """

    # 哪个任务。
    task_id: str
    # 当前阶段："execute"、"human_review" 或 "complete"。
    phase: str
    # 前端看到的业务状态，例如 HUMAN_REVIEW_PENDING。
    status: str
    # 还没有被采纳、忽略或修改的风险编号。
    pending_risk_ids: list[str]
    # 这个任务已经人工恢复过几次。
    recovery_count: int


# 测试用这个集合确认 Checkpoint 没有偷偷保存合同正文或完整风险。
CONTROL_STATE_KEYS = frozenset(ReviewControlState.__annotations__)


class ReviewWorkflowHandlers(Protocol):
    """主图要求传入对象必须提供哪些方法。

    ``Protocol`` 只是类型检查清单，不会执行这些方法。
    真正的实现是 ``ReviewOrchestratorAgent`` 中的同名方法。
    """

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
    """控制图需要的两个实际处理方法。"""

    def checkpoint_phase(self, state: ReviewControlState, runtime) -> dict: ...

    def await_human_review(self, state: ReviewControlState, runtime) -> dict: ...


# 这两个常量把节点顺序列出来，便于测试验证图没有漏步骤。
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
    """把合同审查步骤连接成一张可执行的主图。

    两个参数可以这样理解：

    * ``handlers`` 是“干活的人”，里面有解析、分类和检索方法。
    * ``context_schema`` 是“工具箱的规格”，工具箱里有数据库和合同内容。

    State 是步骤之间传递的数据。context 是每个步骤都能使用的工具箱。
    """

    # 先造好“处理一条风险”的小流程，主图后面会反复调用它。
    risk_graph = _build_risk_graph(handlers, context_schema)

    def run_risk_subgraph(state: ReviewGraphState, runtime) -> dict:
        # 假设有 3 条风险，这个函数会被调用 3 次。
        # 每次拿到不同的 risk_item，但都使用同一套风险处理步骤。
        result = risk_graph.invoke(state, context=runtime.context)
        return {"risk_results": list(result.get("risk_results") or [])}

    # 第 1 步：创建一张空白流程图，并声明工作单类型。
    graph = StateGraph(ReviewGraphState, context_schema=context_schema)
    # 第 2 步：登记所有处理站点。
    # 左边是流程图中的名字，右边是实际执行的 Python 方法。
    graph.add_node("bootstrap", handlers.bootstrap)
    graph.add_node("parse_document", handlers.parse_document)
    graph.add_node("classify_contract", handlers.classify_contract)
    graph.add_node("structure_clauses", handlers.structure_clauses)
    graph.add_node("retrieve_playbook", handlers.retrieve_playbook)
    graph.add_node("build_context", handlers.build_context)
    graph.add_node("risk_subgraph", run_risk_subgraph)
    graph.add_node("aggregate_risks", handlers.aggregate_risks)

    # 第 3 步：用箭头连接节点。START 表示入口。
    graph.add_edge(START, "bootstrap")
    # guarded edge 的意思是“先看 terminal”。
    # terminal=False 就去下一步；terminal=True 就直接结束。
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
    # 每条风险子流程完成后都来到 aggregate_risks。
    # LangGraph 会等同一批分支全部结束，再执行汇总。
    graph.add_edge("risk_subgraph", "aggregate_risks")
    graph.add_edge("aggregate_risks", END)
    # 第 4 步：compile 检查图是否完整，并生成可执行对象。
    # 主图的结果每一步都写数据库，所以这里不额外保存 Checkpoint。
    return graph.compile()


def build_review_control_graph(
    handlers: ReviewControlHandlers,
    context_schema: type,
    checkpointer,
):
    """创建一张专门负责“暂停和继续”的小图。

    例子：系统发现 3 条风险，需要人处理。

    1. ``await_human_review`` 暂停图。
    2. Checkpointer 记住暂停位置。
    3. 用户采纳第 1 条后，程序继续运行。
    4. 还有 2 条未处理，所以再次暂停。
    5. 全部处理后走到 END。
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
    # 把 checkpointer 交给 compile 后，interrupt 才能在服务重启后继续。
    return graph.compile(checkpointer=checkpointer)


def _build_risk_graph(handlers: ReviewWorkflowHandlers, context_schema: type):
    """创建“只处理一条风险”的小流程。

    正常路线：
    准备数据 -> 模型分析 -> Critic 复核 -> 验证证据 -> 整理结果。

    如果证据不够，路线会变成：
    重新检索 -> 再分析 -> 再复核 -> 再验证。

    最多重试几次由节点代码控制，模型不能随便跳到任意函数。
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
    # routes 是“每个站点允许去哪里”的白名单。
    # 例如 verify_evidence 只能去 repair_retrieval 或 finalize_risk。
    for node_name, destinations in routes.items():
        graph.add_conditional_edges(
            node_name,
            _branch_route,
            destinations,
        )
    graph.add_edge("finalize_risk", END)
    return graph.compile()


def _add_guarded_edge(graph: StateGraph, source: str, target: str) -> None:
    """连接两个步骤，但在前一步失败时直接结束。

    ``source`` 是当前节点名，``target`` 是正常情况下的下一节点名。
    """

    graph.add_conditional_edges(
        source,
        lambda state: END if state.get("terminal") else target,
        [target, END],
    )


def _dispatch_risk_items(state: ReviewGraphState):
    """把多条风险拆成多份相同的小任务。

    假设 ``risk_items`` 有 3 项，这里就返回 3 个 ``Send``：

    * Send 1：让 risk_subgraph 处理风险 1。
    * Send 2：让 risk_subgraph 处理风险 2。
    * Send 3：让 risk_subgraph 处理风险 3。

    这就是本项目的并行来源。DeepSeek 模式最多同时跑 2 份。
    """

    if state.get("terminal"):
        return END
    risk_items = list(state.get("risk_items") or [])
    if not risk_items:
        return "aggregate_risks"
    return [
        Send(
            # 第一个参数：把任务送到哪个节点。
            "risk_subgraph",
            {
                # 第二个参数：这次调用能看到的 State。
                "task_id": state["task_id"],
                "risk_item": risk_item,
                # 每个分支从空结果开始，完成后由 add 合并。
                "risk_results": [],
            },
        )
        for risk_item in risk_items
    ]


def _branch_route(state: ReviewGraphState) -> str:
    """读取当前风险的下一步；没写下一步时直接整理结果。"""

    return str(state.get("branch_route") or "finalize_risk")
