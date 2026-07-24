import os
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ENV_FILE = PROJECT_ROOT / ".env"


def _load_project_environment(dotenv_path: str | Path = PROJECT_ENV_FILE) -> bool:
    if os.getenv("REVIEW_AGENT_LOAD_DOTENV", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    return load_dotenv(dotenv_path=dotenv_path, override=False)


_load_project_environment()


def _default_memory_db_path() -> str:
    runtime_root = os.getenv("REVIEW_AGENT_RUNTIME_ROOT", "").strip()
    if runtime_root:
        return str(Path(runtime_root) / "data" / "review_agent_memory.sqlite3")
    return os.path.join(os.path.dirname(__file__), "data", "review_agent_memory.sqlite3")


def _default_upload_dir() -> str:
    runtime_root = os.getenv("REVIEW_AGENT_RUNTIME_ROOT", "").strip()
    if runtime_root:
        return str(Path(runtime_root) / "data" / "uploads")
    return os.path.join(os.path.dirname(__file__), "data", "uploads")


def _default_runtime_log_file() -> str:
    runtime_root = os.getenv("REVIEW_AGENT_RUNTIME_ROOT", "").strip()
    if runtime_root:
        return str(Path(runtime_root) / "logs" / "review_agent.log")
    return os.path.join(os.path.dirname(__file__), "logs", "review_agent.log")


def _allowed_origins() -> tuple[str, ...]:
    raw_value = os.getenv("REVIEW_AGENT_ALLOWED_ORIGINS", "")
    origins = tuple(origin.strip().rstrip("/") for origin in raw_value.split(",") if origin.strip())
    if "*" in origins:
        raise ValueError("REVIEW_AGENT_ALLOWED_ORIGINS must not contain '*'")
    return origins


def _optional_non_negative_float(name: str) -> float | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    value = float(raw_value)
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return value


def _positive_float(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def _positive_int(name: str, default: str) -> int:
    raw_value = os.getenv(name, default).strip()
    if not raw_value.isdigit() or int(raw_value) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(raw_value)


def _bounded_positive_int(name: str, default: str, maximum: int) -> int:
    value = _positive_int(name, default)
    if value > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("REVIEW_AGENT_HOST", "127.0.0.1")
    port: int = int(os.getenv("REVIEW_AGENT_PORT", "8000"))
    max_upload_bytes: int = int(os.getenv("REVIEW_AGENT_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    allowed_origins: tuple[str, ...] = field(default_factory=_allowed_origins)
    llm_mode: str = os.getenv("REVIEW_AGENT_LLM_MODE", "local_structured")
    llm_base_url: str = os.getenv("REVIEW_AGENT_LLM_BASE_URL", "").strip()
    llm_api_key: str = os.getenv("REVIEW_AGENT_LLM_API_KEY", "").strip()
    llm_model: str = os.getenv("REVIEW_AGENT_LLM_MODEL", "").strip()
    llm_timeout_seconds: float = field(
        default_factory=lambda: _positive_float("REVIEW_AGENT_LLM_TIMEOUT_SECONDS", "60")
    )
    llm_context_budget_tokens: int = field(
        default_factory=lambda: _positive_int(
            "REVIEW_AGENT_LLM_CONTEXT_BUDGET_TOKENS", "6000"
        )
    )
    llm_prompt_cost_per_million: float | None = field(
        default_factory=lambda: _optional_non_negative_float("REVIEW_AGENT_LLM_PROMPT_COST_PER_1M")
    )
    llm_completion_cost_per_million: float | None = field(
        default_factory=lambda: _optional_non_negative_float("REVIEW_AGENT_LLM_COMPLETION_COST_PER_1M")
    )
    embedding_mode: str = os.getenv("REVIEW_AGENT_EMBEDDING_MODE", "openai_compatible")
    embedding_base_url: str = os.getenv("REVIEW_AGENT_EMBEDDING_BASE_URL", "").strip()
    embedding_api_key: str = os.getenv("REVIEW_AGENT_EMBEDDING_API_KEY", "").strip()
    embedding_model: str = os.getenv("REVIEW_AGENT_EMBEDDING_MODEL", "").strip()
    embedding_timeout_seconds: float = field(
        default_factory=lambda: _positive_float("REVIEW_AGENT_EMBEDDING_TIMEOUT_SECONDS", "60")
    )
    node_timeout_seconds: float = field(
        default_factory=lambda: _positive_float("REVIEW_AGENT_NODE_TIMEOUT_SECONDS", "90")
    )
    task_timeout_seconds: float = field(
        default_factory=lambda: _positive_float("REVIEW_AGENT_TASK_TIMEOUT_SECONDS", "900")
    )
    deepseek_task_timeout_seconds: float = field(
        default_factory=lambda: _positive_float(
            "REVIEW_AGENT_DEEPSEEK_TASK_TIMEOUT_SECONDS", "3600"
        )
    )
    llm_max_concurrency: int = field(
        default_factory=lambda: _bounded_positive_int(
            "REVIEW_AGENT_LLM_MAX_CONCURRENCY", "2", 4
        )
    )
    memory_db_path: str = os.getenv("REVIEW_AGENT_MEMORY_DB_PATH", _default_memory_db_path())
    database_url: str = os.getenv("REVIEW_AGENT_DATABASE_URL", "").strip()
    redis_url: str = os.getenv("REVIEW_AGENT_REDIS_URL", "redis://127.0.0.1:6379/0").strip()
    rq_queue: str = os.getenv("REVIEW_AGENT_RQ_QUEUE", "review-agent").strip()
    rq_job_timeout_seconds: int = field(
        default_factory=lambda: _positive_int(
            "REVIEW_AGENT_RQ_JOB_TIMEOUT_SECONDS", "7200"
        )
    )
    rq_status_reserve_seconds: int = field(
        default_factory=lambda: _positive_int(
            "REVIEW_AGENT_RQ_STATUS_RESERVE_SECONDS", "120"
        )
    )
    upload_dir: str = os.getenv("REVIEW_AGENT_UPLOAD_DIR", _default_upload_dir())
    runtime_log_file: str = os.getenv(
        "REVIEW_AGENT_RUNTIME_LOG_FILE",
        _default_runtime_log_file(),
    )
    runtime_log_level: str = os.getenv("REVIEW_AGENT_RUNTIME_LOG_LEVEL", "INFO").upper()
    service_name: str = "contract-review-agent"


settings = Settings()
