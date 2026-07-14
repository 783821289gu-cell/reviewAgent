from hashlib import sha256
import re
import zlib

from models.contract import ContractDocument, TextBlock


STREAM_PATTERN = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
STRING_PATTERN = re.compile(rb"\((?:\\.|[^\\)])*\)\s*Tj|\[(.*?)\]\s*TJ", re.S)
ARRAY_STRING_PATTERN = re.compile(rb"\((?:\\.|[^\\)])*\)|<([0-9A-Fa-f\s]+)>")


def parse_pdf(file_name: str, content: bytes) -> ContractDocument:
    if not content.lstrip().startswith(b"%PDF"):
        raise ValueError("PDF 文件无法读取，请确认文件格式正确。")

    texts: list[str] = []
    for stream_match in STREAM_PATTERN.finditer(content):
        object_start = content.rfind(b"obj", 0, stream_match.start())
        object_header = content[object_start:stream_match.start()] if object_start != -1 else b""
        stream_content = stream_match.group(1)

        if b"FlateDecode" in object_header:
            try:
                stream_content = zlib.decompress(stream_content)
            except zlib.error:
                continue

        stream_text = _extract_text_from_stream(stream_content)
        if stream_text:
            texts.append(stream_text)

    if not texts:
        fallback_text = _extract_loose_pdf_text(content)
        if fallback_text:
            texts.append(fallback_text)

    if not texts:
        raise ValueError("PDF 文件未解析出可读文本。")

    blocks: list[TextBlock] = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            TextBlock(
                block_id=f"B{index:04d}",
                block_type="paragraph",
                text=text,
                order=index,
                source_location={
                    "block_id": f"B{index:04d}",
                    "block_type": "paragraph",
                    "order": index,
                    "stream_order": index,
                },
            )
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
                "location": block.source_location,
            }
            for block in blocks
        ],
    )


def _extract_text_from_stream(stream_content: bytes) -> str:
    chunks: list[str] = []
    for match in STRING_PATTERN.finditer(stream_content):
        token = match.group(0)
        if token.endswith(b"Tj"):
            chunks.append(_decode_pdf_literal(token[:-2].strip()))
        else:
            array_body = match.group(1) or b""
            parts: list[str] = []
            for item in ARRAY_STRING_PATTERN.finditer(array_body):
                literal = item.group(0)
                hex_body = item.group(1)
                if hex_body is not None:
                    parts.append(_decode_pdf_hex(hex_body))
                elif literal.startswith(b"("):
                    parts.append(_decode_pdf_literal(literal))
            if parts:
                chunks.append("".join(parts))
    return _normalize_text(" ".join(chunks))


def _extract_loose_pdf_text(content: bytes) -> str:
    candidates = [
        _decode_pdf_literal(match.group(0))
        for match in re.finditer(rb"\((?:\\.|[^\\)]){3,}\)", content)
    ]
    return _normalize_text(" ".join(candidates))


def _decode_pdf_literal(value: bytes) -> str:
    if value.startswith(b"(") and value.endswith(b")"):
        value = value[1:-1]
    value = (
        value.replace(rb"\(", b"(")
        .replace(rb"\)", b")")
        .replace(rb"\\", b"\\")
        .replace(rb"\n", b"\n")
        .replace(rb"\r", b"\r")
        .replace(rb"\t", b"\t")
    )
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1", errors="ignore")


def _decode_pdf_hex(value: bytes) -> str:
    compact = re.sub(rb"\s+", b"", value)
    if len(compact) % 2 == 1:
        compact += b"0"
    raw = bytes.fromhex(compact.decode("ascii"))
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="ignore")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="ignore")


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\x00", " ").split())
