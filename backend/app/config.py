import os
from dataclasses import dataclass


def _default_memory_db_path() -> str:
    return os.path.join(os.path.dirname(__file__), "data", "review_agent_memory.sqlite3")


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("REVIEW_AGENT_HOST", "127.0.0.1")
    port: int = int(os.getenv("REVIEW_AGENT_PORT", "8000"))
    max_upload_bytes: int = int(os.getenv("REVIEW_AGENT_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    llm_mode: str = os.getenv("REVIEW_AGENT_LLM_MODE", "local_structured")
    memory_db_path: str = os.getenv("REVIEW_AGENT_MEMORY_DB_PATH", _default_memory_db_path())
    service_name: str = "contract-review-agent"


settings = Settings()
