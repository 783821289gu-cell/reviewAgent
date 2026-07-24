from operator import add
from typing import Annotated, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send


class ReviewGraphState(TypedDict, total=False):
    task_id: str
    terminal: bool
    risk_items: list[dict]
    risk_item: dict
    risk_results: Annotated[list[dict], add]
    branch_route: str
    branch_payload: dict


class ReviewControlState(TypedDict, total=False):
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
    risk_graph = _build_risk_graph(handlers, context_schema)

    def run_risk_subgraph(state: ReviewGraphState, runtime) -> dict:
        result = risk_graph.invoke(state, context=runtime.context)
        return {"risk_results": list(result.get("risk_results") or [])}

    graph = StateGraph(ReviewGraphState, context_schema=context_schema)
    graph.add_node("bootstrap", handlers.bootstrap)
    graph.add_node("parse_document", handlers.parse_document)
    graph.add_node("classify_contract", handlers.classify_contract)
    graph.add_node("structure_clauses", handlers.structure_clauses)
    graph.add_node("retrieve_playbook", handlers.retrieve_playbook)
    graph.add_node("build_context", handlers.build_context)
    graph.add_node("risk_subgraph", run_risk_subgraph)
    graph.add_node("aggregate_risks", handlers.aggregate_risks)

    graph.add_edge(START, "bootstrap")
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
    graph.add_edge("risk_subgraph", "aggregate_risks")
    graph.add_edge("aggregate_risks", END)
    return graph.compile()


def build_review_control_graph(
    handlers: ReviewControlHandlers,
    context_schema: type,
    checkpointer,
):
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
    return graph.compile(checkpointer=checkpointer)


def _build_risk_graph(handlers: ReviewWorkflowHandlers, context_schema: type):
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
    for node_name, destinations in routes.items():
        graph.add_conditional_edges(
            node_name,
            _branch_route,
            destinations,
        )
    graph.add_edge("finalize_risk", END)
    return graph.compile()


def _add_guarded_edge(graph: StateGraph, source: str, target: str) -> None:
    graph.add_conditional_edges(
        source,
        lambda state: END if state.get("terminal") else target,
        [target, END],
    )


def _dispatch_risk_items(state: ReviewGraphState):
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
    return str(state.get("branch_route") or "finalize_risk")
