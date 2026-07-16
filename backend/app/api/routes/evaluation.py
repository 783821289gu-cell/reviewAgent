from fastapi import APIRouter

from api.errors import task_error
from services.evaluation_service import run_basic_evaluation, run_effect_evaluation


router = APIRouter()


@router.post("/api/evaluation/run")
def run_evaluation() -> dict:
    try:
        return run_basic_evaluation()
    except ValueError as exc:
        raise task_error(str(exc) or "基础评测请求无效。") from exc


@router.post("/api/evaluation/effect/run")
def run_effect() -> dict:
    try:
        return run_effect_evaluation()
    except ValueError as exc:
        raise task_error(str(exc) or "效果评测请求无效。") from exc
