from pathlib import Path
import sys
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TEST_DIR))

from models.playbook import PlaybookRule
from models.review import ReviewStatus
from services.context_builder import build_review_context
from services.playbook_service import load_playbook_rules, retrieve_playbook_rules
from services.task_service import create_review_task
from test_document_pipeline import build_docx_bytes


class PlaybookPositionTest(unittest.TestCase):
    def test_all_rules_define_complete_party_a_and_party_b_configs(self):
        rules = load_playbook_rules()

        self.assertEqual(len(rules), 8)
        self.assertEqual(len({rule.rule_id for rule in rules}), 8)
        for rule in rules:
            self.assertEqual(set(rule.positions), {"甲方", "乙方"})
            party_a = rule.resolve_position("甲方")
            party_b = rule.resolve_position("乙方")
            self.assertTrue(party_a.risk_focus)
            self.assertTrue(party_b.risk_focus)
            self.assertTrue(party_a.revision_template)
            self.assertTrue(party_b.revision_template)
            self.assertNotEqual(party_a.risk_focus, party_b.risk_focus)
            self.assertNotEqual(party_a.revision_template, party_b.revision_template)

    def test_retrieval_resolves_only_the_selected_position_config(self):
        base_input = {
            "contract_type": "NDA",
            "clause_type": "定义",
            "key_fields": {"right_holder": ["披露方"]},
        }

        party_a = retrieve_playbook_rules({**base_input, "review_position": "甲方"})[0]
        party_b = retrieve_playbook_rules({**base_input, "review_position": "乙方"})[0]

        self.assertEqual(party_a["rule_id"], party_b["rule_id"])
        self.assertEqual(party_a["review_position"], "甲方")
        self.assertEqual(party_b["review_position"], "乙方")
        self.assertNotEqual(party_a["severity_default"], party_b["severity_default"])
        self.assertNotEqual(party_a["risk_focus"], party_b["risk_focus"])
        self.assertNotEqual(party_a["revision_template"], party_b["revision_template"])

        context = build_review_context(
            contract_type="NDA",
            review_position="乙方",
            current_clause={
                "clause_id": "CL-001",
                "clause_type": "定义",
                "text": "保密信息包括甲方披露的所有商业信息。",
                "key_fields": {"right_holder": ["甲方"]},
            },
            matched_rule=party_b,
            related_clauses=[],
            related_memory=[],
        )
        self.assertEqual(context["review_position"], "乙方")
        self.assertEqual(context["matched_rule"]["position_config"], party_b["position_config"])
        self.assertEqual(context["matched_rule"]["risk_focus"], party_b["risk_focus"])

    def test_missing_selected_position_config_fails_in_retrieval(self):
        source = load_playbook_rules()[0].to_dict()
        source["positions"].pop("乙方")
        incomplete_rule = PlaybookRule.from_dict(source)

        with patch(
            "services.playbook_service.load_playbook_rules",
            return_value=(incomplete_rule,),
        ):
            with self.assertRaisesRegex(ValueError, "missing position config: 乙方"):
                retrieve_playbook_rules(
                    {
                        "contract_type": "NDA",
                        "clause_type": "定义",
                        "key_fields": {},
                        "review_position": "乙方",
                    }
                )

    def test_same_contract_has_traceable_party_a_and_party_b_differences(self):
        content = build_docx_bytes()

        party_a = create_review_task("same-nda.docx", content, "甲方").to_dict()
        party_b = create_review_task("same-nda.docx", content, "乙方").to_dict()

        self.assertIn(
            party_a["status"],
            {ReviewStatus.EVIDENCE_VERIFIED.value, ReviewStatus.HUMAN_REVIEW_PENDING.value},
        )
        self.assertIn(
            party_b["status"],
            {ReviewStatus.EVIDENCE_VERIFIED.value, ReviewStatus.HUMAN_REVIEW_PENDING.value},
        )
        finding_a = next(
            finding
            for finding in party_a["risk_findings"]
            if finding["risk_type"] == "保密信息范围过宽"
        )
        finding_b = next(
            finding
            for finding in party_b["risk_findings"]
            if finding["risk_type"] == "保密信息范围过宽"
        )

        self.assertEqual(finding_a["evidence_text"], finding_b["evidence_text"])
        self.assertEqual(finding_a["review_position"], "甲方")
        self.assertEqual(finding_b["review_position"], "乙方")
        self.assertNotEqual(finding_a["severity"], finding_b["severity"])
        self.assertNotEqual(finding_a["risk_focus"], finding_b["risk_focus"])
        self.assertNotEqual(finding_a["revision_suggestion"], finding_b["revision_suggestion"])
        self.assertIn("甲方立场风险重点", finding_a["risk_reason"])
        self.assertIn("乙方立场风险重点", finding_b["risk_reason"])


if __name__ == "__main__":
    unittest.main()
