from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from threading import Lock

from config import settings
from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import DocumentStream
from docling.datamodel.pipeline_options import (
    LayoutOptions,
    PdfPipelineOptions,
    RapidOcrOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption
from docling_core.types.doc import TableItem, TextItem
import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect, PDFTextExtractionNotAllowed
from pdfminer.pdffont import PDFUnicodeNotDefined
from pdfplumber.utils.exceptions import PdfminerException

from models.contract import ContractDocument, TextBlock


MIN_EFFECTIVE_TEXT_CHARS = 10
CID_PLACEHOLDER_PATTERN = re.compile(r"\(cid:\d+\)", re.I)
EFFECTIVE_CHARACTER_PATTERN = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
REQUIRED_DOCLING_MODEL_PATHS = (
    Path("docling-project--docling-layout-heron/config.json"),
    Path("docling-project--docling-layout-heron/model.safetensors"),
    Path(
        "docling-project--docling-models/model_artifacts/tableformer/"
        "accurate/tm_config.json"
    ),
    Path(
        "docling-project--docling-models/model_artifacts/tableformer/"
        "accurate/tableformer_accurate.safetensors"
    ),
    Path("RapidOcr/torch/PP-OCRv4/det/ch_PP-OCRv4_det_mobile.pth"),
    Path("RapidOcr/torch/PP-OCRv4/cls/ch_ptocr_mobile_v2.0_cls_mobile.pth"),
    Path("RapidOcr/torch/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile.pth"),
    Path(
        "RapidOcr/paddle/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile/"
        "ppocr_keys_v1.txt"
    ),
)

_PDF_CONVERSION_LOCK = Lock()
_DOCX_CONVERSION_LOCK = Lock()


class DoclingParseError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def parse_pdf_with_docling(file_name: str, content: bytes) -> ContractDocument:
    _validate_pdf(content)
    try:
        with _PDF_CONVERSION_LOCK:
            result = _pdf_converter().convert(
                DocumentStream(name=file_name, stream=BytesIO(content)),
                raises_on_error=True,
            )
    except DoclingParseError:
        raise
    except Exception as exc:
        raise _pdf_conversion_error(exc) from exc

    return _to_contract_document(
        file_name=file_name,
        file_type="pdf",
        content=content,
        document=result.document,
    )


def parse_docx_with_docling(file_name: str, content: bytes) -> ContractDocument:
    try:
        with _DOCX_CONVERSION_LOCK:
            result = _docx_converter().convert(
                DocumentStream(name=file_name, stream=BytesIO(content)),
                raises_on_error=True,
            )
    except Exception as exc:
        raise DoclingParseError(
            "invalid_docx",
            "DOCX 文件无法读取或结构无效，请确认文件格式正确。",
        ) from exc

    return _to_contract_document(
        file_name=file_name,
        file_type="docx",
        content=content,
        document=result.document,
    )


@lru_cache(maxsize=1)
def _pdf_converter() -> DocumentConverter:
    artifacts_path = _validated_docling_artifacts_path(
        settings.docling_artifacts_path,
    )
    layout_options = LayoutOptions()
    layout_options.model_spec = layout_options.model_spec.model_copy(
        update={"revision": settings.docling_layout_revision},
    )
    options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        do_code_enrichment=False,
        do_formula_enrichment=False,
        do_picture_classification=False,
        do_picture_description=False,
        do_chart_extraction=False,
        allow_external_plugins=False,
        enable_remote_services=False,
        document_timeout=settings.docling_document_timeout_seconds,
        accelerator_options=AcceleratorOptions(device=settings.docling_device),
        ocr_options=RapidOcrOptions(
            backend="torch",
            lang=["chinese"],
            force_full_page_ocr=False,
        ),
        layout_options=layout_options,
    )
    options.artifacts_path = artifacts_path

    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        },
    )


