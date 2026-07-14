from pathlib import Path
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from models.risk import VALID_RISK_TYPES
from services.evidence_service import verify_evidence
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
        self.assertTrue(finding["revision_suggestion"])
        self.assertEqual(finding["review_status"], "CONFIRMED_RISK")

    def test_analyze_risk_retries_once_after_invalid_output(self):
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
                "revision_suggestion": "限定保密信息范围。",
                "review_status": "CONFIRMED_RISK",
            },
        ]

        finding = analyze_risk({"review_context": context})

        self.assertEqual(finding["risk_type"], "保密信息范围过宽")

    def test_analyze_risk_invalid_after_retry_raises(self):
        context = build_review_context()
        context["debug_llm_outputs"] = [{"risk_type": "bad"}, {"risk_type": "still_bad"}]

        with self.assertRaises(ValueError):
            analyze_risk({"review_context": context})

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

        revision = generate_revision({"finding": finding, "preferred_position": "乙方"})

        self.assertEqual(revision["preferred_position"], "乙方")
        self.assertEqual(revision["basis_rule_ids"], ["NDA-R001"])
        self.assertTrue(revision["revision_suggestion"])

    def test_generate_revision_exposes_memory_reference_when_used(self):
        context = build_review_context()
        finding = analyze_risk({"review_context": context})
        finding["related_memory"] = [
            {
                "memory_id": "MEM-001",
                "user_action": "update_suggestion",
                "final_suggestion": "历史反馈要求限定披露场景。",
                "source_finding_id": "RISK-OLD",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

        revision = generate_revision({"finding": finding, "preferred_position": "乙方"})

        self.assertIn("历史反馈参考", revision["revision_suggestion"])
        self.assertEqual(revision["memory_references"][0]["memory_id"], "MEM-001")


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
            "severity_default": "中",
            "check_point": "检查保密信息定义是否限定来源、形式、标识、披露场景和合理范围。",
            "revision_template": "建议将保密信息限定为披露方以可识别方式披露并标注为保密的信息。",
        },
        "related_clauses": [],
        "related_memory": [],
        "output_constraints": {},
        "evidence_constraints": {},
    }


if __name__ == "__main__":
    unittest.main()
