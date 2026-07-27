from contextlib import contextmanager, nullcontext
from dataclasses import replace
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
from api.dependencies import require_accepting_tasks
from api.errors import ApiError
from api.routes.health import health
from config import Settings
from main import create_app
from models.review import (
    LLMMode,
    ReviewPosition,
    ReviewStatus,
    TaskCancelledError,
)
from services.checkpoint_service import ReviewCheckpointManager
from services.application_runtime import (
    initialize_review_worker_runtime,
    shutdown_review_worker_runtime,
)
from services.event_notification import PersistentEventStream, RedisEventNotifier
from services.event_service import ReviewEventStore
from services.task_queue import (
    QueueDependencyError,
    QueuedReviewService,
    ReviewTaskQueue,
    WorkerHeartbeat,
)
from workers.review_worker import _build_agent, _record_worker_failure


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


class ExecutionControlRefreshTest(unittest.TestCase):
    def test_local_executor_uses_control_row_until_external_event_changes(self):
        store = ReviewEventStore()
        created = store.create_task(
            "control.pdf",
            "pdf",
            ReviewPosition.PARTY_A,
            content=b"%PDF-control",
            llm_mode=LLMMode.LOCAL_STRUCTURED,
        )
        persistence = Mock()
        persistence.db_path = None
        persistence.store_key = "postgresql://control"
        persistence.task_mutation_lock.side_effect = lambda _task_id: nullcontext()
        persistence.task_repository.try_acquire_execution.return_value = True
        persistence.task_repository.release_execution.return_value = None
        persistence.load_state = None
        persistence.save_state_and_event.return_value = None
        persistence.load_task_execution_control.return_value = {
            "status": ReviewStatus.START.value,
            "execution_owner": "worker-1",
            "latest_event_id": 0,
        }
        store.persistence = persistence

        self.assertTrue(store.try_acquire_execution(created.task_id, "worker-1"))
        persistence.load_state = Mock(
            side_effect=AssertionError("full task reload is not expected")
        )
        updated = store.update_task(
            created.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "queued",
            step_name="upload_received",
        )

        self.assertEqual(updated.status, ReviewStatus.UPLOAD_RECEIVED)
        persistence.load_state.assert_not_called()

        persisted_cancelled = replace(
            updated,
            status=ReviewStatus.CANCEL_REQUESTED,
            message="cancelled elsewhere",
        )
        cancel_event = {
            "event_id": 2,
            "task_id": created.task_id,
            "status": ReviewStatus.CANCEL_REQUESTED.value,
            "message": "cancelled elsewhere",
            "step_name": "cancel_requested",
            "tool_name": "",
            "created_at": "2026-07-27T00:00:00+00:00",
        }
        persistence.load_task_execution_control.return_value = {
            "status": ReviewStatus.CANCEL_REQUESTED.value,
            "execution_owner": "worker-1",
            "latest_event_id": 2,
        }
        persistence.load_state = Mock(
            return_value=(persisted_cancelled, [*updated.events, cancel_event])
        )

        with self.assertRaises(TaskCancelledError):
            store.update_task(
                created.task_id,
                ReviewStatus.DOCUMENT_PARSED,
                "stale worker update",
                step_name="document_parsed",
            )
        persistence.load_state.assert_called_once_with(created.task_id)


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


class ApplicationDependencyHealthTest(unittest.TestCase):
    def test_postgres_outage_is_reported_before_new_tasks_are_accepted(self):
        request = Mock()
        request.app.state.runtime_dependency_error = ""
        request.app.state.accepting_tasks = True
        request.app.state.queued_review_service = Mock()
        request.app.state.runtime = Mock()
        request.app.state.runtime.persistence.is_ready.return_value = False

        response = health(
            request,
            Settings(service_name="contract-review-agent"),
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"postgresql-unavailable", response.body)
        with self.assertRaises(ApiError) as raised:
            require_accepting_tasks(request)
        self.assertEqual(raised.exception.status_code, 503)
        request.app.state.runtime.task_queue.require_ready.assert_not_called()