def _validated_docling_artifacts_path(configured_path: str) -> Path:
    artifacts_path = Path(configured_path) if configured_path else None
    if artifacts_path is None:
        raise DoclingParseError(
            "models_unavailable",
            "Docling 解析模型尚未准备，请先运行模型准备命令。",
        )
    try:
        if not artifacts_path.is_dir():
            raise DoclingParseError(
                "models_unavailable",
                "Docling 解析模型尚未准备，请先运行模型准备命令。",
            )
        is_incomplete = any(
            not (artifacts_path / relative_path).is_file()
            or (artifacts_path / relative_path).stat().st_size == 0
            for relative_path in REQUIRED_DOCLING_MODEL_PATHS
        )
    except OSError as exc:
        raise DoclingParseError(
            "models_unavailable",
            "Docling 解析模型无法读取，请检查模型目录权限。",
        ) from exc
    if is_incomplete:
        raise DoclingParseError(
            "models_unavailable",
            "Docling 解析模型不完整，请重新运行模型准备命令。",
        )
    return artifacts_path


@lru_cache(maxsize=1)
def _docx_converter() -> DocumentConverter:
    return DocumentConverter(
        allowed_formats=[InputFormat.DOCX],
        format_options={
            InputFormat.DOCX: WordFormatOption(),
        },
    )


def _to_contract_document(
    *,
    file_name: str,
    file_type: str,
    content: bytes,
    document,
) -> ContractDocument:
    blocks: list[TextBlock] = []
    page_block_counts: dict[int, int] = {}

    for item, _level in document.iterate_items():
        if isinstance(item, TableItem):
            block_type = "table"
            text = _table_text(item)
        elif isinstance(item, TextItem):
            block_type = "paragraph"
            text = _text_item_text(item)
        else:
            continue
        if not text:
            continue

        order = len(blocks) + 1
        block_id = f"B{order:04d}"
        source_location = {
            "block_id": block_id,
            "block_type": block_type,
            "order": order,
            "docling_label": _label_value(item.label),
        }
        provenance = item.prov[0] if item.prov else None
        if provenance is not None:
            page_number = int(provenance.page_no)
            page_block_counts[page_number] = page_block_counts.get(page_number, 0) + 1
            source_location.update(
                {
                    "page_number": page_number,
                    "page_block_index": page_block_counts[page_number],
                    "bbox": _top_left_bbox(document, provenance),
                }
            )

        blocks.append(
            TextBlock(
                block_id=block_id,
                block_type=block_type,
                text=text,
                order=order,
                source_location=source_location,
            )
        )

    extracted_text = "\n".join(block.text for block in blocks)
    if file_type == "pdf" and _font_mapping_failed(extracted_text):
        raise DoclingParseError(
            "font_mapping_failed",
            "PDF 使用了无法可靠映射为 Unicode 的复杂字体，OCR 也未能恢复正文。",
        )
    if _effective_character_count(extracted_text) < MIN_EFFECTIVE_TEXT_CHARS:
        if file_type == "pdf" and _pdf_has_unmappable_font(content):
            raise DoclingParseError(
                "font_mapping_failed",
                "PDF 使用了无法可靠映射为 Unicode 的复杂字体，OCR 也未能恢复正文。",
            )
        raise DoclingParseError(
            "no_effective_text",
            f"{file_type.upper()} 未解析出可读正文；如为扫描件，请确认图像清晰且 OCR 可识别。",
        )

    paragraphs = [block for block in blocks if block.block_type == "paragraph"]
    tables = [block for block in blocks if block.block_type == "table"]
    content_hash = sha256(content).hexdigest()
    return ContractDocument(
        contract_id=f"contract_{content_hash[:12]}",
        file_name=file_name,
        file_type=file_type,
        content_hash=content_hash,
        paragraphs=paragraphs,
        tables=tables,
        blocks=blocks,
        page_map=[
            {
                "block_id": block.block_id,
                "order": block.order,
                **(
                    {
                        "page_number": block.source_location["page_number"],
                        "bbox": block.source_location["bbox"],
                    }
                    if "page_number" in block.source_location
                    else {}
                ),
                "location": block.source_location,
            }
            for block in blocks
        ],
    )


