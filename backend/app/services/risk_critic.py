import re

from models.risk import (
    CriticDecision,
    CriticReasonCode,
    validate_critic_result,
    validate_risk_finding,
)
from providers.llm_provider import (
    LLMOutputInvalidError,
    LLMProviderError,
    llm_call_records_from_tool_input,
    mark_latest_llm_call_schema_error,
)
from services.llm_service import generate_structured_critic
from services.prompt_service import detect_prompt_injection


class CriticOutputInvalidError(LLMOutputInvalidError):
    pass


def criticize_risk(tool_input: dict) -> dict:
    critic_input = _validated_critic_input(tool_input)
    local_output = _local_critic_result(critic_input)

    prompt_security = detect_prompt_injection(
        critic_input["finding"],
        critic_input["current_clause"],
    )
    if prompt_security["detected"]:
        return validate_critic_result(
            {
                "decision": CriticDecision.REQUEST_HUMAN_REVIEW.value,
                "reason_code": CriticReasonCode.PROMPT_INJECTION_DETECTED.value,
            }
        ).to_dict()

    llm_calls = llm_call_records_from_tool_input(tool_input)
    last_error = None
    for _attempt in (1, 2):
        try:
            candidate = generate_structured_critic(
                critic_input,
                local_output,
                llm_calls=llm_calls,
            )
            return validate_critic_result(candidate).to_dict()
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
            break
    raise CriticOutputInvalidError(f"critic output invalid: {last_error}")


def _validated_critic_input(tool_input: dict) -> dict:
    if not isinstance(tool_input, dict):
        raise ValueError("critic input must be a dict")
    finding = tool_input.get("finding")
    current_clause = tool_input.get("current_clause")
    matched_rule = tool_input.get("matched_rule")
    if not isinstance(finding, dict):
        raise ValueError("critic finding must be a dict")
    if not isinstance(current_clause, dict):
        raise ValueError("critic current_clause must be a dict")
    if not isinstance(matched_rule, dict):
        raise ValueError("critic matched_rule must be a dict")

    validated_finding = validate_risk_finding(finding).to_dict()
    clause_id = _required_string(current_clause, "clause_id", "current_clause")
    clause_text = _required_string(current_clause, "text", "current_clause")
    rule_id = _required_string(matched_rule, "rule_id", "matched_rule")
    risk_type = _required_string(matched_rule, "risk_type", "matched_rule")
    check_point = _required_string(matched_rule, "check_point", "matched_rule")
    return {
        "finding": {
            field_name: validated_finding[field_name]
            for field_name in (
                "risk_id",
                "risk_type",
                "risk_reason",
                "clause_id",
                "evidence_text",
                "matched_rule_ids",
            )
        },
        "current_clause": {"clause_id": clause_id, "text": clause_text},
        "matched_rule": {
            "rule_id": rule_id,
            "risk_type": risk_type,
            "check_point": check_point,
        },
    }


def _local_critic_result(critic_input: dict) -> dict:
    finding = critic_input["finding"]
    current_clause = critic_input["current_clause"]
    matched_rule = critic_input["matched_rule"]

    if finding["clause_id"] != current_clause["clause_id"]:
        return _result(CriticDecision.REJECT, CriticReasonCode.CLAUSE_MISMATCH)
    evidence_text = finding["evidence_text"].strip()
    if not evidence_text or evidence_text not in current_clause["text"]:
        return _result(CriticDecision.REJECT, CriticReasonCode.EVIDENCE_UNSUPPORTED)
    if (
        finding["risk_type"] != matched_rule["risk_type"]
        or finding["matched_rule_ids"] != [matched_rule["rule_id"]]
    ):
        return _result(CriticDecision.REJECT, CriticReasonCode.PLAYBOOK_MISMATCH)
    if (
        not _reason_supports_evidence(finding["risk_reason"], evidence_text)
        or not _reason_supports_playbook(finding["risk_reason"], matched_rule)
    ):
        return _result(
            CriticDecision.REQUEST_HUMAN_REVIEW,
            CriticReasonCode.REASON_SUPPORT_AMBIGUOUS,
        )
    return _result(
        CriticDecision.PASS,
        CriticReasonCode.SUPPORTED_BY_EVIDENCE_AND_PLAYBOOK,
    )


def _result(decision: CriticDecision, reason_code: CriticReasonCode) -> dict:
    return {"decision": decision.value, "reason_code": reason_code.value}


def _reason_supports_evidence(risk_reason: str, evidence_text: str) -> bool:
    reason_terms = _terms(risk_reason)
    evidence_terms = _terms(evidence_text)
    return bool(reason_terms and evidence_terms and len(reason_terms & evidence_terms) >= 2)


def _reason_supports_playbook(risk_reason: str, matched_rule: dict) -> bool:
    reason_terms = _terms(risk_reason)
    playbook_terms = _terms(
        f"{matched_rule.get('risk_type', '')} {matched_rule.get('check_point', '')}"
    )
    return bool(reason_terms and playbook_terms and len(reason_terms & playbook_terms) >= 2)


def _terms(text: str) -> set[str]:
    normalized = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9]{3,}", normalized))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return terms


def _required_string(payload: dict, field_name: str, label: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"critic {label} {field_name} must be a non-empty string")
    return value.strip()
