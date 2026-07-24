from contextlib import contextmanager
import os
from pathlib import Path
import sys
from threading import Thread
from time import sleep
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from redis import Redis
from rq.exceptions import NoSuchJobError
from sqlalchemy import delete


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from db.postgres_models import ReviewTaskRow
from db.postgres_persistence import PostgresReviewPersistence
from config import Settings
from main import create_app
from models.review import LLMMode, ReviewPosition, ReviewStatus
from services.event_notification import PersistentEventStream, RedisEventNotifier
from services.event_service import ReviewEventStore
from services.task_queue import (
    QueueDependencyError,
    QueuedReviewService,
    ReviewTaskQueue,
    WorkerHeartbeat,
)
from workers.review_worker import _execution_timeout, _record_worker_failure


class _Subscription:
    def __init__(self, on_wait=None):
        self.on_wait = on_wait
        self.wait_count = 0
        self.closed = False

    def wait(self, timeout_seconds: float) -> bool:
        self.wait_count += 1
        if self.on_wait is not None:
            self.on_wait()
        return True

    def close(self) -> None:
        self.closed = True


class _Notifier:
    def __init__(self, subscription: _Subscription):
        self.subscription = subscription
        self.subscribed = False

    def publish(self, task_id: str, event_id: int) -> bool:
        return True

    @contextmanager
    def subscribe(self, task_id: str):
        self.subscribed = True
        try:
            yield self.subscription
        finally:
            self.subscription.close()


class _EventRepository:
    def __init__(self):
        self.events = []
        self.queries = []

    def task_exists(self, task_id: str) -> bool:
        return task_id == "task_1"

    def list_after(self, task_id: str, next_event_id: int) -> list[dict]:
        self.queries.append((task_id, next_event_id))
        return [
            event for event in self.events if event["event_id"] >= next_event_id
        ]


class PersistentEventStreamTest(unittest.TestCase):
    def test_subscribes_before_first_query_and_requeries_after_notification(self):
        repository = _EventRepository()
        subscription = _Subscription(
            on_wait=lambda: repository.events.append(
                {"task_id": "task_1", "event_id": 4}
            )
        )
        notifier = _Notifier(subscription)
        stream = PersistentEventStream(repository, notifier)

        events = stream.wait_for_events("task_1", 4, 1.0)

        self.assertTrue(notifier.subscribed)
        self.assertEqual(repository.queries, [("task_1", 4), ("task_1", 4)])
        self.assertEqual(events, [{"task_id": "task_1", "event_id": 4}])
        self.assertEqual(subscription.wait_count, 1)
        self.assertTrue(subscription.closed)

    def test_existing_events_return_without_waiting(self):
        repository = _EventRepository()
        repository.events.append({"task_id": "task_1", "event_id": 2})
        subscription = _Subscription()
        stream = PersistentEventStream(repository, _Notifier(subscription))

        events = stream.wait_for_events("task_1", 2, 1.0)

        self.assertEqual(events[0]["event_id"], 2)
        self.assertEqual(subscription.wait_count, 0)


class PersistentSseRouteTest(unittest.TestCase):
    def test_sse_uses_configured_persistent_event_source(self):
        event = {
            "event_id": 7,
            "task_id": "task_cross_process",
            "status": ReviewStatus.EVIDENCE_VERIFIED.value,
            "message": "done",
            "step_name": "evidence_verified",
            "tool_name": "verify_evidence",
            "created_at": "2026-07-24T00:00:00+00:00",
            "task": {"task_id": "task_cross_process"},
        }
        persistent_stream = Mock()
        persistent_stream.task_exists.return_value = True
        persistent_stream.wait_for_events.return_value = [event]
        app = create_app(
            Settings(
                llm_mode="local_structured",
                memory_db_path=":memory:",
                runtime_log_file=os.devnull,
            ),
            ReviewEventStore(),
            persistent_stream,
        )

        with TestClient(app) as client:
            with client.stream(
                "GET",
                "/api/tasks/task_cross_process/events?after_event_id=6",
            ) as response:
                body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("id: 7", body)
        self.assertIn('"status": "EVIDENCE_VERIFIED"', body)
        persistent_stream.task_exists.assert_called_once_with("task_cross_process")
        persistent_stream.wait_for_events.assert_called_once_with(
            "task_cross_process",
            7,
            1.0,
        )


class ReviewTaskQueueTest(unittest.TestCase):
    def setUp(self):
        self.redis = Mock()
        self.redis.ping.return_value = True
        self.redis.exists.return_value = True
        self.queue = ReviewTaskQueue(
            "redis://unused",
            "review-agent",
            job_timeout_seconds=7200,
            status_reserve_seconds=120,
            redis_client=self.redis,
        )

    @patch("services.task_queue.Worker.all", return_value=[Mock()])
    def test_enqueues_stable_job_without_rq_retry(self, _workers):
        job = Mock()
        self.queue.queue.enqueue_call = Mock(return_value=job)

        with patch(
            "services.task_queue.Job.fetch",
            side_effect=NoSuchJobError,
        ):
            result = self.queue.enqueue("task_123")

        self.assertIs(result, job)
        call = self.queue.queue.enqueue_call.call_args.kwargs
        self.assertEqual(call["func"], "workers.review_worker.execute_review_job")
        self.assertEqual(call["job_id"], "review-task_123")
        self.assertEqual(call["timeout"], 7200)
        self.assertIsNone(call["retry"])
        self.assertEqual(call["meta"]["status_reserve_seconds"], 120)

    @patch("services.task_queue.Worker.all", return_value=[])
    def test_rejects_new_work_when_no_worker_is_registered(self, _workers):
        with self.assertRaisesRegex(QueueDependencyError, "no active worker"):
            self.queue.require_ready()

    @patch("services.task_queue.Worker.all", return_value=[Mock()])
    def test_rejects_stale_rq_registration_without_heartbeat(self, _workers):
        self.redis.exists.return_value = False

        with self.assertRaisesRegex(QueueDependencyError, "no active worker"):
            self.queue.require_ready()

    def test_rejects_invalid_timeout_reserve(self):
        with self.assertRaisesRegex(ValueError, "reserve"):
            ReviewTaskQueue(
                "redis://unused",
                "review-agent",
                job_timeout_seconds=120,
                status_reserve_seconds=120,
                redis_client=self.redis,
            )


