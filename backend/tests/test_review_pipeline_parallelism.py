from contextvars import ContextVar
from pathlib import Path
from threading import Event, Lock
from time import perf_counter, sleep
import sys
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from models.log import StepLog
from services.log_service import ToolExecutionControl
from services.review_batch_executor import OrderedBatchExecutor
from services.review_context_pipeline import (
    ContextBuildOutcome,
    ContextWorkItem,
    ReviewContextPipeline,
    context_work_items,
)
from services.review_agent_support import ReviewAgentSupport


class ReviewPipelineParallelismTest(unittest.TestCase):
    def test_batch_failure_keeps_all_logs_and_safe_source_prefix(self):
        work_items = [
            ContextWorkItem(
                clause={"clause_id": f"CL-{index:03d}"},
                rule={"rule_id": f"R-{index:03d}"},
            )
            for index in range(1, 4)
        ]

        def step_log(index: int) -> StepLog:
            return StepLog(
                task_id="task_batch_failure",
                step_name=f"step-{index}",
                tool_name="test_tool",
                status="success",
                latency_ms=1,
                input_summary="",
                output_summary="",
                token_cost_summary="",
                error_message="",
            )

        outcomes = [
            ContextBuildOutcome(
                work_items[0],
                {"context_id": "CL-001"},
                (step_log(1),),
                None,
            ),
            ContextBuildOutcome(
                work_items[1],
                None,
                (step_log(2),),
                ValueError("failed"),
            ),
            ContextBuildOutcome(
                work_items[2],
                {"context_id": "CL-003"},
                (step_log(3),),
                None,
            ),
        ]
        logs = []
        ReviewAgentSupport._merge_batch_logs(logs, outcomes)

        applied = []
        with self.assertRaisesRegex(ValueError, "failed"):
            for outcome in outcomes:
                if outcome.error is not None:
                    raise outcome.error
                applied.append(outcome.review_context["context_id"])

        self.assertEqual(
            [log.step_name for log in logs],
            ["step-1", "step-2", "step-3"],
        )
        self.assertEqual(applied, ["CL-001"])

    def test_batch_executor_bounds_parallelism_and_preserves_source_order(self):
        active = 0
        maximum_active = 0
        lock = Lock()
        two_started = Event()
        request_context = ContextVar("request_context", default="")
        token = request_context.set("task-context")

        def worker(item: int) -> tuple[int, str]:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    two_started.set()
            try:
                two_started.wait(timeout=1)
                sleep(0.01 * (4 - item))
                return item, request_context.get()
            finally:
                with lock:
                    active -= 1

        try:
            results = OrderedBatchExecutor().map_ordered(
                [1, 2, 3],
                worker,
                max_workers=2,
            )
        finally:
            request_context.reset(token)

        self.assertEqual(maximum_active, 2)
        self.assertEqual([item for item, _ in results], [1, 2, 3])
        self.assertEqual(
            [context for _, context in results],
            ["task-context", "task-context", "task-context"],
        )

    def test_context_pipeline_parallelizes_independent_items_with_stable_output(self):
        active = 0
        maximum_active = 0
        lock = Lock()
        two_started = Event()

        def retrieve_related(tool_input: dict) -> list[dict]:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    two_started.set()
            try:
                two_started.wait(timeout=1)
                sleep(0.02)
                return [{"clause_id": tool_input["current_clause"]["clause_id"]}]
            finally:
                with lock:
                    active -= 1

        registry = {
            "retrieve_related_clauses": retrieve_related,
            "retrieve_memory": lambda tool_input: [
                {"memory_id": tool_input["clause"]["clause_id"]}
            ],
        }
        clauses = [
            {
                "clause_id": f"CL-{index:03d}",
                "clause_type": "保密义务",
                "title": f"Clause {index}",
                "text": f"Clause text {index}",
                "key_fields": {},
            }
            for index in range(1, 4)
        ]
        rule_matches = [
            {
                "clause_id": clause["clause_id"],
                "matched_rules": [
                    {
                        "rule_id": f"R-{clause['clause_id']}",
                        "risk_type": "保密范围",
                        "check_point": "检查保密范围。",
                    }
                ],
            }
            for clause in clauses
        ]
        work_items = context_work_items(clauses, rule_matches)
        control = ToolExecutionControl(
            cancel_check=lambda: None,
            task_started_at=perf_counter(),
            task_timeout_seconds=None,
            node_timeout_seconds=2,
        )
        pipeline = ReviewContextPipeline(
            OrderedBatchExecutor(),
            registry=registry,
        )

        with patch(
            "services.review_context_pipeline.build_review_context",
            side_effect=lambda **payload: {
                "context_id": payload["current_clause"]["clause_id"],
                "current_clause": payload["current_clause"],
                "matched_rule": payload["matched_rule"],
            },
        ):
            outcomes = pipeline.build_batch(
                task_id="task_parallel_context",
                contract_type="NDA",
                review_position="甲方",
                clauses=clauses,
                work_items=work_items,
                execution_control=control,
                parent_step_id="step_batch_parent",
                embedding_cache={},
                db_path=None,
                max_workers=2,
            )

        self.assertEqual(maximum_active, 2)
        self.assertTrue(all(outcome.error is None for outcome in outcomes))
        self.assertEqual(
            [
                outcome.review_context["context_id"]
                for outcome in outcomes
                if outcome.review_context is not None
            ],
            [clause["clause_id"] for clause in clauses],
        )
        self.assertTrue(all(len(outcome.logs) == 2 for outcome in outcomes))
        self.assertTrue(
            all(
                outcome.logs[0].parent_step_id == "step_batch_parent"
                and outcome.logs[1].parent_step_id == outcome.logs[0].step_id
                for outcome in outcomes
            )
        )


if __name__ == "__main__":
    unittest.main()
