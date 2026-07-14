from io import BytesIO
from pathlib import Path
import sys
import unittest
from zipfile import ZipFile


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from models.review import ReviewStatus
from services.task_service import create_review_task
from tools.registry import tool_registry


class DocumentPipelineTest(unittest.TestCase):
    def test_docx_upload_parses_paragraphs_tables_and_clauses(self):
        task = create_review_task(
            file_name="sample.docx",
            content=build_docx_bytes(),
            review_position_value="甲方",
        )

        self.assertEqual(task.status, ReviewStatus.EVIDENCE_VERIFIED)
        payload = task.to_dict()
        self.assertEqual(payload["file_type"], "docx")
        self.assertEqual(len(payload["document"]["paragraphs"]), 4)
        self.assertEqual(len(payload["document"]["tables"]), 1)
        self.assertTrue(payload["document"]["content_hash"])
        self.assertEqual(payload["clauses"][0]["clause_id"], "CL-001")
        self.assertEqual(payload["clauses"][0]["clause_type"], "定义")
        self.assertIn("义务", payload["clauses"][1]["title"])
        self.assertEqual(payload["clauses"][1]["clause_type"], "保密义务")
        self.assertIn("接收方", payload["clauses"][1]["key_fields"]["obligation_subject"])
        self.assertIn("3年", payload["clauses"][1]["key_fields"]["confidentiality_period"])
        self.assertEqual(
            [log["tool_name"] for log in payload["logs"][:2]],
            ["parse_document", "extract_clauses"],
        )
        self.assertEqual(payload["matched_rules"][0]["matched_rules"][0]["rule_id"], "NDA-R001")
        self.assertEqual(payload["review_contexts"][0]["matched_rule"]["rule_id"], "NDA-R001")
        self.assertFalse(payload["review_contexts"][0]["formal_risk_generated"])
        self.assertEqual(payload["risk_findings"][0]["risk_type"], "保密信息范围过宽")
        self.assertIn("商业信息", payload["risk_findings"][0]["evidence_text"])

    def test_pdf_upload_extracts_readable_text(self):
        task = create_review_task(
            file_name="sample.pdf",
            content=build_pdf_bytes(),
            review_position_value="乙方",
        )

        payload = task.to_dict()
        self.assertEqual(task.status, ReviewStatus.EVIDENCE_VERIFIED)
        self.assertEqual(payload["file_type"], "pdf")
        self.assertIn("Confidential Information", payload["document"]["paragraphs"][0]["text"])
        self.assertEqual(len(payload["clauses"]), 2)
        self.assertEqual(payload["clauses"][1]["clause_type"], "期限")
        self.assertEqual(payload["matched_rules"][1]["matched_rules"][0]["rule_id"], "NDA-R003")
        self.assertTrue(payload["review_contexts"])
        self.assertTrue(payload["analysis_results"])
        self.assertIn("stream_order", payload["document"]["paragraphs"][0]["source_location"])
        self.assertNotIn("page", payload["document"]["paragraphs"][0]["source_location"])

    def test_confidentiality_obligation_is_not_misclassified_as_definition(self):
        task = create_review_task(
            file_name="sample.docx",
            content=build_confidentiality_docx_bytes(),
            review_position_value="甲方",
        )

        payload = task.to_dict()
        self.assertEqual(payload["clauses"][0]["clause_type"], "保密义务")

    def test_unreadable_docx_returns_parse_failed(self):
        task = create_review_task(
            file_name="broken.docx",
            content=b"not a docx",
            review_position_value="甲方",
        )

        payload = task.to_dict()
        self.assertEqual(task.status, ReviewStatus.PARSE_FAILED)
        self.assertEqual(payload["logs"][0]["tool_name"], "parse_document")
        self.assertEqual(payload["logs"][0]["status"], "failed")
        self.assertIsNone(payload["document"])
        self.assertIsNone(payload["clauses"])

    def test_rejects_unsupported_file_type(self):
        with self.assertRaises(ValueError):
            create_review_task(
                file_name="sample.exe",
                content=b"hello",
                review_position_value="甲方",
            )

    def test_task_two_tools_are_registered(self):
        self.assertIn("parse_document", tool_registry)
        self.assertIn("extract_clauses", tool_registry)
        self.assertIn("extract_key_fields", tool_registry)
        self.assertIn("retrieve_playbook_rules", tool_registry)
        self.assertIn("retrieve_related_clauses", tool_registry)
        self.assertIn("retrieve_memory", tool_registry)
        self.assertIn("analyze_risk", tool_registry)
        self.assertIn("verify_evidence", tool_registry)
        self.assertIn("generate_revision", tool_registry)


def build_docx_bytes() -> bytes:
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>1. 定义</w:t></w:r></w:p>
    <w:p><w:r><w:t>保密信息是指披露方提供的商业信息。</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>披露方</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>甲方</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p><w:r><w:t>2. 保密义务</w:t></w:r></w:p>
    <w:p><w:r><w:t>接收方应仅用于评估合作目的，并保密3年。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def build_confidentiality_docx_bytes() -> bytes:
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>1. 保密义务</w:t></w:r></w:p>
    <w:p><w:r><w:t>接收方不得披露保密信息。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def build_high_risk_docx_bytes() -> bytes:
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>1. 违约责任</w:t></w:r></w:p>
    <w:p><w:r><w:t>违约方应赔偿守约方全部损失、间接损失，且责任不限。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def build_pdf_bytes() -> bytes:
    stream = (
        b"BT /F1 12 Tf 72 720 Td "
        b"(1. Definitions Confidential Information means business information.) Tj "
        b"(2. Term The confidentiality period is 3 years.) Tj ET"
    )
    return b"\n".join(
        [
            b"%PDF-1.4",
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj",
            b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj",
            b"4 0 obj << /Length " + str(len(stream)).encode("ascii") + b" >>",
            b"stream",
            stream,
            b"endstream",
            b"endobj",
            b"%%EOF",
        ]
    )


if __name__ == "__main__":
    unittest.main()
