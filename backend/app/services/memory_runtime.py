from contextvars import ContextVar, Token
from typing import Protocol


class RelatedMemoryStore(Protocol):
    source: str

    def save(self, item: dict, idempotency_key: str | None = None) -> dict: ...

    def find_ranked_preferences(
        self,
        *,
        contract_type: str,
        clause: dict,
        risk_type: str,
        review_position: str,
        limit: int,
        embedding_call_records: list | None,
    ) -> list[dict]: ...

    def update_preference_lifecycle(
        self,
        preference_id: str,
        *,
        contract_type: str,
        review_position: str,
        lifecycle_status: str,
        confidence: float,
        updated_at: str,
        last_used_at: str | None = None,
    ) -> None: ...

    def stable_memory_id(self, idempotency_key: str) -> str: ...

    def close(self) -> None: ...


_MEMORY_STORE: ContextVar[RelatedMemoryStore | None] = ContextVar(
    "review_agent_memory_store",
    default=None,
)


def bind_memory_store(store: RelatedMemoryStore | None) -> Token:
    return _MEMORY_STORE.set(store)


def reset_memory_store(token: Token) -> None:
    _MEMORY_STORE.reset(token)


def current_memory_store() -> RelatedMemoryStore | None:
    return _MEMORY_STORE.get()
