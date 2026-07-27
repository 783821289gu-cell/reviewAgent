from models.contract import ContractDocument
from parsers.docling_parser import DoclingParseError, parse_pdf_with_docling


class PdfParseError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def parse_pdf(file_name: str, content: bytes) -> ContractDocument:
    try:
        return parse_pdf_with_docling(file_name=file_name, content=content)
    except DoclingParseError as exc:
        raise PdfParseError(exc.reason_code, str(exc)) from exc
