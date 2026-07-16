import json
import sqlite3

from db.sqlite import connect


class EventRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def append(self, connection: sqlite3.Connection, event: dict) -> None:
        connection.execute(
            """
            INSERT INTO task_events (
                task_id, event_id, status, message, step_name, tool_name,
                created_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event["task_id"]),
                int(event["event_id"]),
                str(event["status"]),
                str(event["message"]),
                str(event.get("step_name", "")),
                str(event.get("tool_name", "")),
                str(event["created_at"]),
                json.dumps(event, ensure_ascii=False, sort_keys=True),
            ),
        )

    def list_for_task(self, task_id: str) -> list[dict]:
        connection = connect(self.db_path)
        try:
            rows = connection.execute(
                """
                SELECT payload_json FROM task_events
                WHERE task_id = ? ORDER BY event_id
                """,
                (task_id,),
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]
        finally:
            connection.close()
