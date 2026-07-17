from pathlib import Path
import sys
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))

from models.contract import ContractTypeClassification
from models.review import ReviewPosition, ReviewStatus
from providers.llm_provider import LLMProviderError
from services.evaluation_service import _docx_bytes_from_text
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from tools.contracts import runtime_calls_llm, runtime_llm_mode, tool_contracts
from tools.registry import tool_registry


SAMPLE_DIR = PROJECT_ROOT / "samples" / "contract_type"


class ContractTypeTest(unittest.TestCase):
    def test_chinese_and_english_nda_enter_review_pipeline(self):
        for sample_name in ("nda_zh.txt", "nda_en.txt"):
            with self.subTest(sample=sample_name):
                state = self._run_sample(sample_name)
                self.assertIn(
                    state.status,
                    {ReviewStatus.EVIDENCE_VERIFIED, ReviewStatus.HUMAN_REVIEW_PENDING},
                )
                self.assertEqual(state.contract_classification["contract_type"], "NDA")
                self.assertEqual(state.contract_classification["decision"], "SUPPORTED")
                self.assertGreaterEqual(state.contract_classification["confidence"], 0.8)
                self.assertTrue(state.contract_classification["evidence"])
                self.assertIn("classify_contract_type", [log["tool_name"] for log in state.logs])
                self.assertIn("retrieve_playbook_rules", [log["tool_name"] for log in state.logs])

    def test_procurement_service_and_employment_are_rejected_before_playbook(self):
        expected_types = {
            "procurement_zh.txt": "PROCUREMENT",
            "service_en.txt": "SERVICE",
            "employment_zh.txt": "EMPLOYMENT",
        }
        for sample_name, expected_type in expected_types.items():
            with self.subTest(sample=sample_name):
                state = self._run_sample(sample_name)
                self.assertEqual(state.status, ReviewStatus.UNSUPPORTED_CONTRACT_TYPE)
                self.assertEqual(state.contract_classification["contract_type"], expected_type)
                self.assertEqual(
                    state.contract_classification["decision"],
                    "UNSUPPORTED_CONTRACT_TYPE",
                )
                self.assertEqual(state.risk_findings, [])
                tool_names = [log["tool_name"] for log in state.logs]
                self.assertEqual(tool_names, ["parse_document", "classify_contract_type"])
                self.assertNotIn("retrieve_playbook_rules", tool_names)

    def test_ambiguous_contract_requires_manual_review_without_playbook(self):
        state = self._run_sample("ambiguous_zh.txt")

        self.assertEqual(state.status, ReviewStatus.NEED_MANUAL_REVIEW)
        self.assertEqual(state.contract_classification["contract_type"], "UNKNOWN")
        self.assertEqual(state.contract_classification["decision"], "NEED_MANUAL_REVIEW")
        self.assertIn("需要人工复核", state.message)
        self.assertEqual(state.risk_findings, [])
        self.assertNotIn("retrieve_playbook_rules", [log["tool_name"] for log in state.logs])

    def test_classification_tool_has_structured_contract_and_no_fake_external_llm(self):
        contract = tool_contracts["classify_contract_type"]
        self.assertEqual(set(contract.input_schema), {"document"})
        self.assertEqual(
            set(contract.output_schema),
            {"contract_type", "confidence", "evidence", "decision"},
        )
        self.assertTrue(contract.calls_llm)
        self.assertFalse(runtime_calls_llm("classify_contract_type"))
        self.assertEqual(
            runtime_llm_mode("classify_contract_type"),
            "deterministic_features_no_external_llm",
        )

        logs = []
        result = invoke_tool(
            "task_contract_type",
            tool_registry,
            "classify_contract_type",
            {"document": _document_payload(self._sample_text("nda_zh.txt"))},
            logs,
            step_name="contract_type_classification",
        )
        self.assertEqual(
            set(result),
            {"contract_type", "confidence", "evidence", "decision"},
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].status, "success")
        self.assertEqual(logs[0].token_cost_summary, "deterministic_features_no_external_llm")

    def test_client_file_name_cannot_promote_ambiguous_content_to_nda(self):
        state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
            file_name="customer_nda.docx",
            file_type="docx",
            content=_docx_bytes_from_text(
                "Collaboration Agreement\nThe parties may exchange Confidential Information."
            ),
            review_position=ReviewPosition.PARTY_A,
        )

        self.assertEqual(state.status, ReviewStatus.NEED_MANUAL_REVIEW)
        self.assertEqual(state.contract_classification["decision"], "NEED_MANUAL_REVIEW")
        self.assertEqual(state.contract_classification["contract_type"], "UNKNOWN")
        self.assertNotIn("retrieve_playbook_rules", [log["tool_name"] for log in state.logs])

    def test_nda_title_is_not_overridden_by_body_reference_to_services_agreement(self):
        result = tool_registry["classify_contract_type"](
            {
                "document": _document_payload(
                    "Non-Disclosure Agreement\n"
                    "The parties may later enter into a Master Services Agreement.\n"
                    "The Receiving Party shall not disclose Confidential Information of the Discloser."
                )
            }
        )

        self.assertEqual(result["decision"], "SUPPORTED")
        self.assertEqual(result["contract_type"], "NDA")

    def test_confidentiality_section_inside_generic_agreement_is_not_promoted_to_nda(self):
        result = tool_registry["classify_contract_type"](
            {
                "document": _document_payload(
                    "Cooperation Agreement\n"
                    "Confidential Information means commercial information shared by either party.\n"
                    "Each party shall keep confidential all Confidential Information."
                )
            }
        )

        self.assertEqual(result["decision"], "NEED_MANUAL_REVIEW")
        self.assertEqual(result["contract_type"], "UNKNOWN")

    def test_high_confidence_deterministic_classification_skips_provider(self):
        with patch("services.contract_type_service.generate_structured_contract_type") as provider_call:
            result = tool_registry["classify_contract_type"](
                {"document": _document_payload(self._sample_text("nda_zh.txt"))}
            )

        self.assertEqual(result["decision"], "SUPPORTED")
        provider_call.assert_not_called()

    def test_low_confidence_classification_uses_structured_provider(self):
        llm_result = {
            "contract_type": "UNKNOWN",
            "confidence": 0.6,
            "evidence": ["模型仍无法确认合同类型"],
            "decision": "NEED_MANUAL_REVIEW",
        }
        with patch(
            "services.contract_type_service.generate_structured_contract_type",
            return_value=llm_result,
        ) as provider_call:
            result = tool_registry["classify_contract_type"](
                {"document": _document_payload("Cooperation Agreement\nGeneral commercial terms.")}
            )

        self.assertEqual(result, llm_result)
        provider_call.assert_called_once()

    def test_low_confidence_provider_failure_keeps_manual_review(self):
        with patch(
            "services.contract_type_service.generate_structured_contract_type",
            side_effect=LLMProviderError("temporary_error", "controlled failure", True),
        ) as provider_call:
            result = tool_registry["classify_contract_type"](
                {"document": _document_payload("Cooperation Agreement\nGeneral terms.")}
            )

        self.assertEqual(provider_call.call_count, 2)
        self.assertEqual(result["contract_type"], "UNKNOWN")
        self.assertEqual(result["decision"], "NEED_MANUAL_REVIEW")
        self.assertIn("temporary_error", result["evidence"][-1])

    def test_low_confidence_schema_failure_is_not_retried(self):
        with patch(
            "services.contract_type_service.generate_structured_contract_type",
            return_value={"contract_type": "UNKNOWN"},
        ) as provider_call:
            result = tool_registry["classify_contract_type"](
                {"document": _document_payload("Cooperation Agreement\nGeneral terms.")}
            )

        provider_call.assert_called_once()
        self.assertEqual(result["contract_type"], "UNKNOWN")
        self.assertEqual(result["decision"], "NEED_MANUAL_REVIEW")
        self.assertIn("schema_error", result["evidence"][-1])

    def test_common_non_nda_titles_are_rejected(self):
        for title, expected_type in {
            "Purchase Order": "PROCUREMENT",
            "Master Services Agreement": "SERVICE",
            "Employee Agreement": "EMPLOYMENT",
        }.items():
            with self.subTest(title=title):
                result = tool_registry["classify_contract_type"](
                    {"document": _document_payload(f"{title}\nStandard commercial terms.")}
                )
                self.assertEqual(result["decision"], "UNSUPPORTED_CONTRACT_TYPE")
                self.assertEqual(result["contract_type"], expected_type)

    def test_bare_nda_title_is_supported_without_using_file_name(self):
        result = tool_registry["classify_contract_type"](
            {
                "document": _document_payload(
                    "NDA\nConfidential Information includes technical information."
                )
            }
        )

        self.assertEqual(result["decision"], "SUPPORTED")
        self.assertEqual(result["contract_type"], "NDA")

    def test_structured_result_rejects_inconsistent_decision(self):
        with self.assertRaisesRegex(ValueError, "known non-NDA"):
            ContractTypeClassification(
                contract_type="NDA",
                confidence=0.9,
                evidence=["conflicting output"],
                decision="UNSUPPORTED_CONTRACT_TYPE",
            )
        with self.assertRaisesRegex(ValueError, "must be numeric"):
            ContractTypeClassification(
                contract_type="NDA",
                confidence=True,
                evidence=["invalid confidence"],
                decision="SUPPORTED",
            )
        with self.assertRaisesRegex(ValueError, "non-empty strings"):
            ContractTypeClassification(
                contract_type="NDA",
                confidence=0.9,
                evidence="not-a-list",
                decision="SUPPORTED",
            )

    def _run_sample(self, sample_name: str):
        text = self._sample_text(sample_name)
        return ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
            file_name=sample_name.replace(".txt", ".docx"),
            file_type="docx",
            content=_docx_bytes_from_text(text),
            review_position=ReviewPosition.PARTY_A,
        )

    def _sample_text(self, sample_name: str) -> str:
        return (SAMPLE_DIR / sample_name).read_text(encoding="utf-8")


def _document_payload(text: str, file_name: str = "sample.docx") -> dict:
    blocks = [
        {"block_id": f"B-{index:03d}", "text": line}
        for index, line in enumerate((item.strip() for item in text.splitlines()), start=1)
        if line
    ]
    return {"file_name": file_name, "blocks": blocks}


if __name__ == "__main__":
    unittest.main()
