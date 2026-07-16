from models.review import AgentState, ReviewPosition, ReviewTask, new_task
from services.document_service import SUPPORTED_FILE_TYPES
from services.review_service import ReviewOrchestratorAgent, review_orchestrator_agent


SUPPORTED_REVIEW_POSITIONS = {position.value: position for position in ReviewPosition}


def create_task_shell(payload: dict) -> ReviewTask:
    file_name = str(payload.get("file_name", "")).strip()
    file_type = str(payload.get("file_type", "")).strip().lower()
    review_position_value = str(payload.get("review_position", "")).strip()

    if not file_name:
        raise ValueError("file_name is required")

    if not file_type:
        raise ValueError("file_type is required")

    if file_type not in SUPPORTED_FILE_TYPES:
        raise ValueError("仅支持上传 .docx 或 .pdf 文件。")

    if review_position_value not in SUPPORTED_REVIEW_POSITIONS:
        raise ValueError("review_position must be 甲方 or 乙方")

    return new_task(
        file_name=file_name,
        file_type=file_type,
        review_position=SUPPORTED_REVIEW_POSITIONS[review_position_value],
    )


def create_review_task(file_name: str, content: bytes, review_position_value: str) -> ReviewTask:
    state = _run_review(file_name, content, review_position_value, async_mode=False)
    return state.to_review_task()


def start_review_task(
    file_name: str,
    content: bytes,
    review_position_value: str,
    review_agent: ReviewOrchestratorAgent = review_orchestrator_agent,
) -> AgentState:
    return _run_review(file_name, content, review_position_value, async_mode=True, review_agent=review_agent)


def _run_review(
    file_name: str,
    content: bytes,
    review_position_value: str,
    async_mode: bool,
    review_agent: ReviewOrchestratorAgent = review_orchestrator_agent,
) -> AgentState:
    normalized_file_name = file_name.strip()
    file_type = _get_file_type(normalized_file_name)
    review_position = _get_review_position(review_position_value)

    if file_type not in SUPPORTED_FILE_TYPES:
        raise ValueError("仅支持上传 .docx 或 .pdf 文件。")
    if not content:
        raise ValueError("上传文件为空，无法解析。")

    if async_mode:
        return review_agent.start(
            file_name=normalized_file_name,
            file_type=file_type,
            content=content,
            review_position=review_position,
        )
    return review_agent.run_sync(
        file_name=normalized_file_name,
        file_type=file_type,
        content=content,
        review_position=review_position,
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
