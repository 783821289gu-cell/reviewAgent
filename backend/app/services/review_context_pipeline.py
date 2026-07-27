from dataclasses import dataclass

from models.log import StepLog
from services.context_builder import build_review_context
from services.log_service import ToolExecutionControl, invoke_tool
from services.review_batch_executor import OrderedBatchExecutor
from tools.registry import tool_registry


@dataclass(frozen=True)
class ContextWorkItem:
    clause: dict
    rule: dict


@dataclass(frozen=True)
class ContextBuildOutcome:
    work_item: ContextWorkItem
    review_context: dict | None
    logs: tuple[StepLog, ...]
    error: Exception | None


def context_work_items(
    clauses: list[dict],
    rule_matches: list[dict],
) -> list[ContextWorkItem]:
    clauses_by_id = {clause["clause_id"]: clause for clause in clauses}
    items = []
    for rule_group in rule_matches:
        clause = clauses_by_id.get(rule_group["clause_id"])
        if clause is None:
            continue
        items.extend(
            ContextWorkItem(clause=clause, rule=rule)
            for rule in rule_group["matched_rules"]
        )
    return items


class ReviewContextPipeline:
    """Build evidence inputs without owning task state or persistence."""

    def __init__(
        self,
        batch_executor: OrderedBatchExecutor,
        *,
        registry: dict | None = None,
    ):
        self.batch_executor = batch_executor
        self.registry = tool_registry if registry is None else registry

    def build_batch(
        self,
        *,
        task_id: str,
        contract_type: str,
        review_position: str,
        clauses: list[dict],
        work_items: list[ContextWorkItem],
        execution_control: ToolExecutionControl,
        parent_step_id: str,
        embedding_cache: dict,
        db_path: str | None,
        max_workers: int,
    ) -> list[ContextBuildOutcome]:
        shared_cache = embedding_cache if max_workers == 1 else None

        def build_item(item: ContextWorkItem) -> ContextBuildOutcome:
            logs: list[StepLog] = []
            try:
                related = invoke_tool(
                    task_id,
                    self.registry,
                    "retrieve_related_clauses",
                    {
                        "contract_type": contract_type,
                        "current_clause": item.clause,
                        "clauses": clauses,
                        "risk_type": item.rule["risk_type"],
                        "playbook_check_point": item.rule["check_point"],
                        "limit": 3,
                        "embedding_cache": (
                            shared_cache if shared_cache is not None else {}
                        ),
                    },
                    logs,
                    step_name="related_clause_retrieval",
                    execution_control=execution_control,
                    parent_step_id=parent_step_id,
                )
                memory_input = {
                    "contract_type": contract_type,
                    "clause": item.clause,
                    "risk_type": item.rule["risk_type"],
                    "review_position": review_position,
                    "memory_items": [],
                    "limit": 3,
                }
                if db_path:
                    memory_input["db_path"] = db_path
                memory = invoke_tool(
                    task_id,
                    self.registry,
                    "retrieve_memory",
                    memory_input,
                    logs,
                    step_name="memory_retrieval",
                    execution_control=execution_control,
                )
                review_context = build_review_context(
                    contract_type=contract_type,
                    review_position=review_position,
                    current_clause=item.clause,
                    matched_rule=item.rule,
                    related_clauses=related,
                    related_memory=memory,
                )
                return ContextBuildOutcome(
                    work_item=item,
                    review_context=review_context,
                    logs=tuple(logs),
                    error=None,
                )
            except Exception as exc:
                return ContextBuildOutcome(
                    work_item=item,
                    review_context=None,
                    logs=tuple(logs),
                    error=exc,
                )

        return self.batch_executor.map_ordered(
            work_items,
            build_item,
            max_workers=max_workers,
        )
