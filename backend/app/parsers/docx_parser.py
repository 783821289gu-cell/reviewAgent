from models.contract import ContractDocument
from parsers.docling_parser import DoclingParseError, parse_docx_with_docling


def parse_docx(file_name: str, content: bytes) -> ContractDocument:
    try:
        return parse_docx_with_docling(file_name=file_name, content=content)
    except DoclingParseError as exc:
        raise ValueError(str(exc)) from exc
