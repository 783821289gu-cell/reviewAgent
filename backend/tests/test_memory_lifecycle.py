from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from services.memory_service import retrieve_memory, write_memory


class MemoryLifecycleTest(unittest.TestCase):
    def test_idempotent_episode_is_not_overwritten_by_a_repeated_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            first = write_memory(
                {
                    "db_path": db_path,
                    "idempotency_key": "feedback:stable-001",
                    "human_feedback": feedback(
                        "MEM-STABLE-001",
                        "Use the approved wording.",
                        "2026-01-01T00:00:00+00:00",
                    ),
                }
            )
            repeated_payload = feedback(
                "MEM-SHOULD-NOT-EXIST",
                "Overwrite the approved wording.",
                "2026-01-02T00:00:00+00:00",
            )
            repeated = write_memory(
                {
                    "db_path": db_path,
                    "idempotency_key": "feedback:stable-001",
                    "human_feedback": repeated_payload,
                }
            )
            preference = retrieve(db_path)[0]
            connection = sqlite3.connect(db_path)
            try:
                episode_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(repeated, first)
        self.assertEqual(episode_count, 1)
        self.assertEqual(preference["source_memory_ids"], ["MEM-STABLE-001"])
        self.assertEqual(preference["final_suggestion"], "Use the approved wording.")

    def test_repetition_increases_confidence_and_conflict_reduces_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            write(db_path, feedback("MEM-001", "Suggestion A", "2026-01-01T00:00:00+00:00"))
            first = retrieve(db_path)[0]
            write(db_path, feedback("MEM-002", "Suggestion A", "2026-01-02T00:00:00+00:00"))
            repeated = retrieve(db_path)[0]
            write(db_path, feedback("MEM-003", "Suggestion B", "2026-01-03T00:00:00+00:00"))
            conflicted = retrieve(db_path)[0]
            write(
                db_path,
                feedback(
                    "MEM-004",
                    "Suggestion B",
                    "2026-01-04T00:00:00+00:00",
                    action="ignore",
                ),
            )
            opposed = retrieve(db_path)[0]
            connection = sqlite3.connect(db_path)
            try:
                episode_count = connection.execute(
                    "SELECT COUNT(*) FROM memory_items"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertGreater(repeated["confidence"], first["confidence"])
        self.assertLess(conflicted["confidence"], repeated["confidence"])
        self.assertLess(opposed["confidence"], conflicted["confidence"])
        self.assertEqual(conflicted["conflict_status"], "conflicted")
        self.assertFalse(conflicted["can_influence_suggestion"])
        self.assertEqual(opposed["support_count"], 3)
        self.assertEqual(opposed["opposition_count"], 1)
        self.assertEqual(len(opposed["source_memory_ids"]), 4)
        self.assertEqual(episode_count, 4)
        support_suggestions = {
            item["final_suggestion"]
            for item in opposed["variants"]
            if item["stance"] == "support"
        }
        self.assertEqual(support_suggestions, {"Suggestion A", "Suggestion B"})

    def test_expiry_lowers_only_the_derived_preference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            write(db_path, feedback("MEM-OLD", "Old suggestion", "2025-01-01T00:00:00+00:00"))
            active = retrieve_memory(
                {
                    **retrieve_input(db_path),
                    "now": "2025-01-02T00:00:00+00:00",
                    "stale_after_days": 365,
                }
            )[0]
            expired = retrieve_memory(
                {
                    **retrieve_input(db_path),
                    "now": "2026-01-03T00:00:00+00:00",
                    "stale_after_days": 365,
                }
            )[0]
            connection = sqlite3.connect(db_path)
            try:
                episode = connection.execute(
                    "SELECT memory_id, final_suggestion FROM memory_items"
                ).fetchone()
                preference = connection.execute(
                    "SELECT lifecycle_status, confidence, last_used_at "
                    "FROM semantic_preferences"
                ).fetchone()
            finally:
                connection.close()

            write(
                db_path,
                feedback(
                    "MEM-FRESH",
                    "Old suggestion",
                    "2026-01-03T00:00:00+00:00",
                ),
            )
            reactivated = retrieve_memory(
                {
                    **retrieve_input(db_path),
                    "now": "2026-01-04T00:00:00+00:00",
                    "stale_after_days": 365,
                }
            )[0]

        self.assertEqual(active["lifecycle_status"], "active")
        self.assertTrue(expired["expired"])
        self.assertEqual(expired["lifecycle_status"], "expired")
        self.assertEqual(expired["confidence"], 0.275)
        self.assertFalse(expired["can_influence_suggestion"])
        self.assertEqual(tuple(episode), ("MEM-OLD", "Old suggestion"))
        self.assertEqual(preference[0], "expired")
        self.assertEqual(preference[1], 0.275)
        self.assertEqual(preference[2], "2025-01-02T00:00:00+00:00")
        self.assertEqual(reactivated["lifecycle_status"], "active")
        self.assertFalse(reactivated["expired"])
        self.assertTrue(reactivated["can_influence_suggestion"])

    def test_preferences_are_separated_by_review_position(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite3")
            write(db_path, feedback("MEM-A", "Party A wording", "2026-01-01T00:00:00+00:00", position="party_a"))
            write(db_path, feedback("MEM-B", "Party B wording", "2026-01-01T00:00:00+00:00", position="party_b"))
            party_a = retrieve(db_path, position="party_a")
            party_b = retrieve(db_path, position="party_b")

        self.assertEqual(party_a[0]["source_memory_ids"], ["MEM-A"])
        self.assertEqual(party_b[0]["source_memory_ids"], ["MEM-B"])
        self.assertNotEqual(party_a[0]["memory_id"], party_b[0]["memory_id"])

    def test_duplicate_provided_episode_ids_are_rejected(self):
        duplicate = feedback(
            "MEM-DUPLICATE",
            "Suggestion A",
            "2026-01-01T00:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "must be unique"):
            retrieve_memory(
                {
                    **retrieve_input(":memory:"),
                    "memory_items": [duplicate, dict(duplicate)],
                    "now": "2026-01-05T00:00:00+00:00",
                }
            )


def write(db_path: str, payload: dict) -> dict:
    return write_memory({"db_path": db_path, "human_feedback": payload})


def retrieve(db_path: str, position: str = "party_b") -> list[dict]:
    return retrieve_memory(
        {
            **retrieve_input(db_path, position),
            "now": "2026-01-05T00:00:00+00:00",
        }
    )


def retrieve_input(db_path: str, position: str = "party_b") -> dict:
    return {
        "db_path": db_path,
        "contract_type": "NDA",
        "clause": {"clause_type": "use_restriction"},
        "risk_type": "unclear_use_restriction",
        "review_position": position,
        "memory_items": [],
        "limit": 3,
    }


def feedback(
    memory_id: str,
    suggestion: str,
    created_at: str,
    *,
    action: str = "update_suggestion",
    position: str = "party_b",
) -> dict:
    return {
        "memory_id": memory_id,
        "contract_type": "NDA",
        "clause_type": "use_restriction",
        "risk_type": "unclear_use_restriction",
        "review_position": position,
        "user_action": action,
        "original_severity": "medium",
        "final_severity": "medium",
        "original_suggestion": "Original suggestion",
        "final_suggestion": suggestion,
        "ignore_reason": "Later feedback rejected this preference." if action == "ignore" else "",
        "source_finding_id": f"RISK-{memory_id}",
        "source_clause_id": "CL-001",
        "include_in_report": action != "ignore",
        "created_at": created_at,
    }


if __name__ == "__main__":
    unittest.main()
