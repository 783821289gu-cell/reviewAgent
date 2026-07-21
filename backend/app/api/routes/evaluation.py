from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from api.errors import task_error
from services.evaluation_service import run_basic_evaluation, run_effect_evaluation


router = APIRouter()


class EffectEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_ids: list[str] | None = None
    run_label: str = Field(default="", max_length=80)


@router.post("/api/evaluation/run")
def run_evaluation() -> dict:
    try:
        return run_basic_evaluation()
    except ValueError as exc:
        raise task_error(str(exc) or "基础评测请求无效。") from exc


@router.post("/api/evaluation/effect/run")
def run_effect(payload: EffectEvaluationRequest | None = None) -> dict:
    try:
        return run_effect_evaluation(payload.model_dump() if payload is not None else None)
    except ValueError as exc:
        raise task_error(str(exc) or "效果评测请求无效。") from exc
