from pathlib import Path
from unittest.mock import patch
import sys
import unittest

from pydantic import BaseModel


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from tools.adapters import invoke_validated_tool, structured_tools
from tools.contracts import tool_contracts
from tools.registry import tool_registry


EXPECTED_TOOL_NAMES = {
    "parse_document",
    "classify_contract_type",
    "extract_clauses",
    "extract_key_fields",
    "retrieve_playbook_rules",
    "retrieve_related_clauses",
    "retrieve_memory",
    "analyze_risk",
    "criticize_risk",
    "plan_review_action",
    "verify_evidence",
    "generate_revision",
    "write_memory",
    "generate_report",
}


class ToolAdapterTest(unittest.TestCase):
    def test_every_registered_tool_has_pydantic_input_and_output_models(self):
        self.assertEqual(EXPECTED_TOOL_NAMES, set(tool_registry))
        self.assertEqual(EXPECTED_TOOL_NAMES, set(tool_contracts))
        for tool_name, contract in tool_contracts.items():
            self.assertTrue(issubclass(contract.input_model, BaseModel), tool_name)
            self.assertTrue(issubclass(contract.output_model, BaseModel), tool_name)
            payload = contract.to_dict()
            self.assertEqual(payload["name"], tool_name)
            self.assertEqual(payload["input_json_schema"]["type"], "object")
            self.assertEqual(payload["output_json_schema"]["type"], "object")

    def test_structured_tools_are_generated_from_the_registry(self):
        generated = structured_tools()
        self.assertEqual(list(tool_registry), [tool.name for tool in generated])
        for tool in generated:
            self.assertIs(tool.args_schema, tool_contracts[tool.name].input_model)

    def test_adapter_converts_document_payload_and_wraps_clause_output(self):
        source_text = "  第一条 保密义务  "
        result = invoke_validated_tool(
            "extract_clauses",
            {
                "document": {
                    "contract_id": "contract-test",
                    "file_name": "sample.docx",
                    "file_type": "docx",
                    "content_hash": "hash",
                    "paragraphs": [
                        {
                            "block_id": "block-1",
                            "block_type": "paragraph",
                            "text": source_text,
                            "order": 0,
                            "source_location": {},
                        }
                    ],
                    "tables": [],
                    "blocks": [
                        {
                            "block_id": "block-1",
                            "block_type": "paragraph",
                            "text": source_text,
                            "order": 0,
                            "source_location": {},
                        }
                    ],
                    "page_map": [],
                }
            },
        )
        self.assertEqual("CL-001", result["clauses"][0]["clause_id"])
        self.assertEqual("保密义务", result["clauses"][0]["clause_type"])
        self.assertEqual(source_text.strip(), result["clauses"][0]["text"])

    def test_validation_does_not_normalize_source_or_evidence_text(self):
        source_text = "  original contract text\n"
        validated = tool_contracts[
            "classify_contract_type"
        ].input_model.model_validate(
            {"document": {**_document_payload(), "blocks": [
                {
                    "block_id": "block-1",
                    "block_type": "paragraph",
                    "text": source_text,
                    "order": 0,
                    "source_location": {},
                }
            ]}}
        )
        self.assertEqual(source_text, validated.document.blocks[0].text)

    def test_adapter_rejects_unknown_input_and_invalid_tool_output(self):
        with self.assertRaisesRegex(ValueError, "input validation failed"):
            invoke_validated_tool(
                "retrieve_playbook_rules",
                {
                    "contract_type": "NDA",
                    "clause_type": "保密义务",
                    "key_fields": {},
                    "review_position": "甲方",
                    "unexpected": "not allowed",
                },
            )

        with patch.dict(
            tool_registry,
            {
                "classify_contract_type": lambda _tool_input: {
                    "contract_type": "NDA",
                    "confidence": 2,
                    "evidence": ["invalid"],
                    "decision": "SUPPORTED",
                }
            },
        ):
            with self.assertRaisesRegex(ValueError, "output validation failed"):
                invoke_validated_tool(
                    "classify_contract_type",
                    {"document": _document_payload()},
                )

    def test_write_memory_contract_rejects_an_unknown_review_position(self):
        with self.assertRaisesRegex(ValueError, "input validation failed"):
            invoke_validated_tool(
                "write_memory",
                {
                    "human_feedback": {
                        "contract_type": "NDA",
                        "clause_type": "保密义务",
                        "risk_type": "保密信息范围过宽",
                        "review_position": "第三方",
                        "user_action": "accept",
                        "source_finding_id": "risk-1",
                        "source_clause_id": "CL-001",
                    }
                },
            )


def _document_payload() -> dict:
    return {
        "contract_id": "contract-test",
        "file_name": "sample.docx",
        "file_type": "docx",
        "content_hash": "hash",
        "paragraphs": [],
        "tables": [],
        "blocks": [],
        "page_map": [],
    }


if __name__ == "__main__":
    unittest.main()
