from pathlib import Path
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_SAMPLES = PROJECT_ROOT / "samples" / "pdf"
PDF_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pdf"
sys.path.insert(0, str(APP_DIR))

from parsers.pdf_parser import PdfParseError, parse_pdf


class PdfParserTest(unittest.TestCase):
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

    def test_scanned_pdf_requires_ocr(self):
        self._assert_parse_error("scanned_image.pdf", "ocr_required", "需要 OCR")

    def test_empty_text_pdf_requires_effective_text_or_ocr(self):
        self._assert_parse_error("empty_text.pdf", "no_effective_text", "需要 OCR")

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


if __name__ == "__main__":
    unittest.main()
