from contextlib import contextmanager
import json
import logging
from time import monotonic
from typing import Iterator, Protocol

from redis import Redis
from redis.exceptions import RedisError

from db.postgres_repositories import PostgresEventRepository


LOGGER = logging.getLogger(__name__)


class EventNotificationSubscription(Protocol):
    def wait(self, timeout_seconds: float) -> bool: ...

    def close(self) -> None: ...


class EventNotifier(Protocol):
    def publish(self, task_id: str, event_id: int) -> bool: ...

    @contextmanager
    def subscribe(
        self,
        task_id: str,
    ) -> Iterator[EventNotificationSubscription]: ...


class RedisEventNotifier:
    def __init__(
        self,
        redis_client: Redis,
        *,
        channel_prefix: str = "review-agent:task-events",
    ):
        self.redis_client = redis_client
        self.channel_prefix = channel_prefix.rstrip(":")

    def publish(self, task_id: str, event_id: int) -> bool:
        payload = json.dumps(
            {"task_id": task_id, "event_id": int(event_id)},
            separators=(",", ":"),
        )
        try:
            self.redis_client.publish(self._channel(task_id), payload)
            return True
        except RedisError:
            LOGGER.exception(
                "Redis event notification failed after PostgreSQL commit",
                extra={"task_id": task_id, "event_id": event_id},
            )
            return False

    @contextmanager
    def subscribe(
        self,
        task_id: str,
    ) -> Iterator[EventNotificationSubscription]:
        pubsub = self.redis_client.pubsub(ignore_subscribe_messages=False)
        pubsub.subscribe(self._channel(task_id))
        acknowledgement = pubsub.get_message(timeout=1.0)
        if not acknowledgement or acknowledgement.get("type") != "subscribe":
            pubsub.close()
            raise RedisError("Redis did not acknowledge the event subscription")
        subscription = _RedisSubscription(pubsub)
        try:
            yield subscription
        finally:
            subscription.close()

    def _channel(self, task_id: str) -> str:
        return f"{self.channel_prefix}:{task_id}"


class _RedisSubscription:
    def __init__(self, pubsub):
        self.pubsub = pubsub

    def wait(self, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        deadline = monotonic() + timeout_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            message = self.pubsub.get_message(timeout=remaining)
            if message is None:
                return False
            if message.get("type") == "message":
                return True

    def close(self) -> None:
        self.pubsub.close()


class PersistentEventStream:
    def __init__(
        self,
        repository: PostgresEventRepository,
        notifier: EventNotifier,
        *,
        notification_retry_seconds: float = 0.25,
    ):
        if notification_retry_seconds <= 0:
            raise ValueError("notification_retry_seconds must be positive")
        self.repository = repository
        self.notifier = notifier
        self.notification_retry_seconds = notification_retry_seconds

    def task_exists(self, task_id: str) -> bool:
        return self.repository.task_exists(task_id)

    def wait_for_events(
        self,
        task_id: str,
        next_event_id: int,
        timeout_seconds: float,
    ) -> list[dict]:
        deadline = monotonic() + max(0.0, timeout_seconds)
        try:
            with self.notifier.subscribe(task_id) as subscription:
                events = self.repository.list_after(task_id, next_event_id)
                if events:
                    return events
                remaining = deadline - monotonic()
                if remaining > 0:
                    subscription.wait(remaining)
                return self.repository.list_after(task_id, next_event_id)
        except RedisError:
            LOGGER.exception(
                "Redis event wait failed; polling PostgreSQL until the wait deadline",
                extra={"task_id": task_id, "next_event_id": next_event_id},
            )
            return self._poll_postgres(task_id, next_event_id, deadline)

    def _poll_postgres(
        self,
        task_id: str,
        next_event_id: int,
        deadline: float,
    ) -> list[dict]:
        while True:
            events = self.repository.list_after(task_id, next_event_id)
            if events or monotonic() >= deadline:
                return events
            remaining = deadline - monotonic()
            if remaining <= 0:
                return []
            from time import sleep

            sleep(min(self.notification_retry_seconds, remaining))
