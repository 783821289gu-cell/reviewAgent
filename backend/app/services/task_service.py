from models.review import AgentState, LLMMode, ReviewPosition, ReviewStatus, ReviewTask, new_task
from services.document_service import SUPPORTED_FILE_TYPES
from services.event_service import ReviewEventStore
from services.review_service import ReviewOrchestratorAgent, get_default_review_agent


SUPPORTED_REVIEW_POSITIONS = {position.value: position for position in ReviewPosition}
SUPPORTED_LLM_MODES = {mode.value: mode for mode in LLMMode}


def create_task_shell(payload: dict) -> ReviewTask:
    file_name = str(payload.get("file_name", "")).strip()
    file_type = str(payload.get("file_type", "")).strip().lower()
    review_position_value = str(payload.get("review_position", "")).strip()
    llm_mode_value = str(payload.get("llm_mode", LLMMode.LOCAL_STRUCTURED.value)).strip()

    if not file_name:
        raise ValueError("file_name is required")

    if not file_type:
        raise ValueError("file_type is required")

    if file_type not in SUPPORTED_FILE_TYPES:
        raise ValueError("仅支持上传 .docx 或 .pdf 文件。")

    if review_position_value not in SUPPORTED_REVIEW_POSITIONS:
        raise ValueError("review_position must be 甲方 or 乙方")
    if llm_mode_value not in SUPPORTED_LLM_MODES:
        raise ValueError("llm_mode must be local_structured or openai_compatible")

    return new_task(
        file_name=file_name,
        file_type=file_type,
        review_position=SUPPORTED_REVIEW_POSITIONS[review_position_value],
        llm_mode=SUPPORTED_LLM_MODES[llm_mode_value],
    )


def create_review_task(
    file_name: str,
    content: bytes,
    review_position_value: str,
    llm_mode_value: str = LLMMode.LOCAL_STRUCTURED.value,
) -> ReviewTask:
    state = _run_review(
        file_name,
        content,
        review_position_value,
        llm_mode_value,
        async_mode=False,
    )
    return state.to_review_task()


def start_review_task(
    file_name: str,
    content: bytes,
    review_position_value: str,
    llm_mode_value: str = LLMMode.LOCAL_STRUCTURED.value,
    review_agent: ReviewOrchestratorAgent | None = None,
) -> AgentState:
    return _run_review(
        file_name,
        content,
        review_position_value,
        llm_mode_value,
        async_mode=True,
        review_agent=review_agent,
    )


def cancel_review_task(
    task_id: str,
    reason: str,
    event_store: ReviewEventStore,
) -> dict:
    normalized_reason = str(reason).strip()
    if not normalized_reason:
        raise ValueError("cancel reason is required")
    if len(normalized_reason) > 300:
        raise ValueError("cancel reason must not exceed 300 characters")
    state = event_store.request_cancel(task_id, normalized_reason)
    if not event_store.is_execution_active(task_id):
        state = event_store.finalize_cancel(task_id)
    task_payload = event_store.get_task_payload(task_id) or state.to_dict()
    return {
        "status": task_payload["status"],
        "message": task_payload["message"],
        "task": task_payload,
    }


def recover_review_task(
    task_id: str,
    payload: dict,
    review_agent: ReviewOrchestratorAgent,
) -> dict:
    reason, operator_action, resume_from = parse_recovery_request(payload)
    state = review_agent.recover_task(
        task_id,
        resume_from=resume_from,
        operator_action=operator_action,
        reason=reason,
    )
    task_payload = review_agent.event_store.get_task_payload(task_id) or state.to_dict()
    return {
        "status": task_payload["status"],
        "message": task_payload["message"],
        "task": task_payload,
    }


def parse_recovery_request(
    payload: dict,
) -> tuple[str, str, ReviewStatus | None]:
    reason = str(payload.get("reason", "")).strip()
    operator_action = str(
        payload.get("operator_action", "manual_retry")
    ).strip()
    resume_from_value = str(payload.get("resume_from", "")).strip()
    if not reason:
        raise ValueError("recovery reason is required")
    if len(reason) > 500:
        raise ValueError("recovery reason must not exceed 500 characters")
    if not operator_action or len(operator_action) > 100:
        raise ValueError("operator_action is invalid")
    resume_from = None
    if resume_from_value:
        try:
            resume_from = ReviewStatus(resume_from_value)
        except ValueError as exc:
            raise ValueError(f"invalid recovery checkpoint: {resume_from_value}") from exc
    return reason, operator_action, resume_from


def _run_review(
    file_name: str,
    content: bytes,
    review_position_value: str,
    llm_mode_value: str,
    async_mode: bool,
    review_agent: ReviewOrchestratorAgent | None = None,
) -> AgentState:
    normalized_file_name = file_name.strip()
    file_type = _get_file_type(normalized_file_name)
    review_position = _get_review_position(review_position_value)
    llm_mode = _get_llm_mode(llm_mode_value)

    if file_type not in SUPPORTED_FILE_TYPES:
        raise ValueError("仅支持上传 .docx 或 .pdf 文件。")
    if not content:
        raise ValueError("上传文件为空，无法解析。")

    active_review_agent = review_agent or get_default_review_agent()
    if async_mode:
        return active_review_agent.start(
            file_name=normalized_file_name,
            file_type=file_type,
            content=content,
            review_position=review_position,
            llm_mode=llm_mode,
        )
    return active_review_agent.run_sync(
        file_name=normalized_file_name,
        file_type=file_type,
        content=content,
        review_position=review_position,
        llm_mode=llm_mode,
    )


def _get_file_type(file_name: str) -> str:
    if not file_name:
        raise ValueError("file_name is required")
    if "." not in file_name:
        raise ValueError("仅支持上传 .docx 或 .pdf 文件。")
    return file_name.rsplit(".", 1)[1].lower()


def _get_review_position(review_position_value: str) -> ReviewPosition:
    normalized_value = str(review_position_value or "").strip()
    if normalized_value not in SUPPORTED_REVIEW_POSITIONS:
        raise ValueError("review_position must be 甲方 or 乙方")
    return SUPPORTED_REVIEW_POSITIONS[normalized_value]


def _get_llm_mode(llm_mode_value: str) -> LLMMode:
    normalized_value = str(llm_mode_value or "").strip()
    if normalized_value not in SUPPORTED_LLM_MODES:
        raise ValueError("llm_mode must be local_structured or openai_compatible")
    return SUPPORTED_LLM_MODES[normalized_value]
