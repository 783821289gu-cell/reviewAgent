"""Analyzer 角色：根据“当前条款 + Playbook 规则”提出候选风险。

先把它理解成合同审查里的“初审员”，而不是一个独立 Agent：

* LangGraph 的 ``analyze_risk`` 节点决定什么时候调用本文件。
* 本文件只完成一次结构化判断，没有自己的状态、记忆或工具选择循环。
* DeepSeek 模式下，它和 Critic、Planner 共用同一个 LLM Provider，但
  ``operation="analyze_risk"`` 会让 PromptService 使用 Analyzer 专属提示词。
* 本地模式下不会请求 DeepSeek，而是返回 ``llm_service`` 生成的确定性结果；
  后面的字段校验完全相同。

Analyzer 专属提示词原文位于 ``prompt_service._OPERATION_INSTRUCTIONS``：

    Analyze only the current clause against the supplied Playbook rule and
    evidence constraint. A formal risk must quote evidence from the current clause.

意思是：只分析当前条款，不得扩大到合同外；正式风险必须引用当前条款原文。
最终发给 DeepSeek 的并不只有这一句话，还会自动拼上共用系统规则、输入数据和
``RISK_OUTPUT_SCHEMA``。完整拼装过程见 ``prompt_service.build_prompt_package``。
"""

from models.risk import validate_risk_finding
from providers.llm_provider import (
    LLMOutputInvalidError,
    LLMProviderError,
    llm_call_records_from_tool_input,
    mark_latest_llm_call_schema_error,
)
from services.llm_service import (
    REVISION_OUTPUT_SCHEMA,
    RISK_OUTPUT_SCHEMA,
    generate_structured_revision,
    generate_structured_risk,
)
from services.prompt_service import PROMPT_VERSION


def analyze_risk(tool_input: dict) -> dict:
    """执行一次 Analyzer 判断，返回经过校验的 ``RiskFinding`` 字典。

    ``tool_input`` 不是用户手写的提示词，而是 Tool Registry 传入的工具参数。
    业务上真正需要的是 ``tool_input["review_context"]``；工具执行器还可能附加
    ``_llm_call_records``，用于记录本次 DeepSeek 调用耗时、token 和错误。

    ``review_context`` 的核心数据来自前面节点：

    * ``current_clause``：当前正在审查的合同条款。
    * ``matched_rule``：Playbook 检索命中的一条规则。
    * ``review_position``：甲方或乙方审查立场。
    * ``related_clauses``：检索到的关联条款。
    * ``related_memory``：可以参考但不能覆盖 Playbook 的历史反馈。
    * ``evidence_constraints``：证据必须来自哪里等硬约束。

    简化示例：

    输入 A::

        {
            "current_clause": {"clause_id": "CL-002", "text": "保密义务持续1年"},
            "matched_rule": {"rule_id": "NDA-R01", "risk_type": "保密期限不足"},
            "review_position": "甲方"
        }

    DeepSeek 候选输出 B::

        {
            "clause_id": "CL-002",
            "risk_type": "保密期限不足",
            "evidence_text": "保密义务持续1年",
            "risk_reason": "期限低于规则要求",
            ...
        }

    B 只有通过字段白名单、RiskFinding 类型校验和审查立场校验后才会返回给
    LangGraph；否则抛出 ``LLMOutputInvalidError``，不会把半合法结果向后传。
    """

    # 第 1 步：只取 Analyzer 的业务输入。缺少 review_context 时不调用模型。
    review_context = tool_input.get("review_context")
    if not isinstance(review_context, dict):
        raise ValueError("review_context must be a dict")

    # 第 2 步：把 Playbook 决定的权威字段和 prompt 版本加入上下文。
    # 这样 DeepSeek 可以写“风险理由”，但不能把 CL-002 改成其他条款，也不能
    # 自行改变风险类型、规则 ID、审查立场和 Playbook 给出的修改方向。
    review_context = _with_authoritative_output_constraints(review_context)

    # 这不是模型输入内容，而是工具执行器提供的审计列表。Provider 每调用一次
    # DeepSeek，都会向列表追加一条 LLMCallMetadata。
    llm_calls = llm_call_records_from_tool_input(tool_input)

    last_error = None
    # 最多尝试两次。attempt 会传给本地调试输出选择逻辑；真实 Provider 的网络
    # 重试与这里的“重新生成一次结构化结果”是不同层次。
    for attempt in (1, 2):
        try:
            # 第 3 步：进入 llm_service。它用 operation="analyze_risk" 选择
            # Analyzer 专属提示词，并要求输出严格符合 RISK_OUTPUT_SCHEMA。
            candidate = generate_structured_risk(
                review_context,
                attempt=attempt,
                llm_calls=llm_calls,
            )

            # 第 4 步：先拒绝模型偷偷增加的字段，再覆盖不能由模型决定的字段。
            _validate_output_fields(candidate, RISK_OUTPUT_SCHEMA, "risk finding")
            candidate = _apply_authoritative_fields(candidate, review_context)

            # 第 5 步：把普通 dict 变成领域模型做完整校验，再确认结论仍属于
            # 当前条款、当前规则和当前审查立场。
            finding = validate_risk_finding(candidate)
            _validate_position_basis(finding.to_dict(), review_context)
            return finding.to_dict()
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
            break
    raise LLMOutputInvalidError(f"LLM output invalid: {last_error}")


