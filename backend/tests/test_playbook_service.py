from pathlib import Path
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from services.log_service import invoke_tool
from services.playbook_service import load_playbook_rules, retrieve_playbook_rules
from tools.contracts import tool_contracts
from tools.registry import tool_registry


CORE_RISKS = {
    "保密信息范围过宽",
    "缺少保密信息例外",
    "保密期限不合理",
    "使用目的或使用限制不清",
    "允许披露对象过宽",
    "返还或销毁义务不明确",
    "责任无限或责任边界不清",
    "违约责任或违约金明显不合理",
}

REQUIRED_RULE_FIELDS = {
    "rule_id",
    "contract_type",
    "clause_type",
    "risk_type",
    "check_point",
    "risk_criteria",
    "positions",
}


class PlaybookServiceTest(unittest.TestCase):
    def test_nda_playbook_covers_first_version_core_risks(self):
        rules = load_playbook_rules()
        payloads = [rule.to_dict() for rule in rules]

        self.assertEqual(len(payloads), 8)
        self.assertEqual({item["risk_type"] for item in payloads}, CORE_RISKS)
        for item in payloads:
            self.assertTrue(REQUIRED_RULE_FIELDS.issubset(item))
            self.assertEqual(item["contract_type"], "NDA")
            self.assertEqual(set(item["positions"]), {"甲方", "乙方"})
            for position_config in item["positions"].values():
                self.assertIn(position_config["severity_default"], {"高", "中", "低"})
                self.assertTrue(position_config["risk_focus"])
                self.assertTrue(position_config["revision_template"])

    def test_retrieve_rules_by_contract_clause_fields_and_position(self):
        matched_rules = retrieve_playbook_rules(
            {
                "contract_type": "NDA",
                "clause_type": "期限",
                "key_fields": {"confidentiality_period": ["永久"]},
                "review_position": "乙方",
            }
        )

        self.assertEqual(len(matched_rules), 1)
        self.assertEqual(matched_rules[0]["rule_id"], "NDA-R003")
        self.assertEqual(matched_rules[0]["risk_type"], "保密期限不合理")
        self.assertEqual(matched_rules[0]["matched_key_fields"], ["confidentiality_period"])
        self.assertEqual(matched_rules[0]["review_position"], "乙方")
        self.assertEqual(matched_rules[0]["severity_default"], "高")
        self.assertEqual(
            matched_rules[0]["position_config"]["revision_template"],
            matched_rules[0]["revision_template"],
        )

    def test_unsupported_contract_type_returns_no_rules(self):
        matched_rules = retrieve_playbook_rules(
            {
                "contract_type": "MSA",
                "clause_type": "期限",
                "key_fields": {"confidentiality_period": ["3年"]},
                "review_position": "甲方",
            }
        )

        self.assertEqual(matched_rules, [])

    def test_no_clause_match_returns_no_rules_without_formal_risk(self):
        matched_rules = retrieve_playbook_rules(
            {
                "contract_type": "NDA",
                "clause_type": "争议解决",
                "key_fields": {},
                "review_position": "甲方",
            }
        )

        self.assertEqual(matched_rules, [])

    def test_registered_tool_invokes_playbook_service(self):
        logs = []

        matched_rules = invoke_tool(
            "task_test",
            tool_registry,
            "retrieve_playbook_rules",
            {
                "contract_type": "NDA",
                "clause_type": "允许披露",
                "key_fields": {"permitted_disclosure_targets": ["顾问"]},
                "review_position": "甲方",
            },
            logs,
            step_name="playbook_retrieval",
        )

        self.assertEqual(matched_rules[0]["rule_id"], "NDA-R005")
        self.assertEqual(logs[0].tool_name, "retrieve_playbook_rules")
        self.assertEqual(logs[0].status, "success")
        self.assertFalse(tool_contracts["retrieve_playbook_rules"].calls_llm)
        self.assertIn("contract_type", tool_contracts["retrieve_playbook_rules"].input_schema)


if __name__ == "__main__":
    unittest.main()
