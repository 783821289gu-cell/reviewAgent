from fastapi import APIRouter, Depends

from api.dependencies import get_settings
from api.schemas import HealthResponse, StatusesResponse, ToolsResponse
from config import Settings
from models.review import ReviewStatus
from tools.registry import tool_contracts_payload


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(app_settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "service": app_settings.service_name,
        "scope": "task-9",
    }


@router.get("/api/statuses", response_model=StatusesResponse)
def statuses() -> dict:
    return {"statuses": [status.value for status in ReviewStatus]}


@router.get("/api/tools", response_model=ToolsResponse)
def tools() -> dict:
    return {"tools": tool_contracts_payload()}
