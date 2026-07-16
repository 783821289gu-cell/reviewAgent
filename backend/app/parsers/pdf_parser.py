from hashlib import sha256
from io import BytesIO
import re

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException
from pdfminer.pdfdocument import PDFPasswordIncorrect, PDFTextExtractionNotAllowed
from pdfminer.pdfexceptions import PDFException
from pdfminer.pdffont import PDFUnicodeNotDefined
from pdfminer.psparser import PSEOF

from models.contract import ContractDocument, TextBlock


MIN_EFFECTIVE_TEXT_CHARS = 10
CID_PLACEHOLDER_PATTERN = re.compile(r"\(cid:\d+\)", re.I)
EFFECTIVE_CHARACTER_PATTERN = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")


class PdfParseError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def parse_pdf(file_name: str, content: bytes) -> ContractDocument:
    if not content.lstrip().startswith(b"%PDF-"):
        raise PdfParseError(
            "invalid_signature",
            "PDF 文件签名无效，请确认文件格式正确。",
        )

    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            blocks, image_count, character_text = _extract_blocks(pdf.pages)
    except PdfParseError:
        raise
    except (PDFPasswordIncorrect, PDFTextExtractionNotAllowed) as exc:
        raise PdfParseError(
            "encrypted",
            "PDF 已加密或受密码保护，当前不支持解密，请上传未加密文本型 PDF。",
        ) from exc
    except PdfminerException as exc:
        wrapped_error = exc.args[0] if exc.args else None
        if isinstance(wrapped_error, (PDFPasswordIncorrect, PDFTextExtractionNotAllowed)):
            raise PdfParseError(
                "encrypted",
                "PDF 已加密或受密码保护，当前不支持解密，请上传未加密文本型 PDF。",
            ) from exc
        if isinstance(wrapped_error, PDFUnicodeNotDefined) or _font_error(exc):
            raise PdfParseError(
                "font_mapping_failed",
                "PDF 使用了无法映射为 Unicode 的复杂字体，无法可靠提取正文。",
            ) from exc
        raise PdfParseError(
            "corrupted",
            "PDF 文件损坏或结构不完整，无法解析。",
        ) from exc
    except PDFUnicodeNotDefined as exc:
        raise PdfParseError(
            "font_mapping_failed",
            "PDF 使用了无法映射为 Unicode 的复杂字体，无法可靠提取正文。",
        ) from exc
    except (PDFException, PSEOF, OSError, ValueError, TypeError, IndexError, KeyError) as exc:
        raise PdfParseError(
            "corrupted",
            "PDF 文件损坏或结构不完整，无法解析。",
        ) from exc
    except Exception as exc:
        raise PdfParseError(
            "corrupted",
            "PDF 解析器无法读取该文件，文件可能已损坏或使用了不支持的结构。",
        ) from exc

    extracted_text = "\n".join(block.text for block in blocks)
    if _font_mapping_failed(extracted_text) or _font_mapping_failed(character_text):
        raise PdfParseError(
            "font_mapping_failed",
            "PDF 使用了无法映射为 Unicode 的复杂字体，无法可靠提取正文。",
        )
    if _effective_character_count(extracted_text) < MIN_EFFECTIVE_TEXT_CHARS:
        if image_count > 0:
            raise PdfParseError(
                "ocr_required",
                "PDF 是扫描件，当前未启用 OCR，需要 OCR 后再上传文本型 PDF。",
            )
        raise PdfParseError(
            "no_effective_text",
            "PDF 未包含可提取的有效文本；如为扫描件则需要 OCR，当前版本未启用 OCR。",
        )

    content_hash = sha256(content).hexdigest()
    return ContractDocument(
        contract_id=f"contract_{content_hash[:12]}",
        file_name=file_name,
        file_type="pdf",
        content_hash=content_hash,
        paragraphs=blocks,
        tables=[],
        blocks=blocks,
        page_map=[
            {
                "block_id": block.block_id,
                "order": block.order,
                "page_number": block.source_location["page_number"],
                "bbox": block.source_location["bbox"],
                "location": block.source_location,
            }
            for block in blocks
        ],
    )


def _extract_blocks(pages) -> tuple[list[TextBlock], int, str]:
    blocks: list[TextBlock] = []
    image_count = 0
    character_parts: list[str] = []
    for page_number, page in enumerate(pages, start=1):
        image_count += len(page.images or [])
        character_parts.extend(
            str(item.get("text", ""))
            for item in (page.chars or [])
            if isinstance(item, dict)
        )
        try:
            words = page.dedupe_chars().extract_words(
                x_tolerance=3,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
        except PDFUnicodeNotDefined:
            raise
        except Exception as exc:
            if _font_error(exc):
                raise PdfParseError(
                    "font_mapping_failed",
                    "PDF 使用了无法映射为 Unicode 的复杂字体，无法可靠提取正文。",
                ) from exc
            raise

        for line_index, line_words in enumerate(_group_words_into_lines(words), start=1):
            text = _line_text(line_words)
            if not text:
                continue
            order = len(blocks) + 1
            block_id = f"B{order:04d}"
            bbox = _line_bbox(line_words)
            blocks.append(
                TextBlock(
                    block_id=block_id,
                    block_type="paragraph",
                    text=text,
                    order=order,
                    source_location={
                        "block_id": block_id,
                        "block_type": "paragraph",
                        "order": order,
                        "page_number": page_number,
                        "page_block_index": line_index,
                        "bbox": bbox,
                    },
                )
            )
    return blocks, image_count, "".join(character_parts)


def _group_words_into_lines(words: list[dict]) -> list[list[dict]]:
    sorted_words = sorted(
        (word for word in words if str(word.get("text", "")).strip()),
        key=lambda word: (float(word.get("top", 0)), float(word.get("x0", 0))),
    )
    lines: list[list[dict]] = []
    for word in sorted_words:
        top = float(word.get("top", 0))
        if not lines or abs(top - _line_top(lines[-1])) > 3:
            lines.append([word])
        else:
            lines[-1].append(word)
    for line in lines:
        line.sort(key=lambda word: float(word.get("x0", 0)))
    return lines


def _line_top(words: list[dict]) -> float:
    return sum(float(word.get("top", 0)) for word in words) / len(words)


def _line_text(words: list[dict]) -> str:
    return " ".join(str(word.get("text", "")).strip() for word in words).strip()


def _line_bbox(words: list[dict]) -> list[float]:
    return [
        round(min(float(word.get("x0", 0)) for word in words), 2),
        round(min(float(word.get("top", 0)) for word in words), 2),
        round(max(float(word.get("x1", 0)) for word in words), 2),
        round(max(float(word.get("bottom", 0)) for word in words), 2),
    ]


def _effective_character_count(text: str) -> int:
    return len(EFFECTIVE_CHARACTER_PATTERN.findall(text or ""))


def _font_mapping_failed(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return False
    placeholder_length = sum(len(match.group(0)) for match in CID_PLACEHOLDER_PATTERN.finditer(compact))
    invalid_count = placeholder_length + compact.count("\ufffd")
    invalid_count += sum(1 for character in compact if "\ue000" <= character <= "\uf8ff")
    invalid_count += compact.count("?") if compact.count("?") >= 5 else 0
    return invalid_count / len(compact) >= 0.2


def _font_error(exc: Exception) -> bool:
    message = f"{exc.__class__.__name__}: {exc}".lower()
    return any(token in message for token in ("unicode", "font", "cmap", "cid"))
