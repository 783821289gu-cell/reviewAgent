from dataclasses import dataclass
from threading import Event, Thread
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Worker
from rq.exceptions import NoSuchJobError
from rq.job import Job

from models.review import AgentState, LLMMode, ReviewPosition, ReviewStatus
from services.event_service import ReviewEventStore


class QueueDependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueueHealth:
    redis_ready: bool
    worker_count: int
    queue_name: str


class ReviewTaskQueue:
    def __init__(
        self,
        redis_url: str,
        queue_name: str,
        *,
        job_timeout_seconds: int,
        status_reserve_seconds: int,
        redis_client: Redis | None = None,
    ):
        if not str(redis_url).strip():
            raise ValueError("Redis URL is required")
        if not str(queue_name).strip():
            raise ValueError("RQ queue name is required")
        if job_timeout_seconds <= 0 or status_reserve_seconds <= 0:
            raise ValueError("RQ timeout values must be positive")
        if status_reserve_seconds >= job_timeout_seconds:
            raise ValueError("RQ status reserve must be smaller than the job timeout")
        self.redis = redis_client or Redis.from_url(redis_url)
        self.queue = Queue(str(queue_name).strip(), connection=self.redis)
        self.job_timeout_seconds = int(job_timeout_seconds)
        self.status_reserve_seconds = int(status_reserve_seconds)

    def health(self) -> QueueHealth:
        try:
            redis_ready = bool(self.redis.ping())
            workers = Worker.all(connection=self.redis, queue=self.queue)
            heartbeat_ready = bool(
                self.redis.exists(_worker_heartbeat_key(self.queue.name))
            )
        except RedisError as exc:
            raise QueueDependencyError("Redis is unavailable") from exc
        return QueueHealth(
            redis_ready=redis_ready,
            worker_count=1 if workers and heartbeat_ready else 0,
            queue_name=self.queue.name,
        )

    def require_ready(self) -> QueueHealth:
        health = self.health()
        if not health.redis_ready:
            raise QueueDependencyError("Redis ping did not return a ready state")
        if health.worker_count < 1:
            raise QueueDependencyError(
                f"RQ queue {health.queue_name!r} has no active worker"
            )
        return health

    def enqueue(self, task_id: str) -> Job:
        normalized_task_id = str(task_id).strip()
        if not normalized_task_id:
            raise ValueError("task_id is required")
        job_id = f"review-{normalized_task_id}"
        existing = self._existing_job(job_id)
        if existing is not None:
            return existing
        try:
            return self.queue.enqueue_call(
                func="workers.review_worker.execute_review_job",
                args=(normalized_task_id,),
                timeout=self.job_timeout_seconds,
                result_ttl=86400,
                failure_ttl=604800,
                job_id=job_id,
                retry=None,
                meta={
                    "task_id": normalized_task_id,
                    "status_reserve_seconds": self.status_reserve_seconds,
                },
            )
        except RedisError as exc:
            raise QueueDependencyError("failed to enqueue review task") from exc

    def _existing_job(self, job_id: str) -> Job | None:
        try:
            return Job.fetch(job_id, connection=self.redis)
        except NoSuchJobError:
            return None
        except RedisError as exc:
            raise QueueDependencyError("failed to inspect RQ job state") from exc


class WorkerHeartbeat:
    def __init__(
        self,
        redis_client: Redis,
        queue_name: str,
        *,
        ttl_seconds: int = 15,
        refresh_seconds: int = 5,
    ):
        if ttl_seconds <= 0 or refresh_seconds <= 0:
            raise ValueError("worker heartbeat intervals must be positive")
        if refresh_seconds >= ttl_seconds:
            raise ValueError("worker heartbeat refresh must be shorter than its TTL")
        self.redis = redis_client
        self.key = _worker_heartbeat_key(queue_name)
        self.ttl_seconds = ttl_seconds
        self.refresh_seconds = refresh_seconds
        self.token = uuid4().hex
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker heartbeat is already running")
        self._refresh()
        self._thread = Thread(
            target=self._run,
            name="rq-worker-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.refresh_seconds + 1)
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('del', KEYS[1]) else return 0 end"
        )
        self.redis.eval(script, 1, self.key, self.token)

    def _run(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            try:
                self._refresh()
            except RedisError:
                continue

    def _refresh(self) -> None:
        self.redis.set(self.key, self.token, ex=self.ttl_seconds)


class QueuedReviewService:
    def __init__(
        self,
        event_store: ReviewEventStore,
        task_queue: ReviewTaskQueue,
    ):
        if event_store.persistence is None:
            raise ValueError("queued review requires persistent task storage")
        self.event_store = event_store
        self.task_queue = task_queue

    def start(
        self,
        *,
        file_name: str,
        file_type: str,
        content: bytes,
        review_position: ReviewPosition,
        llm_mode: LLMMode,
    ) -> AgentState:
        self.task_queue.require_ready()
        state = self.event_store.create_task(
            file_name,
            file_type,
            review_position,
            content=content,
            llm_mode=llm_mode,
        )
        state = self.event_store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "Upload persisted; review is waiting for the RQ worker.",
            step_name="upload_received",
        )
        try:
            self.task_queue.enqueue(state.task_id)
        except Exception as exc:
            self.event_store.update_task(
                state.task_id,
                ReviewStatus.TASK_ERROR,
                f"Review task could not be queued: {exc}",
                step_name="queue_failed",
            )
            raise
        return state


def _worker_heartbeat_key(queue_name: str) -> str:
    return f"review-agent:worker-heartbeat:{queue_name}"