def _apply_authoritative_fields(candidate: dict, review_context: dict) -> dict:
    """用 Playbook 权威值覆盖模型无权决定的建议方向。

    DeepSeek 仍被要求输出这些字段，是为了得到固定 JSON 结构；真正返回前，
    ``risk_focus`` 和 ``revision_suggestion`` 会以 Playbook 配置为准。
    """

    normalized = dict(candidate)
    authoritative = _authoritative_fields(review_context)
    normalized["risk_focus"] = authoritative["risk_focus"]
    normalized["revision_suggestion"] = authoritative["revision_suggestion"]
    return normalized


def _with_authoritative_output_constraints(review_context: dict) -> dict:
    """给 Prompt 增加模型必须遵守的固定字段和版本号，不修改原字典。"""

    normalized = dict(review_context)
    output_constraints = dict(review_context.get("output_constraints") or {})
    output_constraints["authoritative_fields"] = _authoritative_fields(review_context)
    normalized["output_constraints"] = output_constraints
    normalized["prompt_version"] = PROMPT_VERSION
    return normalized


def _authoritative_fields(review_context: dict) -> dict:
    """从可信业务数据中提取 Analyzer 不得自行改变的字段。

    数据来源：

    * 条款 ID 来自 ``current_clause``。
    * 风险类型和规则 ID 来自 ``matched_rule``。
    * 风险关注点和修改模板来自该规则当前立场的 ``position_config``。
    """

    review_position = str(review_context.get("review_position", "")).strip()
    if not review_position:
        raise ValueError("review_context review_position is required")
    matched_rule = review_context.get("matched_rule")
    if not isinstance(matched_rule, dict):
        raise ValueError("review_context matched_rule must be a dict")
    position_config = matched_rule.get("position_config")
    if not isinstance(position_config, dict):
        raise ValueError("matched rule position_config is required")
    current_clause = review_context.get("current_clause")
    if not isinstance(current_clause, dict):
        raise ValueError("review_context current_clause must be a dict")
    return {
        "risk_type": str(matched_rule.get("risk_type", "")),
        "clause_id": str(current_clause.get("clause_id", "")),
        "matched_rule_ids": [str(matched_rule.get("rule_id", ""))],
        "review_position": review_position,
        "risk_focus": str(position_config.get("risk_focus", "")),
        "revision_suggestion": str(position_config.get("revision_template", "")),
    }


def _validate_position_basis(finding: dict, review_context: dict) -> None:
    """防止模型输出跨条款、跨规则或改变甲乙方立场。"""

    review_position = str(review_context.get("review_position", "")).strip()
    matched_rule = review_context.get("matched_rule")
    if not isinstance(matched_rule, dict):
        raise ValueError("review_context matched_rule must be a dict")
    position_config = matched_rule.get("position_config")
    if not isinstance(position_config, dict):
        raise ValueError("matched rule position_config is required")

    if str(finding.get("review_position", "")).strip() != review_position:
        raise ValueError("risk finding review_position does not match review context")
    if str(finding.get("risk_focus", "")).strip() != str(
        position_config.get("risk_focus", "")
    ).strip():
        raise ValueError("risk finding risk_focus does not match position_config")
    if str(finding.get("risk_type", "")) != str(matched_rule.get("risk_type", "")):
        raise ValueError("risk finding risk_type does not match matched rule")
    current_clause = review_context.get("current_clause")
    if not isinstance(current_clause, dict):
        raise ValueError("review_context current_clause must be a dict")
    if str(finding.get("clause_id", "")).strip() != str(
        current_clause.get("clause_id", "")
    ).strip():
        raise ValueError("risk finding clause_id is outside the current clause whitelist")
    matched_rule_id = str(matched_rule.get("rule_id", "")).strip()
    finding_rule_ids = [
        str(rule_id).strip() for rule_id in finding.get("matched_rule_ids") or []
    ]
    if finding_rule_ids != [matched_rule_id]:
        raise ValueError("risk finding rule IDs are outside the matched rule whitelist")


