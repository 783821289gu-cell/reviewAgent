from fastapi import Request

from api.errors import ApiError
from config import Settings
from services.event_service import ReviewEventStore
from services.review_service import ReviewOrchestratorAgent
from services.task_queue import QueuedReviewService
from services.task_queue import QueueDependencyError


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_event_store(request: Request) -> ReviewEventStore:
    event_store = request.app.state.event_store
    if event_store is None:
        raise _runtime_unavailable(request)
    return event_store


def get_review_agent(request: Request) -> ReviewOrchestratorAgent:
    review_agent = request.app.state.review_agent
    if review_agent is None:
        raise _runtime_unavailable(request)
    return review_agent


def get_queued_review_service(
    request: Request,
) -> QueuedReviewService | None:
    return request.app.state.queued_review_service


def require_accepting_tasks(request: Request) -> None:
    if not request.app.state.accepting_tasks:
        raise _runtime_unavailable(request)
    runtime = request.app.state.runtime
    if runtime is None:
        return
    if not runtime.persistence.is_ready():
        raise _dependency_unavailable("PostgreSQL is unavailable")
    try:
        runtime.task_queue.require_ready()
    except QueueDependencyError as exc:
        raise _dependency_unavailable(str(exc)) from exc


def _runtime_unavailable(request: Request) -> ApiError:
    reason = str(request.app.state.runtime_dependency_error or "").strip()
    message = (
        f"审查运行依赖不可用：{reason}"
        if reason
        else "服务正在启动或关闭，暂不接受审查任务。"
    )
    return ApiError(
        status_code=503,
        payload={
            "status": "TASK_ERROR",
            "message": message,
        },
    )


def _dependency_unavailable(reason: str) -> ApiError:
    return ApiError(
        status_code=503,
        payload={
            "status": "TASK_ERROR",
            "message": f"审查运行依赖不可用：{reason}",
        },
    )
