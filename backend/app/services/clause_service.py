import re

from models.contract import Clause, ContractDocument


CLAUSE_START_PATTERN = re.compile(
    r"^\s*(第[一二三四五六七八九十百\d]+条|[一二三四五六七八九十]+、|\d+[\.\、]|Article\s+\d+|Section\s+\d+)",
    re.I,
)
INLINE_CLAUSE_PATTERN = re.compile(
    r"(?<!\S)(第[一二三四五六七八九十百\d]+条|[一二三四五六七八九十]+、|\d+[\.\、]|Article\s+\d+|Section\s+\d+)",
    re.I,
)

CLAUSE_TYPE_KEYWORDS = {
    "定义": ("定义", "保密信息", "confidential information", "definition", "definitions"),
    "保密义务": ("保密义务", "保密责任", "不得披露", "confidentiality", "non-disclosure"),
    "使用限制": ("使用目的", "使用限制", "仅用于", "purpose", "use restriction", "use only"),
    "例外": ("例外", "不包括", "除外", "exception", "exclusion", "excluded"),
    "允许披露": ("允许披露", "代表", "关联方", "顾问", "disclose", "representative", "affiliate"),
    "期限": ("期限", "有效期", "持续", "term", "period", "duration", "survive"),
    "返还销毁": ("返还", "销毁", "return", "destroy", "destruction"),
    "违约责任": ("违约", "责任", "赔偿", "违约金", "liability", "damages", "breach"),
    "争议解决": ("争议", "仲裁", "诉讼", "适用法律", "dispute", "arbitration", "governing law"),
}

CLAUSE_TYPE_PRIORITY = (
    "期限",
    "保密义务",
    "使用限制",
    "例外",
    "允许披露",
    "返还销毁",
    "违约责任",
    "争议解决",
    "定义",
)

KEY_FIELD_RULES = {
    "obligation_subject": ("接收方", "接受方", "recipient", "receiving party"),
    "right_holder": ("披露方", "提供方", "disclosing party", "owner"),
    "permitted_disclosure_targets": ("代表", "关联方", "员工", "顾问", "representative", "affiliate", "employee", "advisor"),
    "use_purpose": ("目的", "仅用于", "purpose", "use only"),
    "liability_scope": ("责任", "上限", "不限", "liability", "limitation", "unlimited"),
    "breach_liability": ("违约", "违约金", "赔偿", "breach", "damages", "penalty"),
}

PERIOD_PATTERN = re.compile(r"(永久|无限期|\d+\s*(?:年|个月|years?|months?))", re.I)


def extract_clauses(tool_input: dict) -> list[Clause]:
    document = tool_input.get("document")
    if isinstance(document, dict):
        raise ValueError("extract_clauses 需要 ContractDocument 对象。")
    if not isinstance(document, ContractDocument):
        raise ValueError("extract_clauses 输入无效。")

    groups = _group_blocks(document)
    clauses: list[Clause] = []
    clause_index = 1
    for group in groups:
        text = "\n".join(block.text for block in group).strip()
        if not text:
            continue
        for segment_index, segment in enumerate(_split_inline_clauses(text), start=1):
            clause_id = f"CL-{clause_index:03d}"
            source_location = {
                "start_block_id": group[0].block_id,
                "end_block_id": group[-1].block_id,
                "start_order": group[0].order,
                "end_order": group[-1].order,
                "segment_index": segment_index,
            }
            clauses.append(
                Clause(
                    clause_id=clause_id,
                    title=_extract_title(segment),
                    text=segment,
                    clause_type=_classify_clause(segment),
                    key_fields={},
                    source_location=source_location,
                )
            )
            clause_index += 1

    if not clauses:
        raise ValueError("未能从合同正文中拆分出条款。")
    return clauses


def extract_key_fields(tool_input: dict) -> dict:
    clause = tool_input.get("clause")
    if isinstance(clause, dict):
        text = str(clause.get("text", ""))
    elif isinstance(clause, Clause):
        text = clause.text
    else:
        raise ValueError("extract_key_fields 输入无效。")

    lower_text = text.lower()
    key_fields = {
        "obligation_subject": [],
        "right_holder": [],
        "confidentiality_period": [],
        "permitted_disclosure_targets": [],
        "use_purpose": [],
        "liability_scope": [],
        "breach_liability": [],
    }

    for field_name, keywords in KEY_FIELD_RULES.items():
        matches = [keyword for keyword in keywords if keyword.lower() in lower_text]
        key_fields[field_name] = matches

    key_fields["confidentiality_period"] = PERIOD_PATTERN.findall(text)
    return key_fields


def attach_key_fields(clauses: list[Clause], key_fields_by_clause: dict[str, dict]) -> list[Clause]:
    return [
        Clause(
            clause_id=clause.clause_id,
            title=clause.title,
            text=clause.text,
            clause_type=clause.clause_type,
            key_fields=key_fields_by_clause.get(clause.clause_id, {}),
            source_location=clause.source_location,
        )
        for clause in clauses
    ]


def _group_blocks(document: ContractDocument):
    groups = []
    current_group = []

    for block in document.blocks:
        text = block.text.strip()
        if not text:
            continue
        is_new_clause = CLAUSE_START_PATTERN.match(text) is not None
        if is_new_clause and current_group:
            groups.append(current_group)
            current_group = []
        current_group.append(block)

    if current_group:
        groups.append(current_group)

    if not groups:
        groups = [[block] for block in document.blocks if block.text.strip()]

    return groups


def _extract_title(text: str) -> str:
    first_line = text.splitlines()[0].strip()
    return first_line[:80] if first_line else text[:80]


def _split_inline_clauses(text: str) -> list[str]:
    matches = list(INLINE_CLAUSE_PATTERN.finditer(text))
    if len(matches) <= 1:
        return [text]

    segments: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[start:end].strip()
        if segment:
            segments.append(segment)
    return segments or [text]


def _classify_clause(text: str) -> str:
    lower_text = text.lower()
    leading_text = lower_text[:80]
    if (
        re.search(r"(^|\s)(term|duration|period)\b", leading_text)
        or "期限" in leading_text
        or "有效期" in leading_text
    ):
        return "期限"

    for clause_type in CLAUSE_TYPE_PRIORITY:
        keywords = CLAUSE_TYPE_KEYWORDS[clause_type]
        if any(keyword.lower() in lower_text for keyword in keywords):
            return clause_type
    return "其他"
