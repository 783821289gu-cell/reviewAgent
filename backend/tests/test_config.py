import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "backend" / "app"
sys.path.insert(0, str(APP_DIR))

from config import PROJECT_ENV_FILE, Settings, _load_project_environment


class ConfigTest(unittest.TestCase):
    def test_project_env_file_is_loaded_without_overriding_process_environment(self):
        with TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text(
                "REVIEW_AGENT_DOTENV_TEST_FILE=file-value\n"
                "REVIEW_AGENT_DOTENV_TEST_EXISTING=file-value\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "REVIEW_AGENT_LOAD_DOTENV": "1",
                    "REVIEW_AGENT_DOTENV_TEST_EXISTING": "process-value",
                },
                clear=False,
            ):
                os.environ.pop("REVIEW_AGENT_DOTENV_TEST_FILE", None)

                loaded = _load_project_environment(dotenv_path)

                self.assertTrue(loaded)
                self.assertEqual(
                    os.environ["REVIEW_AGENT_DOTENV_TEST_FILE"],
                    "file-value",
                )
                self.assertEqual(
                    os.environ["REVIEW_AGENT_DOTENV_TEST_EXISTING"],
                    "process-value",
                )

    def test_explicit_disable_keeps_test_process_isolated(self):
        with TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text(
                "REVIEW_AGENT_DOTENV_TEST_DISABLED=file-value\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"REVIEW_AGENT_LOAD_DOTENV": "0"},
                clear=False,
            ):
                os.environ.pop("REVIEW_AGENT_DOTENV_TEST_DISABLED", None)

                self.assertFalse(_load_project_environment(dotenv_path))
                self.assertNotIn("REVIEW_AGENT_DOTENV_TEST_DISABLED", os.environ)

    def test_missing_env_file_keeps_existing_environment(self):
        with TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.env"
            with patch.dict(
                os.environ,
                {"REVIEW_AGENT_DOTENV_TEST_EXISTING": "process-value"},
                clear=False,
            ):
                self.assertFalse(_load_project_environment(missing_path))
                self.assertEqual(
                    os.environ["REVIEW_AGENT_DOTENV_TEST_EXISTING"],
                    "process-value",
                )

    def test_default_env_file_is_project_root_env(self):
        self.assertEqual(PROJECT_ENV_FILE, PROJECT_ROOT / ".env")

    def test_execution_timeout_and_runtime_log_defaults_are_explicit(self):
        settings = Settings()

        self.assertEqual(settings.node_timeout_seconds, 90)
        self.assertEqual(settings.task_timeout_seconds, 900)
        self.assertTrue(settings.runtime_log_file.endswith("review_agent.log"))
        self.assertEqual(settings.runtime_log_level, "INFO")


if __name__ == "__main__":
    unittest.main()
