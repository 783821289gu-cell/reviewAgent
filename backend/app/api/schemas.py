from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    scope: str


class StatusesResponse(BaseModel):
    statuses: list[str]


class ToolsResponse(BaseModel):
    tools: list[dict]


class FeedbackRequest(BaseModel):
    risk_id: str
    action: Literal[
        "accept",
        "ignore",
        "update_severity",
        "update_suggestion",
        "update_evidence",
    ]
    final_severity: str | None = None
    final_suggestion: str | None = None
    final_evidence_text: str | None = None
    ignore_reason: str | None = None
    include_in_report: bool | None = None


class LocalReviewRequest(BaseModel):
    task_id: str
    clause_id: str
    selected_text: str