def _validate_pdf(content: bytes) -> None:
    if not content.lstrip().startswith(b"%PDF-"):
        raise DoclingParseError(
            "invalid_signature",
            "PDF 文件签名无效，请确认文件格式正确。",
        )
    try:
        with pdfplumber.open(BytesIO(content)):
            pass
    except (PDFPasswordIncorrect, PDFTextExtractionNotAllowed) as exc:
        raise DoclingParseError(
            "encrypted",
            "PDF 已加密或受密码保护，当前不支持解密，请上传未加密文件。",
        ) from exc
    except PdfminerException as exc:
        wrapped_error = exc.args[0] if exc.args else None
        if isinstance(wrapped_error, (PDFPasswordIncorrect, PDFTextExtractionNotAllowed)):
            raise DoclingParseError(
                "encrypted",
                "PDF 已加密或受密码保护，当前不支持解密，请上传未加密文件。",
            ) from exc
        raise DoclingParseError(
            "corrupted",
            "PDF 文件损坏或结构不完整，无法解析。",
        ) from exc
    except (OSError, ValueError, TypeError, IndexError, KeyError) as exc:
        raise DoclingParseError(
            "corrupted",
            "PDF 文件损坏或结构不完整，无法解析。",
        ) from exc


def _pdf_conversion_error(exc: Exception) -> DoclingParseError:
    message = f"{exc.__class__.__name__}: {exc}".lower()
    if any(token in message for token in ("password", "encrypted", "decrypt")):
        return DoclingParseError(
            "encrypted",
            "PDF 已加密或受密码保护，当前不支持解密，请上传未加密文件。",
        )
    if any(token in message for token in ("artifact", "model", "checkpoint")):
        return DoclingParseError(
            "models_unavailable",
            "Docling 解析模型不可用，请检查模型准备状态和配置路径。",
        )
    if "timeout" in message:
        return DoclingParseError(
            "parse_timeout",
            "PDF 解析超过配置时限，请检查文档规模或提高 Docling 解析时限。",
        )
    return DoclingParseError(
        "corrupted",
        "PDF 解析器无法读取该文件，文件可能已损坏或使用了不支持的结构。",
    )


def _table_text(item: TableItem) -> str:
    rows: list[str] = []
    for row in item.data.grid:
        cells = [_normalize_text(cell.text) for cell in row]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _text_item_text(item: TextItem) -> str:
    text = _normalize_text(item.text)
    marker = _normalize_text(getattr(item, "marker", ""))
    if getattr(item, "enumerated", False) and marker and not text.startswith(marker):
        return f"{marker} {text}"
    return text


def _top_left_bbox(document, provenance) -> list[float]:
    bbox = provenance.bbox
    page = document.pages.get(int(provenance.page_no))
    if page is not None:
        bbox = bbox.to_top_left_origin(float(page.size.height))
    return [
        round(float(bbox.l), 2),
        round(float(bbox.t), 2),
        round(float(bbox.r), 2),
        round(float(bbox.b), 2),
    ]


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _label_value(label) -> str:
    return str(getattr(label, "value", label))


def _effective_character_count(text: str) -> int:
    return len(EFFECTIVE_CHARACTER_PATTERN.findall(text or ""))


def _font_mapping_failed(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return False
    placeholder_length = sum(
        len(match.group(0))
        for match in CID_PLACEHOLDER_PATTERN.finditer(compact)
    )
    invalid_count = placeholder_length + compact.count("\ufffd")
    invalid_count += sum(1 for character in compact if "\ue000" <= character <= "\uf8ff")
    invalid_count += compact.count("?") if compact.count("?") >= 5 else 0
    return invalid_count / len(compact) >= 0.2


def _pdf_has_unmappable_font(content: bytes) -> bool:
    diagnostic_text: list[str] = []
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            for page in pdf.pages:
                diagnostic_text.append(page.extract_text() or "")
                diagnostic_text.append(
                    "".join(str(character.get("text", "")) for character in page.chars)
                )
    except PDFUnicodeNotDefined:
        return True
    except (PdfminerException, OSError, ValueError, TypeError, IndexError, KeyError):
        return False
    return _font_mapping_failed("\n".join(diagnostic_text))
