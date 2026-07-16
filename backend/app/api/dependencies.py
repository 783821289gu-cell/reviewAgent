from fastapi import Request

from api.errors import ApiError
from config import Settings
from services.event_service import ReviewEventStore
from services.review_service import ReviewOrchestratorAgent


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_event_store(request: Request) -> ReviewEventStore:
    return request.app.state.event_store


def get_review_agent(request: Request) -> ReviewOrchestratorAgent:
    return request.app.state.review_agent


def require_accepting_tasks(request: Request) -> None:
    if not request.app.state.accepting_tasks:
        raise ApiError(
            status_code=503,
            payload={
                "status": "TASK_ERROR",
                "message": "服务正在关闭，暂不接受新的审查任务。",
            },
        )
