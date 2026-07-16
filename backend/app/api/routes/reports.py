from fastapi import APIRouter, Depends

from api.dependencies import get_event_store
from api.errors import ApiError, task_error
from services.event_service import ReviewEventStore
from services.report_task_service import ReportTaskNotFoundError, generate_task_report


router = APIRouter()


@router.post("/api/tasks/{task_id}/report")
def create_report(
    task_id: str,
    event_store: ReviewEventStore = Depends(get_event_store),
) -> dict:
    try:
        return generate_task_report(task_id, event_store)
    except ReportTaskNotFoundError as exc:
        raise ApiError(status_code=404, payload={"message": str(exc)}) from exc
    except ValueError as exc:
        raise task_error(str(exc) or "报告生成请求无效。") from exc
