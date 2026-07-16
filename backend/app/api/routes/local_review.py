from fastapi import APIRouter, Depends

from api.dependencies import get_event_store
from api.errors import ApiError, task_error
from api.schemas import LocalReviewRequest
from services.event_service import ReviewEventStore
from services.local_review_service import run_local_review


router = APIRouter()


@router.post("/api/local-review")
def local_review(
    payload: LocalReviewRequest,
    event_store: ReviewEventStore = Depends(get_event_store),
) -> dict:
    task = event_store.get_task(payload.task_id.strip())
    if task is None:
        raise ApiError(status_code=404, payload={"message": "未找到审查任务，无法执行局部审查。"})

    try:
        return run_local_review(
            task.to_dict(),
            clause_id=payload.clause_id,
            selected_text=payload.selected_text,
        )
    except ValueError as exc:
        raise task_error(str(exc) or "局部审查请求无效。") from exc
