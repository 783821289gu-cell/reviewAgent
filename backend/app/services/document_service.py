from parsers.docx_parser import parse_docx
from parsers.pdf_parser import parse_pdf


SUPPORTED_FILE_TYPES = {"docx", "pdf"}


def parse_document(tool_input: dict):
    file_name = str(tool_input.get("file_name", "")).strip()
    file_type = str(tool_input.get("file_type", "")).strip().lower()
    content = tool_input.get("content", b"")

    if file_type not in SUPPORTED_FILE_TYPES:
        raise ValueError("仅支持上传 .docx 或 .pdf 文件。")
    if not isinstance(content, bytes) or not content:
        raise ValueError("上传文件为空，无法解析。")

    if file_type == "docx":
        return parse_docx(file_name=file_name, content=content)
    if file_type == "pdf":
        return parse_pdf(file_name=file_name, content=content)

    raise ValueError("仅支持上传 .docx 或 .pdf 文件。")