def generate_revision(tool_input: dict) -> dict:
    finding = tool_input.get("finding")
    preferred_position = str(tool_input.get("preferred_position", "")).strip()
    if not isinstance(finding, dict):
        raise ValueError("finding must be a dict")
    if not preferred_position:
        raise ValueError("preferred_position is required")
    finding_position = str(finding.get("review_position", "")).strip()
    if finding_position != preferred_position:
        raise ValueError("preferred_position does not match finding review_position")
    llm_calls = llm_call_records_from_tool_input(tool_input)

    local_revision = str(finding.get("revision_suggestion", "")).strip()
    if not local_revision:
        local_revision = (
            f"建议围绕“{finding.get('risk_type', '')}”和“{finding.get('risk_focus', '')}”"
            f"补充明确、可执行且符合{preferred_position}审查立场的条款表述。"
        )

    last_error = None
    revision_text = ""
    for _attempt in (1, 2):
        try:
            candidate = generate_structured_revision(
                finding,
                preferred_position,
                local_revision,
                llm_calls=llm_calls,
            )
            _validate_output_fields(candidate, REVISION_OUTPUT_SCHEMA, "revision output")
            revision_text = _validate_revision_candidate(candidate)
            break
        except LLMProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            mark_latest_llm_call_schema_error(llm_calls)
            break
    if not revision_text:
        raise LLMOutputInvalidError(f"revision output invalid: {last_error}")

    memory_references = _memory_references(finding.get("related_memory") or [])
    if memory_references:
        reference = memory_references[0]
        suggested_text = reference.get("final_suggestion", "")
        if suggested_text:
            revision_text = f"{revision_text} 历史反馈参考：{suggested_text}"

    return {
        "revision_suggestion": revision_text,
        "preferred_position": preferred_position,
        "risk_focus": str(finding.get("risk_focus", "")),
        "basis_rule_ids": list(finding.get("matched_rule_ids") or []),
        "memory_references": memory_references,
    }


def _validate_revision_candidate(candidate: dict) -> str:
    if not isinstance(candidate, dict):
        raise ValueError("revision output must be a dict")
    revision_text = candidate.get("revision_suggestion")
    if not isinstance(revision_text, str) or not revision_text.strip():
        raise ValueError("revision_suggestion must be a non-empty string")
    return revision_text.strip()


def _validate_output_fields(candidate: dict, output_schema: dict, label: str) -> None:
    if not isinstance(candidate, dict):
        raise ValueError(f"{label} must be a dict")
    allowed_fields = set((output_schema.get("properties") or {}).keys())
    unexpected_fields = set(candidate) - allowed_fields
    if unexpected_fields:
        raise ValueError(
            f"{label} contains fields outside the output whitelist: "
            f"{', '.join(sorted(str(item) for item in unexpected_fields))}"
        )


def _memory_references(related_memory: list) -> list[dict]:
    references = []
    for item in related_memory:
        if not isinstance(item, dict):
            continue
        injection = item.get("memory_injection") or {}
        if item.get("memory_type") != "semantic_preference":
            continue
        if not item.get("can_influence_suggestion"):
            continue
        if injection.get("trimmed"):
            continue
        references.append(
            {
                "memory_id": str(item.get("memory_id", "")),
                "memory_type": "semantic_preference",
                "memory_source": str(item.get("memory_source", "")),
                "match_score": item.get("match_score", 0),
                "confidence": item.get("confidence", 0.0),
                "final_severity": str(item.get("final_severity", "")),
                "final_suggestion": str(item.get("final_suggestion", "")),
                "source_memory_ids": list(item.get("source_memory_ids") or []),
                "suggestion_affected": True,
                "trimmed": False,
            }
        )
        break
    return references
