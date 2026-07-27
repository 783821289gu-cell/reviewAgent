from dataclasses import dataclass
from threading import Lock

from redis import Redis

from config import Settings
from db.postgres_persistence import PostgresReviewPersistence
from providers.bge_provider import BGEEmbeddingProvider
from services.checkpoint_service import ReviewCheckpointManager
from services.event_notification import (
    PersistentEventStream,
    RedisEventNotifier,
)
from services.event_service import ReviewEventStore
from services.langgraph_memory_store import LangGraphPostgresMemoryStore
from services.postgres_clause_retrieval import PostgresHybridClauseRetriever
from services.review_service import ReviewOrchestratorAgent
from services.task_queue import QueuedReviewService, ReviewTaskQueue


@dataclass
class ApplicationRuntime:
    persistence: PostgresReviewPersistence
    redis_client: Redis
    event_store: ReviewEventStore
    persistent_event_stream: PersistentEventStream
    task_queue: ReviewTaskQueue
    queued_review_service: QueuedReviewService
    review_agent: ReviewOrchestratorAgent

    def close(self) -> None:
        try:
            self.review_agent.close()
        finally:
            try:
                self.persistence.dispose()
            finally:
                self.redis_client.close()


@dataclass
class ReviewWorkerRuntime:
    settings: Settings
    persistence: PostgresReviewPersistence
    redis_client: Redis
    event_store: ReviewEventStore
    review_agent: ReviewOrchestratorAgent

    def warmup(self) -> None:
        self.review_agent.warmup()

    def close(self) -> None:
        try:
            self.review_agent.close()
        finally:
            try:
                self.persistence.dispose()
            finally:
                self.redis_client.close()


_WORKER_RUNTIME: ReviewWorkerRuntime | None = None
_WORKER_RUNTIME_LOCK = Lock()


def build_application_runtime(settings: Settings) -> ApplicationRuntime:
    if not settings.database_url:
        raise RuntimeError("REVIEW_AGENT_DATABASE_URL is required")
    redis_client = Redis.from_url(settings.redis_url)
    persistence = None
    review_agent = None
    try:
        redis_client.ping()
        notifier = RedisEventNotifier(redis_client)
        persistence = PostgresReviewPersistence(
            settings.database_url,
            settings.upload_dir,
            event_notifier=notifier,
        )
        event_store = ReviewEventStore(persistence)
        review_agent = build_postgres_review_agent(
            event_store,
            persistence,
            settings,
            include_retriever=False,
        )
        task_queue = ReviewTaskQueue(
            settings.redis_url,
            settings.rq_queue,
            job_timeout_seconds=settings.rq_job_timeout_seconds,
            status_reserve_seconds=settings.rq_status_reserve_seconds,
            redis_client=redis_client,
        )
        return ApplicationRuntime(
            persistence=persistence,
            redis_client=redis_client,
            event_store=event_store,
            persistent_event_stream=PersistentEventStream(
                persistence.event_repository,
                notifier,
            ),
            task_queue=task_queue,
            queued_review_service=QueuedReviewService(event_store, task_queue),
            review_agent=review_agent,
        )
    except Exception:
        _close_safely(review_agent)
        _close_safely(persistence, method_name="dispose")
        _close_safely(redis_client)
        raise


def initialize_review_worker_runtime(settings: Settings) -> ReviewWorkerRuntime:
    global _WORKER_RUNTIME
    with _WORKER_RUNTIME_LOCK:
        if _WORKER_RUNTIME is not None:
            return _WORKER_RUNTIME
        runtime = _build_review_worker_runtime(settings)
        try:
            runtime.warmup()
        except Exception:
            runtime.close()
            raise
        _WORKER_RUNTIME = runtime
        return runtime


def shutdown_review_worker_runtime() -> None:
    global _WORKER_RUNTIME
    with _WORKER_RUNTIME_LOCK:
        runtime = _WORKER_RUNTIME
        _WORKER_RUNTIME = None
    if runtime is not None:
        runtime.close()


def _build_review_worker_runtime(settings: Settings) -> ReviewWorkerRuntime:
    if not settings.database_url:
        raise RuntimeError("REVIEW_AGENT_DATABASE_URL is required by the RQ worker")
    redis_client = Redis.from_url(settings.redis_url)
    persistence = None
    review_agent = None
    try:
        redis_client.ping()
        persistence = PostgresReviewPersistence(
            settings.database_url,
            settings.upload_dir,
            event_notifier=RedisEventNotifier(redis_client),
        )
        event_store = ReviewEventStore(persistence)
        review_agent = build_postgres_review_agent(
            event_store,
            persistence,
            settings,
            include_retriever=True,
        )
        return ReviewWorkerRuntime(
            settings=settings,
            persistence=persistence,
            redis_client=redis_client,
            event_store=event_store,
            review_agent=review_agent,
        )
    except Exception:
        _close_safely(review_agent)
        _close_safely(persistence, method_name="dispose")
        _close_safely(redis_client)
        raise


def build_postgres_review_agent(
    event_store: ReviewEventStore,
    persistence: PostgresReviewPersistence,
    settings: Settings,
    *,
    include_retriever: bool,
) -> ReviewOrchestratorAgent:
    checkpoint_manager = ReviewCheckpointManager.postgres(settings.database_url)
    related_clause_retriever = None
    memory_store = None
    try:
        embedding_provider = BGEEmbeddingProvider(settings)
        if include_retriever:
            related_clause_retriever = PostgresHybridClauseRetriever(
                persistence.engine,
                Redis.from_url(settings.redis_url),
                settings,
                embedding_provider=embedding_provider,
            )
        memory_store = LangGraphPostgresMemoryStore.postgres(
            settings.database_url,
            settings,
            embedding_provider=embedding_provider,
        )
        return ReviewOrchestratorAgent(
            event_store,
            node_timeout_seconds=settings.node_timeout_seconds,
            llm_max_concurrency=settings.llm_max_concurrency,
            checkpoint_manager=checkpoint_manager,
            related_clause_retriever=related_clause_retriever,
            memory_store=memory_store,
        )
    except Exception:
        resources = (
            related_clause_retriever,
            memory_store,
            checkpoint_manager,
        )
        for resource in resources:
            _close_safely(resource)
        raise


def _close_safely(resource, *, method_name: str = "close") -> None:
    if resource is None:
        return
    try:
        getattr(resource, method_name)()
    except Exception:
        return
