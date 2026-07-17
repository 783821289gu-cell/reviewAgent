import re

from models.contract import ContractDocument, ContractTypeClassification
from providers.llm_provider import (
    LLMProviderError,
    llm_call_records_from_tool_input,
    mark_latest_llm_call_schema_error,
)
from services.llm_service import generate_structured_contract_type


NDA_TITLE_PATTERNS = (
    "保密协议",
    "保密及不披露协议",
    "不披露协议",
    "non-disclosure agreement",
    "non disclosure agreement",
    "nondisclosure agreement",
    "mutual nda",
    "confidentiality agreement",
    "confidential disclosure agreement",
)
CONFIDENTIAL_DEFINITION_PATTERNS = (
    "保密信息是指",
    "保密信息包括",
    "confidential information means",
    "confidential information includes",
)
CONFIDENTIAL_OBLIGATION_PATTERNS = (
    "保密义务",
    "应当保密",
    "承担保密",
    "不得披露保密信息",
    "不得向无关人员披露",
    "confidentiality obligation",
    "shall keep confidential",
    "shall not disclose",
    "protect confidential information",
    "confidentiality period",
)
RECEIVING_PARTY_PATTERNS = ("接收方", "接受方", "receiving party", "recipient")
DISCLOSING_PARTY_PATTERNS = ("披露方", "disclosing party", "discloser")
UNSUPPORTED_CONTRACT_PATTERNS = {
    "PROCUREMENT": (
        "采购合同",
        "采购协议",
        "采购订单",
        "购销合同",
        "purchase agreement",
        "purchase order",
        "procurement agreement",
        "supply agreement",
    ),
    "SERVICE": (
        "服务合同",
        "服务协议",
        "service agreement",
        "services agreement",
        "consulting agreement",
    ),
    "EMPLOYMENT": (
        "劳动合同",
        "劳动协议",
        "employment agreement",
        "employment contract",
        "employee agreement",
    ),
}


def classify_contract_type(tool_input: dict) -> dict:
    document = tool_input.get("document")
    texts = _document_texts(document)
    llm_calls = llm_call_records_from_tool_input(tool_input)
    deterministic = _classify_deterministic(texts)
    if deterministic["decision"] != "NEED_MANUAL_REVIEW":
        return deterministic

    last_error = None
    for _attempt in (1, 2):
        try:
            candidate = generate_structured_contract_type(
                texts,
                deterministic,
                llm_calls=llm_calls,
            )
            return ContractTypeClassification(**candidate).to_dict()
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
            break

    fallback = dict(deterministic)
    fallback["evidence"] = [
        *deterministic["evidence"],
        f"模型补充识别失败，保持人工复核：{_error_label(last_error)}",
    ]
    return ContractTypeClassification(**fallback).to_dict()


def _classify_deterministic(texts: list[str]) -> dict:
    normalized = _normalize_text("\n".join(texts))
    title_text = _normalize_text(texts[0])
    has_nda_title = _is_nda_title(title_text)

    unsupported_type, unsupported_evidence = _unsupported_type(title_text)
    if unsupported_type and has_nda_title:
        return ContractTypeClassification(
            contract_type="UNKNOWN",
            confidence=0.45,
            evidence=[
                "标题同时包含 NDA 和其他合同类型标识",
                "合同类型标识冲突，不能自动进入正式审查",
            ],
            decision="NEED_MANUAL_REVIEW",
        ).to_dict()
    if unsupported_type:
        return ContractTypeClassification(
            contract_type=unsupported_type,
            confidence=0.99,
            evidence=[unsupported_evidence],
            decision="UNSUPPORTED_CONTRACT_TYPE",
        ).to_dict()

    evidence = []
    has_definition = _contains_any(normalized, CONFIDENTIAL_DEFINITION_PATTERNS)
    has_obligation = _contains_any(normalized, CONFIDENTIAL_OBLIGATION_PATTERNS)
    has_confidential_terms = "保密信息" in normalized or "confidential information" in normalized
    has_receiving_party = _contains_any(normalized, RECEIVING_PARTY_PATTERNS)
    has_disclosing_party = _contains_any(normalized, DISCLOSING_PARTY_PATTERNS)
    has_party_pair = has_receiving_party and has_disclosing_party

    if has_nda_title:
        evidence.append("文档标题包含 NDA/保密协议标识")
    if has_party_pair:
        evidence.append("正文同时识别到披露方和接收方角色")
    elif has_receiving_party:
        evidence.append("正文识别到接收方角色，但未同时识别披露方")
    elif has_disclosing_party:
        evidence.append("正文识别到披露方角色，但未同时识别接收方")
    if has_definition:
        evidence.append("正文包含保密信息定义结构")
    elif has_confidential_terms:
        evidence.append("正文包含保密信息术语")
    if has_obligation:
        evidence.append("正文包含保密义务或禁止披露结构")

    supported = (
        (has_nda_title and (has_party_pair or has_definition or has_obligation or has_confidential_terms))
        or (has_party_pair and (has_definition or has_obligation))
    )
    if supported:
        confidence = 0.98 if len(evidence) >= 3 else 0.88
        return ContractTypeClassification(
            contract_type="NDA",
            confidence=confidence,
            evidence=evidence,
            decision="SUPPORTED",
        ).to_dict()

    if not evidence:
        evidence.append("未识别到明确合同类型或完整 NDA 结构")
    else:
        evidence.append("现有特征不足以确定为 NDA")
    return ContractTypeClassification(
        contract_type="UNKNOWN",
        confidence=0.55 if len(evidence) > 1 else 0.2,
        evidence=evidence,
        decision="NEED_MANUAL_REVIEW",
    ).to_dict()


def _error_label(error: Exception | None) -> str:
    if isinstance(error, LLMProviderError):
        return error.error_type
    return "schema_error"


def _document_texts(document) -> list[str]:
    if isinstance(document, ContractDocument):
        blocks = document.blocks
        texts = [block.text.strip() for block in blocks if block.text.strip()]
    elif isinstance(document, dict):
        blocks = document.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("document blocks must be a list")
        texts = [str(block.get("text", "")).strip() for block in blocks if isinstance(block, dict)]
        texts = [text for text in texts if text]
    else:
        raise ValueError("document must be a ContractDocument or dict")
    if not texts:
        raise ValueError("document contains no text for contract type classification")
    return texts


def _unsupported_type(text: str) -> tuple[str, str]:
    for contract_type, patterns in UNSUPPORTED_CONTRACT_PATTERNS.items():
        matched = next((pattern for pattern in patterns if pattern in text), None)
        if matched:
            return contract_type, f"文档标题明确包含非 NDA 合同标识：{matched}"
    return "", ""


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)


def _is_nda_title(title_text: str) -> bool:
    return _contains_any(title_text, NDA_TITLE_PATTERNS) or "nda" in re.findall(r"[a-z0-9]+", title_text)


def _normalize_text(text: str) -> str:
    return text.casefold().translate(str.maketrans({character: "-" for character in "‐‑‒–—―"}))
