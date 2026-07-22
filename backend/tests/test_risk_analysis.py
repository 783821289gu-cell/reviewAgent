from pathlib import Path
import sys
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from models.risk import VALID_RISK_TYPES, validate_risk_finding
from providers.llm_provider import LLMOutputInvalidError
from services.evidence_service import verify_evidence
from services.prompt_service import PROMPT_VERSION
from services.risk_analyzer import analyze_risk, generate_revision
from test_retrieval_context import build_clause_payloads


class RiskAnalysisTest(unittest.TestCase):
    def test_analyze_risk_returns_structured_result(self):
        context = build_review_context()

        finding = analyze_risk({"review_context": context})

        self.assertIn(finding["risk_type"], VALID_RISK_TYPES)
        self.assertEqual(finding["severity"], "中")
        self.assertGreaterEqual(finding["confidence"], 0)
        self.assertLessEqual(finding["confidence"], 1)
        self.assertEqual(finding["clause_id"], "CL-001")
        self.assertIn("商业信息", finding["evidence_text"])
        self.assertEqual(finding["matched_rule_ids"], ["NDA-R001"])
        self.assertEqual(finding["review_position"], "甲方")
        self.assertEqual(finding["risk_focus"], context["matched_rule"]["risk_focus"])
        self.assertIn("甲方立场风险重点", finding["risk_reason"])
        self.assertTrue(finding["revision_suggestion"])
        self.assertEqual(finding["review_status"], "CONFIRMED_RISK")

    def test_analyze_risk_does_not_retry_after_invalid_output(self):
        context = build_review_context()
        context["debug_llm_outputs"] = [
            {"risk_type": "bad"},
            {
                "risk_type": "保密信息范围过宽",
                "severity": "中",
                "confidence": 0.8,
                "risk_reason": "证据文本“保密信息是指披露方提供的商业信息、技术资料和合作资料。”命中检查点。",
                "clause_id": "CL-001",
                "evidence_text": "保密信息是指披露方提供的商业信息、技术资料和合作资料。",
                "matched_rule_ids": ["NDA-R001"],
                "review_position": "甲方",
                "risk_focus": context["matched_rule"]["risk_focus"],
                "revision_suggestion": "限定保密信息范围。",
                "review_status": "CONFIRMED_RISK",
            },
        ]

        with self.assertRaises(LLMOutputInvalidError):
            analyze_risk({"review_context": context})

    def test_analyze_risk_type_error_is_not_retried(self):
        context = build_review_context()
        invalid_output = {
            "risk_type": "保密信息范围过宽",
            "severity": "中",
            "confidence": True,
            "risk_reason": "证据文本命中检查点。",
            "clause_id": "CL-001",
            "evidence_text": "保密信息是指披露方提供的商业信息、技术资料和合作资料。",
            "matched_rule_ids": ["NDA-R001"],
            "review_position": "甲方",
            "risk_focus": context["matched_rule"]["risk_focus"],
            "revision_suggestion": "限定保密信息范围。",
            "review_status": "CONFIRMED_RISK",
        }

        with patch(
            "services.risk_analyzer.generate_structured_risk",
            return_value=invalid_output,
        ) as provider_call:
            with self.assertRaisesRegex(LLMOutputInvalidError, "confidence must be numeric"):
                analyze_risk({"review_context": context})

        provider_call.assert_called_once()

    def test_analyze_risk_canonicalizes_playbook_risk_focus(self):
        context = build_review_context()
        context["output_constraints"].pop("authoritative_fields", None)
        mismatched_output = {
            "risk_type": "保密信息范围过宽",
            "severity": "中",
            "confidence": 0.8,
            "risk_reason": "证据文本命中检查点。",
            "clause_id": "CL-001",
            "evidence_text": "保密信息是指披露方提供的商业信息、技术资料和合作资料。",
            "matched_rule_ids": ["NDA-R001"],
            "review_position": "乙方",
            "risk_focus": "与 Playbook 无关的风险重点。",
            "revision_suggestion": "限定保密信息范围。",
            "review_status": "CONFIRMED_RISK",
        }
        mismatched_output["review_position"] = context["review_position"]
        mismatched_output["revision_suggestion"] = ""
        with patch(
            "services.risk_analyzer.generate_structured_risk",
            return_value=mismatched_output,
        ) as provider_call:
            finding = analyze_risk({"review_context": context})

        provider_call.assert_called_once()
        provider_context = provider_call.call_args.args[0]
        self.assertEqual(
            provider_context["output_constraints"]["authoritative_fields"]["risk_focus"],
            context["matched_rule"]["position_config"]["risk_focus"],
        )
        self.assertEqual(provider_context["prompt_version"], PROMPT_VERSION)
        self.assertEqual(
            finding["risk_focus"],
            context["matched_rule"]["position_config"]["risk_focus"],
        )
        self.assertEqual(
            finding["revision_suggestion"],
            context["matched_rule"]["position_config"]["revision_template"],
        )

    def test_risk_model_rejects_weakly_typed_llm_output(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})
        finding["confidence"] = True
        with self.assertRaisesRegex(ValueError, "confidence must be numeric"):
            validate_risk_finding(finding)

        finding = analyze_risk({"review_context": context})
        finding["matched_rule_ids"] = [123]
        with self.assertRaisesRegex(ValueError, "non-empty strings"):
            validate_risk_finding(finding)

    def test_verify_evidence_requires_clause_text_and_rule_match(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})

        result = verify_evidence(
            {
                "finding": finding,
                "clauses": build_clause_payloads(),
                "matched_rule": context["matched_rule"],
            }
        )

        self.assertTrue(result["is_valid"])
        self.assertEqual(result["verified_clause_id"], "CL-001")

    def test_verify_evidence_rejects_missing_text(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})
        finding["evidence_text"] = "不存在的证据"

        result = verify_evidence(
            {
                "finding": finding,
                "clauses": build_clause_payloads(),
                "matched_rule": context["matched_rule"],
            }
        )

        self.assertFalse(result["is_valid"])
        self.assertEqual(result["failure_reason"], "evidence_text not found in clause text")

    def test_verify_evidence_rejects_unrelated_risk_reason(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})
        finding["risk_reason"] = "付款逾期导致商业风险。"

        result = verify_evidence(
            {
                "finding": finding,
                "clauses": build_clause_payloads(),
                "matched_rule": context["matched_rule"],
            }
        )

        self.assertFalse(result["is_valid"])
        self.assertEqual(result["failure_reason"], "risk_reason is not related to evidence_text")

    def test_generate_revision_returns_positioned_suggestion(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})

        revision = generate_revision({"finding": finding, "preferred_position": "甲方"})

        self.assertEqual(revision["preferred_position"], "甲方")
        self.assertEqual(revision["basis_rule_ids"], ["NDA-R001"])
        self.assertTrue(revision["revision_suggestion"])

    def test_generate_revision_uses_structured_provider_boundary(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})

        with patch(
            "services.risk_analyzer.generate_structured_revision",
            return_value={"revision_suggestion": "使用模型生成的结构化修改建议。"},
        ) as provider_call:
            revision = generate_revision({"finding": finding, "preferred_position": "甲方"})

        provider_call.assert_called_once()
        self.assertEqual(revision["revision_suggestion"], "使用模型生成的结构化修改建议。")

    def test_generate_revision_rejects_schema_errors_without_retry(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})

        for invalid_output in ({}, {"revision_suggestion": 123}):
            with self.subTest(invalid_output=invalid_output):
                with patch(
                    "services.risk_analyzer.generate_structured_revision",
                    return_value=invalid_output,
                ) as provider_call:
                    with self.assertRaises(LLMOutputInvalidError):
                        generate_revision(
                            {"finding": finding, "preferred_position": "甲方"}
                        )

                provider_call.assert_called_once()

    def test_generate_revision_rejects_position_mismatch(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})

        with self.assertRaisesRegex(ValueError, "does not match"):
            generate_revision({"finding": finding, "preferred_position": "乙方"})

    def test_generate_revision_exposes_memory_reference_when_used(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})
        finding["related_memory"] = [
            {
                "memory_id": "PREF-001",
                "memory_type": "semantic_preference",
                "memory_source": "sqlite_semantic_preference",
                "match_score": 8,
                "confidence": 0.65,
                "can_influence_suggestion": True,
                "final_suggestion": "历史反馈要求限定披露场景。",
                "source_memory_ids": ["MEM-001", "MEM-002"],
                "memory_injection": {"trimmed": False},
            },
            {
                "memory_id": "PREF-002",
                "memory_type": "semantic_preference",
                "memory_source": "sqlite_semantic_preference",
                "match_score": 7,
                "confidence": 0.55,
                "can_influence_suggestion": True,
                "final_suggestion": "This lower-ranked preference was not applied.",
                "source_memory_ids": ["MEM-003"],
                "memory_injection": {"trimmed": False},
            },
        ]

        revision = generate_revision({"finding": finding, "preferred_position": "甲方"})

        self.assertIn("历史反馈参考", revision["revision_suggestion"])
        self.assertNotIn("lower-ranked", revision["revision_suggestion"])
        self.assertEqual(len(revision["memory_references"]), 1)
        self.assertEqual(revision["memory_references"][0]["memory_id"], "PREF-001")
        self.assertTrue(revision["memory_references"][0]["suggestion_affected"])
        self.assertEqual(revision["basis_rule_ids"], finding["matched_rule_ids"])

    def test_generate_revision_does_not_use_conflicted_or_episodic_memory(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})
        baseline = generate_revision({"finding": finding, "preferred_position": "甲方"})
        finding["related_memory"] = [
            {
                "memory_id": "PREF-CONFLICT",
                "memory_type": "semantic_preference",
                "can_influence_suggestion": False,
                "final_suggestion": "Conflicted suggestion must not be used.",
            },
            {
                "memory_id": "MEM-RAW",
                "memory_type": "human_feedback",
                "can_influence_suggestion": True,
                "final_suggestion": "Raw feedback must not be used directly.",
            },
        ]

        revision = generate_revision({"finding": finding, "preferred_position": "甲方"})

        self.assertEqual(revision["revision_suggestion"], baseline["revision_suggestion"])
        self.assertEqual(revision["memory_references"], [])


def build_review_context() -> dict:
    return {
        "context_id": "CL-001:NDA-R001",
        "contract_type": "NDA",
        "review_position": "甲方",
        "current_clause": build_clause_payloads()[0],
        "clause_type": "定义",
        "key_fields": {"right_holder": ["披露方"]},
        "matched_rule": {
            "rule_id": "NDA-R001",
            "risk_type": "保密信息范围过宽",
            "review_position": "甲方",
            "severity_default": "中",
            "risk_focus": "确保甲方保密信息定义可识别且可执行。",
            "check_point": "检查保密信息定义是否限定来源、形式、标识、披露场景和合理范围。",
            "revision_template": "建议将保密信息限定为披露方以可识别方式披露并标注为保密的信息。",
            "position_config": {
                "severity_default": "中",
                "risk_focus": "确保甲方保密信息定义可识别且可执行。",
                "revision_template": "建议将保密信息限定为披露方以可识别方式披露并标注为保密的信息。",
            },
        },
        "related_clauses": [],
        "related_memory": [],
        "output_constraints": {},
        "evidence_constraints": {},
    }


if __name__ == "__main__":
    unittest.main()
