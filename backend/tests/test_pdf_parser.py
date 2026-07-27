from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from docling.datamodel.base_models import InputFormat

APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_SAMPLES = PROJECT_ROOT / "samples" / "pdf"
PDF_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pdf"
sys.path.insert(0, str(APP_DIR))

from parsers.pdf_parser import PdfParseError, parse_pdf
from parsers.docling_parser import (
    DoclingParseError,
    REQUIRED_DOCLING_MODEL_PATHS,
    _pdf_converter,
    _validated_docling_artifacts_path,
)


class PdfParserTest(unittest.TestCase):
    def test_standard_pipeline_enables_ocr_and_tables_without_vlm(self):
        options = _pdf_converter().format_to_options[
            InputFormat.PDF
        ].pipeline_options

        self.assertEqual(
            _pdf_converter().format_to_options[InputFormat.PDF].pipeline_cls.__name__,
            "StandardPdfPipeline",
        )
        self.assertTrue(options.do_ocr)
        self.assertTrue(options.do_table_structure)
        self.assertFalse(options.ocr_options.force_full_page_ocr)
        self.assertEqual(options.ocr_options.backend, "torch")
        self.assertEqual(options.ocr_options.lang, ["chinese"])
        self.assertFalse(options.do_picture_description)
        self.assertFalse(options.do_chart_extraction)
        self.assertEqual(options.table_structure_options.mode.value, "accurate")
        self.assertEqual(
            options.layout_options.model_spec.revision,
            "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
        )

    def test_model_artifacts_must_be_complete(self):
        with TemporaryDirectory() as temp_dir:
            with self.assertRaises(DoclingParseError) as raised:
                _validated_docling_artifacts_path(temp_dir)

            self.assertEqual(raised.exception.reason_code, "models_unavailable")
            self.assertIn("不完整", str(raised.exception))

            root = Path(temp_dir)
            for relative_path in REQUIRED_DOCLING_MODEL_PATHS:
                model_file = root / relative_path
                model_file.parent.mkdir(parents=True, exist_ok=True)
                model_file.write_bytes(b"model")

            self.assertEqual(
                _validated_docling_artifacts_path(temp_dir),
                root,
            )

    def test_unreadable_model_directory_has_dependency_error(self):
        with patch.object(Path, "is_dir", side_effect=OSError("access denied")):
            with self.assertRaises(DoclingParseError) as raised:
                _validated_docling_artifacts_path("models")

        self.assertEqual(raised.exception.reason_code, "models_unavailable")
        self.assertIn("无法读取", str(raised.exception))

    def test_english_text_pdf_extracts_blocks_pages_and_coordinates(self):
        document = parse_pdf("nda_text_en.pdf", (PDF_SAMPLES / "nda_text_en.pdf").read_bytes())

        self.assertEqual(document.file_type, "pdf")
        self.assertEqual(len(document.blocks), 4)
        self.assertEqual(document.paragraphs, document.blocks)
        self.assertEqual([block.source_location["page_number"] for block in document.blocks], [1, 1, 2, 2])
        self.assertIn("Confidential Information", document.blocks[1].text)
        self.assertEqual(document.page_map[0]["block_id"], document.blocks[0].block_id)
        self.assertEqual(document.page_map[0]["bbox"], document.blocks[0].source_location["bbox"])
        self.assertTrue(all(len(block.source_location["bbox"]) == 4 for block in document.blocks))

    def test_chinese_text_pdf_extracts_unicode_text_and_pages(self):
        document = parse_pdf("nda_text_zh.pdf", (PDF_SAMPLES / "nda_text_zh.pdf").read_bytes())

        text = "\n".join(block.text for block in document.blocks)
        self.assertEqual(len(document.blocks), 4)
        self.assertEqual(document.paragraphs, document.blocks)
        self.assertIn("保密协议", text)
        self.assertIn("保密信息", text)
        self.assertEqual([block.source_location["page_number"] for block in document.blocks], [1, 1, 2, 2])

    def test_scanned_pdf_is_parsed_with_on_demand_ocr(self):
        document = parse_pdf(
            "scanned_image.pdf",
            (PDF_FIXTURES / "scanned_image.pdf").read_bytes(),
        )

        self.assertEqual(len(document.blocks), 1)
        self.assertEqual(
            document.blocks[0].text,
            "SCANNED NDA IMAGE - NO PDF TEXT LAYER",
        )
        self.assertEqual(document.blocks[0].source_location["page_number"], 1)
        self.assertEqual(len(document.blocks[0].source_location["bbox"]), 4)

    def test_pdf_table_is_mapped_to_table_block(self):
        document = parse_pdf("table.pdf", _build_table_pdf())

        self.assertEqual(len(document.tables), 1)
        self.assertEqual(document.tables, document.blocks)
        self.assertEqual(
            document.tables[0].text,
            "Field | Value\nTerm | Three years",
        )
        self.assertEqual(document.tables[0].source_location["page_number"], 1)
        self.assertEqual(len(document.tables[0].source_location["bbox"]), 4)

    def test_empty_text_pdf_requires_effective_text_or_ocr(self):
        self._assert_parse_error("empty_text.pdf", "no_effective_text", "OCR")

    def test_encrypted_pdf_has_specific_error(self):
        self._assert_parse_error("encrypted.pdf", "encrypted", "加密")

    def test_complex_font_mapping_has_specific_error(self):
        self._assert_parse_error("complex_font.pdf", "font_mapping_failed", "复杂字体")

    def test_damaged_pdf_has_specific_error(self):
        self._assert_parse_error("damaged.pdf", "corrupted", "损坏")

    def _assert_parse_error(self, fixture_name: str, reason_code: str, message_fragment: str):
        with self.assertRaises(PdfParseError) as raised:
            parse_pdf(fixture_name, (PDF_FIXTURES / fixture_name).read_bytes())

        self.assertEqual(raised.exception.reason_code, reason_code)
        self.assertIn(message_fragment, str(raised.exception))


def _build_table_pdf() -> bytes:
    stream = b"""0.8 w
72 730 m 540 730 l S
72 700 m 540 700 l S
72 670 m 540 670 l S
72 670 m 72 730 l S
300 670 m 300 730 l S
540 670 m 540 730 l S
BT /F1 12 Tf 82 710 Td (Field) Tj ET
BT /F1 12 Tf 310 710 Td (Value) Tj ET
BT /F1 12 Tf 82 680 Td (Term) Tj ET
BT /F1 12 Tf 310 680 Td (Three years) Tj ET
"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{object_number} 0 obj\n".encode())
        content.extend(body)
        content.extend(b"\nendobj\n")
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(content)


if __name__ == "__main__":
    unittest.main()
