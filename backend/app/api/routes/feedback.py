from fastapi import APIRouter, Depends

from api.dependencies import get_event_store
from api.errors import task_error
from api.schemas import FeedbackRequest
from services.event_service import ReviewEventStore
from services.feedback_service import apply_feedback_to_task


router = APIRouter()


@router.post("/api/tasks/{task_id}/feedback")
def submit_feedback(
    task_id: str,
    payload: FeedbackRequest,
    event_store: ReviewEventStore = Depends(get_event_store),
) -> dict:
    try:
        return apply_feedback_to_task(
            task_id,
            payload.model_dump(exclude_none=True),
            event_store=event_store,
        )
    except ValueError as exc:
        raise task_error(str(exc) or "人工反馈请求无效。") from exc
