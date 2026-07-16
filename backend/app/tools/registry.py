from services.clause_service import extract_clauses, extract_key_fields
from services.clause_index_service import retrieve_related_clauses
from services.contract_type_service import classify_contract_type
from services.document_service import parse_document
from services.memory_service import retrieve_memory, write_memory
from services.playbook_service import retrieve_playbook_rules
from services.report_service import generate_report
from services.risk_analyzer import analyze_risk, generate_revision
from services.evidence_service import verify_evidence
from tools.contracts import runtime_calls_llm, runtime_llm_mode, tool_contracts


tool_registry = {
    "parse_document": parse_document,
    "classify_contract_type": classify_contract_type,
    "extract_clauses": extract_clauses,
    "extract_key_fields": extract_key_fields,
    "retrieve_playbook_rules": retrieve_playbook_rules,
    "retrieve_related_clauses": retrieve_related_clauses,
    "retrieve_memory": retrieve_memory,
    "analyze_risk": analyze_risk,
    "verify_evidence": verify_evidence,
    "generate_revision": generate_revision,
    "write_memory": write_memory,
    "generate_report": generate_report,
}


def tool_contracts_payload() -> list[dict]:
    payload = []
    for name in tool_registry:
        contract_payload = tool_contracts[name].to_dict()
        contract_payload["runtime_calls_llm"] = runtime_calls_llm(name)
        contract_payload["runtime_llm_mode"] = runtime_llm_mode(name)
        payload.append(contract_payload)
    return payload
