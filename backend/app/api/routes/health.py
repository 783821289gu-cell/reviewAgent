from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.dependencies import get_settings
from api.schemas import HealthResponse, StatusesResponse, ToolsResponse
from config import Settings
from models.review import ReviewStatus
from tools.registry import tool_contracts_payload
from services.task_queue import QueueDependencyError


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(
    request: Request,
    app_settings: Settings = Depends(get_settings),
) -> dict | JSONResponse:
    runtime_error = str(
        request.app.state.runtime_dependency_error or ""
    ).strip()
    if runtime_error:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "service": app_settings.service_name,
                "scope": "postgresql-or-redis-unavailable",
            },
        )
    runtime = request.app.state.runtime
    if runtime is not None and not runtime.persistence.is_ready():
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "service": app_settings.service_name,
                "scope": "postgresql-unavailable",
            },
        )
    queued_service = request.app.state.queued_review_service
    if queued_service is not None:
        try:
            queued_service.task_queue.require_ready()
        except QueueDependencyError:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "service": app_settings.service_name,
                    "scope": "rq-worker-unavailable",
                },
            )
    return {
        "status": "ok",
        "service": app_settings.service_name,
        "scope": "task-10",
    }


@router.get("/api/statuses", response_model=StatusesResponse)
def statuses() -> dict:
    return {"statuses": [status.value for status in ReviewStatus]}


@router.get("/api/tools", response_model=ToolsResponse)
def tools() -> dict:
    return {"tools": tool_contracts_payload()}
