from contextvars import ContextVar, Token
from typing import Protocol


class RelatedClauseRetriever(Protocol):
    def retrieve(self, task_id: str, request, call_records=None) -> list[dict]: ...

    def close(self) -> None: ...


_RELATED_CLAUSE_RETRIEVER: ContextVar[RelatedClauseRetriever | None] = ContextVar(
    "review_agent_related_clause_retriever",
    default=None,
)
_RETRIEVAL_TASK_ID: ContextVar[str] = ContextVar(
    "review_agent_retrieval_task_id",
    default="",
)


def bind_related_clause_retriever(
    retriever: RelatedClauseRetriever | None,
    task_id: str,
) -> tuple[Token, Token]:
    return (
        _RELATED_CLAUSE_RETRIEVER.set(retriever),
        _RETRIEVAL_TASK_ID.set(task_id),
    )


def reset_related_clause_retriever(tokens: tuple[Token, Token]) -> None:
    retriever_token, task_token = tokens
    _RETRIEVAL_TASK_ID.reset(task_token)
    _RELATED_CLAUSE_RETRIEVER.reset(retriever_token)


def current_related_clause_retriever() -> RelatedClauseRetriever | None:
    return _RELATED_CLAUSE_RETRIEVER.get()


def current_retrieval_task_id() -> str:
    return _RETRIEVAL_TASK_ID.get()
