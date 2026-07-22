import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from providers.llm_provider import LLMOutputInvalidError
from services.llm_service import PLANNER_OUTPUT_SCHEMA
from services.log_service import invoke_tool
from services.planner_service import _validated_planner_input, plan_review_action
from services.prompt_service import build_prompt_package
from tools.registry import tool_registry


class PlannerTest(unittest.TestCase):
    def test_evidence_failure_routes_to_one_restricted_retrieval(self):
        decision = plan_review_action(
            planner_input(
                trigger_reason="EVIDENCE_MISSING",
                current_status="RISK_ANALYZED",
                retry_count=0,
            )
        )

        self.assertEqual(decision["action"], "RETRIEVE_AGAIN")
        self.assertEqual(decision["reason_code"], "EVIDENCE_MISSING")
        self.assertEqual(decision["target_clause_id"], "CL-001")
        self.assertEqual(
            set(decision["query_adjustments"]),
            {"additional_keywords", "top_k"},
        )
        self.assertLessEqual(decision["query_adjustments"]["top_k"], 5)

    def test_low_confidence_requests_human_review_without_query_changes(self):
        decision = plan_review_action(
            planner_input(
                trigger_reason="LOW_CONFIDENCE",
                current_status="RISK_ANALYZED",
            )
        )

        self.assertEqual(decision["action"], "REQUEST_HUMAN_REVIEW")
        self.assertEqual(decision["query_adjustments"], {})

    def test_human_review_discards_unused_model_query_adjustments(self):
        candidate = {
            **valid_decision(),
            "action": "REQUEST_HUMAN_REVIEW",
            "query_adjustments": {
                "additional_keywords": ["unused"],
                "top_k": 5,
            },
        }
        with patch(
            "services.planner_service.generate_structured_planner",
            return_value=candidate,
        ):
            decision = plan_review_action(planner_input())

        self.assertEqual(decision["action"], "REQUEST_HUMAN_REVIEW")
        self.assertEqual(decision["query_adjustments"], {})

    def test_analyzer_verifier_conflict_uses_fixed_reason_code(self):
        decision = plan_review_action(
            planner_input(trigger_reason="ANALYZER_VERIFIER_CONFLICT")
        )

        self.assertEqual(decision["action"], "RETRIEVE_AGAIN")
        self.assertEqual(decision["reason_code"], "ANALYZER_VERIFIER_CONFLICT")

    def test_unknown_target_clause_is_rejected_before_provider_call(self):
        with patch("services.planner_service.generate_structured_planner") as provider:
            with self.assertRaisesRegex(ValueError, "outside the current contract"):
                plan_review_action(planner_input(target_clause_id="CL-999"))

        provider.assert_not_called()

    def test_trigger_from_unauthorized_status_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not allowed in current_status"):
            plan_review_action(planner_input(current_status="DOCUMENT_PARSED"))

    def test_invalid_action_and_formal_risk_fields_are_never_accepted(self):
        invalid_outputs = [
            {
                **valid_decision(),
                "action": "CALL_ANY_TOOL",
            },
            {
                **valid_decision(),
                "risk_finding": {"risk_type": "保密信息范围过宽"},
            },
        ]
        for output in invalid_outputs:
            with self.subTest(output=output):
                with patch(
                    "services.planner_service.generate_structured_planner",
                    return_value=output,
                ):
                    with self.assertRaises(LLMOutputInvalidError):
                        plan_review_action(planner_input())

    def test_exhausted_retry_budget_rejects_retrieve_again(self):
        with patch(
            "services.planner_service.generate_structured_planner",
            return_value=valid_decision(),
        ):
            with self.assertRaisesRegex(LLMOutputInvalidError, "retry budget is exhausted"):
                plan_review_action(planner_input(retry_count=1))

    def test_query_adjustments_reject_unlisted_fields_and_oversized_top_k(self):
        invalid_adjustments = [
            {"tool_name": "generate_report"},
            {"top_k": 6},
            {"additional_keywords": []},
            {"additional_keywords": ["x"] * 6},
        ]
        for adjustments in invalid_adjustments:
            with self.subTest(adjustments=adjustments):
                with patch(
                    "services.planner_service.generate_structured_planner",
                    return_value={
                        **valid_decision(),
                        "query_adjustments": adjustments,
                    },
                ):
                    with self.assertRaises(LLMOutputInvalidError):
                        plan_review_action(planner_input())

    def test_deepseek_prompt_declares_only_trigger_allowed_actions_and_contract_clauses(self):
        validated_input = _validated_planner_input(planner_input())
        prompt = build_prompt_package(
            "plan_review_action",
            validated_input,
            PLANNER_OUTPUT_SCHEMA,
        )
        system_payload = json.loads(prompt.messages[0]["content"])
        allowed = system_payload["task"]["allowed_values"]

        self.assertEqual(
            allowed["action_names"],
            ["REQUEST_HUMAN_REVIEW", "RETRIEVE_AGAIN", "TERMINATE"],
        )
        self.assertEqual(allowed["target_clause_ids"], ["CL-001", "CL-002"])
        self.assertEqual(allowed["tool_names"], [])

    def test_registered_planner_writes_a_redacted_decision_log(self):
        logs = []

        decision = invoke_tool(
            "task_planner",
            tool_registry,
            "plan_review_action",
            planner_input(failure_reason="evidence_text not found in clause text"),
            logs,
            step_name="planner_route",
        )

        self.assertEqual(decision["action"], "RETRIEVE_AGAIN")
        self.assertEqual(logs[0].status, "success")
        self.assertIn("trigger=EVIDENCE_MISSING", logs[0].input_summary)
        self.assertNotIn("evidence_text not found", logs[0].input_summary)
        self.assertIn("action=RETRIEVE_AGAIN", logs[0].output_summary)
        self.assertEqual(
            logs[0].token_cost_summary,
            "deterministic_policy_no_external_llm",
        )


def planner_input(**overrides) -> dict:
    payload = {
        "trigger_reason": "EVIDENCE_MISSING",
        "current_status": "RISK_ANALYZED",
        "target_clause_id": "CL-001",
        "contract_clause_ids": ["CL-001", "CL-002"],
        "retry_count": 0,
        "failure_reason": "evidence_text not found in clause text",
    }
    payload.update(overrides)
    return payload


def valid_decision() -> dict:
    return {
        "action": "RETRIEVE_AGAIN",
        "reason_code": "EVIDENCE_MISSING",
        "target_clause_id": "CL-001",
        "query_adjustments": {
            "additional_keywords": ["原文证据"],
            "top_k": 5,
        },
        "confidence": 0.9,
    }


if __name__ == "__main__":
    unittest.main()
