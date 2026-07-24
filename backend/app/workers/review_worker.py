import os

from redis import Redis
from rq import Queue, SimpleWorker

from config import Settings
from db.postgres_persistence import PostgresReviewPersistence
from services.event_notification import RedisEventNotifier
from models.review import ReviewStatus
from services.event_service import ReviewEventStore, TERMINAL_STATUSES
from services.review_service import ReviewOrchestratorAgent
from services.runtime_log_service import configure_runtime_logging, write_runtime_log
from services.task_queue import WorkerHeartbeat


def execute_review_job(task_id: str) -> dict:
    settings = Settings()
    configure_runtime_logging(
        settings.runtime_log_file,
        settings.runtime_log_level,
    )
    persistence = _build_persistence(settings)
    event_store = None
    try:
        event_store = ReviewEventStore(persistence)
        event_store.load_persisted(clear_execution_leases=False)
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
        agent = ReviewOrchestratorAgent(
            event_store,
            node_timeout_seconds=settings.node_timeout_seconds,
            task_timeout_seconds=_execution_timeout(settings),
            deepseek_task_timeout_seconds=_execution_timeout(settings),
            llm_max_concurrency=settings.llm_max_concurrency,
        )
        write_runtime_log(
            "rq_job_started",
            task_id=task_id,
            queue=settings.rq_queue,
            job_timeout_seconds=settings.rq_job_timeout_seconds,
        )
        agent.run(task_id, content)
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
    finally:
        persistence.dispose()


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
    heartbeat.start()
    try:
        worker.work(with_scheduler=False)
    finally:
        heartbeat.stop()
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


def _execution_timeout(settings: Settings) -> int:
    timeout = (
        settings.rq_job_timeout_seconds
        - settings.rq_status_reserve_seconds
    )
    if timeout <= 0:
        raise RuntimeError("RQ status reserve leaves no execution time")
    return timeout


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