class ReviewWorkerTest(unittest.TestCase):
    def tearDown(self):
        shutdown_review_worker_runtime()

    def test_worker_runtime_is_warmed_once_and_reused(self):
        settings = Settings(database_url="postgresql+psycopg://local/test")
        runtime = Mock()

        with patch(
            "services.application_runtime._build_review_worker_runtime",
            return_value=runtime,
        ) as build_runtime:
            first = initialize_review_worker_runtime(settings)
            second = initialize_review_worker_runtime(settings)

        self.assertIs(first, runtime)
        self.assertIs(second, runtime)
        build_runtime.assert_called_once_with(settings)
        runtime.warmup.assert_called_once_with()

    def test_worker_agent_has_node_timeout_but_no_application_task_timer(self):
        settings = Settings(
            node_timeout_seconds=90,
            rq_job_timeout_seconds=7200,
            rq_status_reserve_seconds=120,
            database_url="postgresql+psycopg://unused",
        )

        checkpoint_manager = ReviewCheckpointManager.in_memory()
        persistence = Mock()
        persistence.engine.dialect.name = "postgresql"
        with patch.object(
            ReviewCheckpointManager,
            "postgres",
            return_value=checkpoint_manager,
        ), patch(
            "services.application_runtime.PostgresHybridClauseRetriever"
        ) as retriever_type, patch(
            "services.application_runtime.LangGraphPostgresMemoryStore.postgres"
        ) as memory_store_factory:
            agent = _build_agent(Mock(), persistence, settings)

        self.assertEqual(agent.node_timeout_seconds, 90)
        self.assertEqual(agent.checkpoint_backend, "memory")
        retriever_type.assert_called_once()
        memory_store_factory.assert_called_once()
        self.assertFalse(hasattr(agent, "task_timeout_seconds"))
        self.assertFalse(hasattr(agent, "deepseek_task_timeout_seconds"))
        agent.close()

    def test_retriever_construction_failure_closes_checkpoint_manager(self):
        settings = Settings(database_url="postgresql+psycopg://local/test")
        checkpoint_manager = Mock()
        persistence = Mock()
        persistence.engine.dialect.name = "postgresql"

        with patch.object(
            ReviewCheckpointManager,
            "postgres",
            return_value=checkpoint_manager,
        ), patch(
            "services.application_runtime.PostgresHybridClauseRetriever",
            side_effect=RuntimeError("retriever unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "retriever unavailable"):
                _build_agent(Mock(), persistence, settings)

        checkpoint_manager.close.assert_called_once()

    def test_memory_store_construction_failure_closes_prior_resources(self):
        settings = Settings(database_url="postgresql+psycopg://local/test")
        checkpoint_manager = Mock()
        retriever = Mock()
        retriever.close.side_effect = RuntimeError("retriever close failed")
        persistence = Mock()
        persistence.engine.dialect.name = "postgresql"

        with patch.object(
            ReviewCheckpointManager,
            "postgres",
            return_value=checkpoint_manager,
        ), patch(
            "services.application_runtime.PostgresHybridClauseRetriever",
            return_value=retriever,
        ), patch(
            "services.application_runtime.LangGraphPostgresMemoryStore.postgres",
            side_effect=RuntimeError("memory store unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "memory store unavailable",
            ):
                _build_agent(Mock(), persistence, settings)

        retriever.close.assert_called_once()
        checkpoint_manager.close.assert_called_once()

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

    def test_cross_process_cancel_refreshes_state_before_worker_update(self):
        state = self.store.create_task(
            "cancel.pdf",
            "pdf",
            ReviewPosition.PARTY_A,
            content=b"%PDF-1.4\ncancel",
            llm_mode=LLMMode.LOCAL_STRUCTURED,
        )
        self.created_task_id = state.task_id
        self.store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "queued",
            step_name="upload_received",
        )
        execution_owner = "integration-worker"
        self.assertTrue(
            self.store.try_acquire_execution(state.task_id, execution_owner)
        )
        second_store = ReviewEventStore(self.persistence)
        second_store.load_persisted(clear_execution_leases=False)

        cancelled = second_store.request_cancel(state.task_id, "integration")

        self.assertEqual(cancelled.status, ReviewStatus.CANCEL_REQUESTED)
        with self.assertRaises(TaskCancelledError):
            self.store.update_task(
                state.task_id,
                ReviewStatus.DOCUMENT_PARSED,
                "stale worker update",
                step_name="document_parsed",
            )
        persisted = self.persistence.load_state(state.task_id)
        self.assertIsNotNone(persisted)
        persisted_state, events = persisted
        self.assertEqual(
            persisted_state.status,
            ReviewStatus.CANCEL_REQUESTED,
        )
        self.assertEqual([event["event_id"] for event in events], [0, 1, 2])
        self.store.release_execution(state.task_id, execution_owner)


if __name__ == "__main__":
    unittest.main()