class WorkerHeartbeatTest(unittest.TestCase):
    def test_start_refreshes_and_stop_deletes_only_own_lease(self):
        redis_client = Mock()
        heartbeat = WorkerHeartbeat(
            redis_client,
            "review-agent",
            ttl_seconds=3,
            refresh_seconds=1,
        )

        heartbeat.start()
        heartbeat.stop()

        redis_client.set.assert_called_with(
            "review-agent:worker-heartbeat:review-agent",
            heartbeat.token,
            ex=3,
        )
        self.assertEqual(redis_client.eval.call_args.args[1:], (
            1,
            "review-agent:worker-heartbeat:review-agent",
            heartbeat.token,
        ))


class QueuedReviewServiceTest(unittest.TestCase):
    def test_dependency_check_happens_before_task_creation(self):
        event_store = Mock()
        event_store.persistence = object()
        task_queue = Mock()
        task_queue.require_ready.side_effect = QueueDependencyError("offline")
        service = QueuedReviewService(event_store, task_queue)

        with self.assertRaises(QueueDependencyError):
            service.start(
                file_name="sample.pdf",
                file_type="pdf",
                content=b"%PDF-test",
                review_position=ReviewPosition.PARTY_A,
                llm_mode=LLMMode.LOCAL_STRUCTURED,
            )

        event_store.create_task.assert_not_called()


class ReviewWorkerTest(unittest.TestCase):
    def test_execution_timeout_preserves_status_flush_window(self):
        settings = Settings(
            rq_job_timeout_seconds=7200,
            rq_status_reserve_seconds=120,
        )

        self.assertEqual(_execution_timeout(settings), 7080)

    def test_pre_execution_failure_is_persisted_as_task_error(self):
        state = Mock()
        state.status = ReviewStatus.UPLOAD_RECEIVED
        event_store = Mock()
        event_store.get_task.return_value = state

        _record_worker_failure(
            event_store,
            "task_1",
            RuntimeError("upload missing"),
        )

        event_store.update_task.assert_called_once_with(
            "task_1",
            ReviewStatus.TASK_ERROR,
            "RQ worker failed before review completed: upload missing",
            step_name="rq_job_failed",
        )


@unittest.skipUnless(
    os.getenv("REVIEW_AGENT_RUN_EXTERNAL_TESTS") == "1",
    "external PostgreSQL/Redis integration is opt-in",
)
class PostgresRedisIntegrationTest(unittest.TestCase):
    def setUp(self):
        database_url = os.environ["REVIEW_AGENT_DATABASE_URL"]
        redis_url = os.environ["REVIEW_AGENT_REDIS_URL"]
        upload_dir = os.environ["REVIEW_AGENT_UPLOAD_DIR"]
        self.redis = Redis.from_url(redis_url)
        self.notifier = RedisEventNotifier(self.redis)
        self.persistence = PostgresReviewPersistence(
            database_url,
            upload_dir,
            event_notifier=self.notifier,
        )
        self.store = ReviewEventStore(self.persistence)
        self.created_task_id = ""

    def tearDown(self):
        if self.created_task_id:
            with self.persistence.engine.begin() as connection:
                connection.execute(
                    delete(ReviewTaskRow).where(
                        ReviewTaskRow.task_id == self.created_task_id
                    )
                )
            for suffix in (".pdf", ".pdf.tmp"):
                (
                    Path(os.environ["REVIEW_AGENT_UPLOAD_DIR"])
                    / f"{self.created_task_id}{suffix}"
                ).unlink(missing_ok=True)
        self.persistence.dispose()

    def test_committed_events_wake_cross_process_reader(self):
        state = self.store.create_task(
            "integration.pdf",
            "pdf",
            ReviewPosition.PARTY_A,
            content=b"%PDF-1.4\nintegration",
            llm_mode=LLMMode.LOCAL_STRUCTURED,
        )
        self.created_task_id = state.task_id
        stream = PersistentEventStream(
            self.persistence.event_repository,
            self.notifier,
        )
        result = []

        reader = Thread(
            target=lambda: result.extend(
                stream.wait_for_events(state.task_id, 1, 3.0)
            )
        )
        reader.start()
        sleep(0.2)
        self.store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "queued",
            step_name="upload_received",
        )
        reader.join(timeout=5.0)

        self.assertFalse(reader.is_alive())
        self.assertEqual([event["event_id"] for event in result], [1])
        persisted = self.persistence.event_repository.list_for_task(state.task_id)
        self.assertEqual([event["event_id"] for event in persisted], [0, 1])
        self.assertEqual(persisted[-1]["status"], ReviewStatus.UPLOAD_RECEIVED.value)


if __name__ == "__main__":
    unittest.main()
