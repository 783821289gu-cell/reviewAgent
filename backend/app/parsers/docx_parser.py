from hashlib import sha256
from io import BytesIO
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from models.contract import ContractDocument, TextBlock


WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parse_docx(file_name: str, content: bytes) -> ContractDocument:
    try:
        with ZipFile(BytesIO(content)) as archive:
            document_xml = archive.read("word/document.xml")
    except (BadZipFile, KeyError) as exc:
        raise ValueError("DOCX 文件无法读取，请确认文件格式正确。") from exc

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as exc:
        raise ValueError("DOCX 文件结构无效，无法解析正文。") from exc

    body = root.find(f"{WORD_NAMESPACE}body")
    if body is None:
        raise ValueError("DOCX 文件缺少正文内容。")

    paragraphs: list[TextBlock] = []
    tables: list[TextBlock] = []
    blocks: list[TextBlock] = []

    for element in body:
        if element.tag == f"{WORD_NAMESPACE}p":
            text = _extract_text(element)
            if text:
                blocks.append(
                    _new_block("paragraph", text, len(blocks) + 1)
                )
        elif element.tag == f"{WORD_NAMESPACE}tbl":
            text = _extract_table_text(element)
            if text:
                blocks.append(
                    _new_block("table", text, len(blocks) + 1)
                )

    if not blocks:
        raise ValueError("DOCX 文件未解析出可读正文。")

    for block in blocks:
        if block.block_type == "paragraph":
            paragraphs.append(block)
        elif block.block_type == "table":
            tables.append(block)

    content_hash = sha256(content).hexdigest()
    return ContractDocument(
        contract_id=f"contract_{content_hash[:12]}",
        file_name=file_name,
        file_type="docx",
        content_hash=content_hash,
        paragraphs=paragraphs,
        tables=tables,
        blocks=blocks,
        page_map=[
            {
                "block_id": block.block_id,
                "order": block.order,
                "location": block.source_location,
            }
            for block in blocks
        ],
    )


def _new_block(block_type: str, text: str, order: int) -> TextBlock:
    return TextBlock(
        block_id=f"B{order:04d}",
        block_type=block_type,
        text=text,
        order=order,
        source_location={
            "block_id": f"B{order:04d}",
            "block_type": block_type,
            "order": order,
        },
    )


def _extract_text(element: ET.Element) -> str:
    parts = [
        node.text or ""
        for node in element.iter(f"{WORD_NAMESPACE}t")
    ]
    return " ".join("".join(parts).split())


def _extract_table_text(table: ET.Element) -> str:
    rows: list[str] = []
    for row in table.iter(f"{WORD_NAMESPACE}tr"):
        cells: list[str] = []
        for cell in row.iter(f"{WORD_NAMESPACE}tc"):
            cell_text = _extract_text(cell)
            if cell_text:
                cells.append(cell_text)
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows).strip()
