import os
from time import perf_counter

from redis import Redis
from rq import Queue, SimpleWorker

from config import Settings
from db.errors import RecoveryError
from db.postgres_persistence import PostgresReviewPersistence
from services.event_notification import RedisEventNotifier
from models.review import ReviewStatus
from services.application_runtime import build_postgres_review_agent
from services.application_runtime import (
    ReviewWorkerRuntime,
    initialize_review_worker_runtime,
    shutdown_review_worker_runtime,
)
from services.event_service import ReviewEventStore, TERMINAL_STATUSES
from services.observability_service import (
    configure_observability,
    shutdown_observability,
)
from services.review_service import ReviewOrchestratorAgent
from services.runtime_log_service import configure_runtime_logging, write_runtime_log
from services.task_queue import (
    ReviewTaskQueue,
    WorkerHeartbeat,
)


def execute_review_job(task_id: str) -> dict:
    settings = Settings()
    configure_runtime_logging(
        settings.runtime_log_file,
        settings.runtime_log_level,
    )
    configure_observability(settings)
    event_store = None
    try:
        runtime = initialize_review_worker_runtime(settings)
        persistence = runtime.persistence
        event_store = runtime.event_store
        state = event_store.get_task(task_id)
        if state is None:
            raise ValueError(f"task not found: {task_id}")
        if state.status in TERMINAL_STATUSES:
            return {
                "task_id": task_id,
                "status": state.status.value,
                "executed": False,
            }
        content = persistence.load_upload(task_id)
        write_runtime_log(
            "rq_job_started",
            task_id=task_id,
            queue=settings.rq_queue,
            job_timeout_seconds=settings.rq_job_timeout_seconds,
        )
        runtime.review_agent.run(task_id, content)
        final_state = event_store.get_task(task_id)
        if final_state is None:
            raise RuntimeError(f"task disappeared after RQ execution: {task_id}")
        write_runtime_log(
            "rq_job_finished",
            task_id=task_id,
            status=final_state.status.value,
        )
        return {
            "task_id": task_id,
            "status": final_state.status.value,
            "executed": True,
        }
    except Exception as exc:
        try:
            _record_worker_failure(event_store, task_id, exc)
        except Exception as persistence_error:
            write_runtime_log(
                "rq_job_failure_persistence_failed",
                task_id=task_id,
                error_type=persistence_error.__class__.__name__,
            )
        raise


def main() -> int:
    settings = Settings()
    configure_runtime_logging(
        settings.runtime_log_file,
        settings.runtime_log_level,
    )
    redis_client = Redis.from_url(settings.redis_url)
    redis_client.ping()
    queue = Queue(settings.rq_queue, connection=redis_client)
    worker = SimpleWorker(
        [queue],
        connection=redis_client,
        name=f"review-agent-worker-{os.getpid()}",
    )
    heartbeat = WorkerHeartbeat(redis_client, settings.rq_queue)
    try:
        runtime_started = perf_counter()
        runtime = initialize_review_worker_runtime(settings)
        write_runtime_log(
            "worker_runtime_ready",
            warmup_ms=round((perf_counter() - runtime_started) * 1000),
            embedding_model=settings.bge_embedding_model,
            reranker_model=settings.bge_reranker_model,
        )
        _recover_pending_jobs(
            settings,
            redis_client,
            runtime=runtime,
        )
        heartbeat.start()
        worker.work(with_scheduler=False)
    finally:
        heartbeat.stop()
        try:
            shutdown_review_worker_runtime()
        finally:
            try:
                shutdown_observability()
            finally:
                redis_client.close()
    return 0


def _build_persistence(settings: Settings) -> PostgresReviewPersistence:
    if not settings.database_url:
        raise RuntimeError("REVIEW_AGENT_DATABASE_URL is required by the RQ worker")
    redis_client = Redis.from_url(settings.redis_url)
    return PostgresReviewPersistence(
        settings.database_url,
        settings.upload_dir,
        event_notifier=RedisEventNotifier(redis_client),
    )


def _build_agent(
    event_store: ReviewEventStore,
    persistence: PostgresReviewPersistence,
    settings: Settings,
) -> ReviewOrchestratorAgent:
    return build_postgres_review_agent(
        event_store,
        persistence,
        settings,
        include_retriever=True,
    )


def _recover_pending_jobs(
    settings: Settings,
    redis_client: Redis,
    *,
    runtime: ReviewWorkerRuntime | None = None,
) -> list[str]:
    owns_persistence = runtime is None
    persistence = runtime.persistence if runtime is not None else _build_persistence(settings)
    try:
        event_store = (
            runtime.event_store
            if runtime is not None
            else ReviewEventStore(persistence)
        )
        event_store.load_persisted(clear_execution_leases=True)
        task_queue = ReviewTaskQueue(
            settings.redis_url,
            settings.rq_queue,
            job_timeout_seconds=settings.rq_job_timeout_seconds,
            status_reserve_seconds=settings.rq_status_reserve_seconds,
            redis_client=redis_client,
        )
        recovered_task_ids = []
        for state in event_store.pending_tasks():
            if state.status == ReviewStatus.CANCEL_REQUESTED:
                event_store.finalize_cancel(state.task_id)
                continue
            try:
                persistence.load_upload(state.task_id)
                event_store.record_recovery(state.task_id)
                task_queue.enqueue(state.task_id, replace_existing=True)
            except RecoveryError as exc:
                event_store.update_task(
                    state.task_id,
                    ReviewStatus.NEED_MANUAL_REVIEW,
                    str(exc),
                    step_name="recovery_blocked",
                )
                continue
            recovered_task_ids.append(state.task_id)
        if recovered_task_ids:
            write_runtime_log(
                "rq_pending_jobs_recovered",
                task_ids=recovered_task_ids,
                count=len(recovered_task_ids),
            )
        return recovered_task_ids
    finally:
        if owns_persistence:
            persistence.dispose()


def _record_worker_failure(
    event_store: ReviewEventStore | None,
    task_id: str,
    error: Exception,
) -> None:
    if event_store is None:
        return
    state = event_store.get_task(task_id)
    if state is None or state.status in TERMINAL_STATUSES:
        return
    reason = str(error).strip() or error.__class__.__name__
    event_store.update_task(
        task_id,
        ReviewStatus.TASK_ERROR,
        f"RQ worker failed before review completed: {reason[:300]}",
        step_name="rq_job_failed",
    )


if __name__ == "__main__":
    raise SystemExit(main())
