from fastapi import APIRouter, Depends

from api.dependencies import get_event_store, get_review_agent
from api.errors import task_error
from api.schemas import FeedbackRequest
from services.event_service import ReviewEventStore
from services.feedback_service import apply_feedback_to_task
from services.review_service import ReviewOrchestratorAgent


router = APIRouter()


@router.post("/api/tasks/{task_id}/feedback")
def submit_feedback(
    task_id: str,
    payload: FeedbackRequest,
    event_store: ReviewEventStore = Depends(get_event_store),
    review_agent: ReviewOrchestratorAgent = Depends(get_review_agent),
) -> dict:
    try:
        feedback_payload = payload.model_dump(exclude_none=True)
        result = apply_feedback_to_task(
            task_id,
            feedback_payload,
            event_store=event_store,
            memory_store=review_agent.memory_store,
        )
        resumed = review_agent.resume_human_review(
            task_id,
            {
                "action": feedback_payload["action"],
                "risk_id": feedback_payload["risk_id"],
            },
        )
        task_payload = event_store.get_task_payload(task_id) or resumed.to_dict()
        result["status"] = task_payload["status"]
        result["message"] = task_payload["message"]
        result["task"] = task_payload
        return result
    except ValueError as exc:
        raise task_error(str(exc) or "人工反馈请求无效。") from exc
