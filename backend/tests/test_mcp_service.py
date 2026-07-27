from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import sys
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from config import Settings
from main import _close_runtime_resources, create_app
from services.event_service import ReviewEventStore
from services.mcp_service import (
    MCP_EXCLUDED_TOOL_NAMES,
    _is_loopback_client,
    create_mcp_runtime,
    exposed_mcp_tool_names,
)
from tools.registry import tool_registry


class McpServiceTest(unittest.TestCase):
    def test_mcp_is_disabled_by_default(self):
        self.assertIsNone(create_mcp_runtime(Settings(mcp_enabled=False)))

    def test_mcp_requires_loopback_api_binding(self):
        with self.assertRaisesRegex(ValueError, "REVIEW_AGENT_HOST=127.0.0.1"):
            create_mcp_runtime(Settings(mcp_enabled=True, host="0.0.0.0"))

    def test_mcp_exposes_only_the_registry_allowlist(self):
        runtime = create_mcp_runtime(
            Settings(mcp_enabled=True, host="127.0.0.1")
        )
        tools = runtime.server._tool_manager.list_tools()
        names = [tool.name for tool in tools]

        self.assertEqual(list(exposed_mcp_tool_names()), names)
        self.assertEqual(11, len(names))
        self.assertEqual(set(tool_registry) - MCP_EXCLUDED_TOOL_NAMES, set(names))
        self.assertTrue(MCP_EXCLUDED_TOOL_NAMES.isdisjoint(names))
        self.assertTrue(all(tool.parameters["type"] == "object" for tool in tools))
        self.assertTrue(all(tool.output_schema["type"] == "object" for tool in tools))

    def test_mcp_tool_invocation_uses_validated_adapter(self):
        runtime = create_mcp_runtime(
            Settings(mcp_enabled=True, host="127.0.0.1")
        )
        result = asyncio.run(
            runtime.server._tool_manager.call_tool(
                "retrieve_playbook_rules",
                {
                    "contract_type": "NDA",
                    "clause_type": "保密义务",
                    "key_fields": {},
                    "review_position": "甲方",
                },
            )
        )
        self.assertIn("matched_rules", result)
        with self.assertRaises(ToolError):
            asyncio.run(
                runtime.server._tool_manager.call_tool(
                    "retrieve_playbook_rules",
                    {
                        "contract_type": "NDA",
                        "clause_type": "保密义务",
                        "key_fields": {},
                    },
                )
            )

    def test_loopback_guard_accepts_only_ip_loopback_clients(self):
        self.assertTrue(_is_loopback_client(("127.0.0.1", 51000)))
        self.assertTrue(_is_loopback_client(("::1", 51000)))
        self.assertFalse(_is_loopback_client(("testclient", 51000)))
        self.assertFalse(_is_loopback_client(("192.0.2.1", 51000)))
        self.assertFalse(_is_loopback_client(None))

    def test_mcp_route_is_absent_when_disabled_and_rejects_remote_clients(self):
        with TemporaryDirectory() as temp_dir:
            disabled = create_app(
                Settings(
                    mcp_enabled=False,
                    memory_db_path=str(Path(temp_dir) / "disabled.sqlite3"),
                    runtime_log_file=str(Path(temp_dir) / "disabled.log"),
                ),
                ReviewEventStore(),
            )
            with TestClient(disabled) as client:
                self.assertEqual(404, client.get("/mcp/").status_code)

            enabled = create_app(
                Settings(
                    mcp_enabled=True,
                    host="127.0.0.1",
                    memory_db_path=str(Path(temp_dir) / "enabled.sqlite3"),
                    runtime_log_file=str(Path(temp_dir) / "enabled.log"),
                ),
                ReviewEventStore(),
            )
            with TestClient(
                enabled,
                client=("192.0.2.1", 51000),
            ) as client:
                response = client.get("/mcp/")
                self.assertEqual(403, response.status_code)
                self.assertIn("local host", response.json()["detail"])

    def test_local_mcp_endpoint_completes_initialize_and_tools_list(self):
        with TemporaryDirectory() as temp_dir:
            application = create_app(
                Settings(
                    mcp_enabled=True,
                    host="127.0.0.1",
                    memory_db_path=str(Path(temp_dir) / "mcp.sqlite3"),
                    runtime_log_file=str(Path(temp_dir) / "mcp.log"),
                ),
                ReviewEventStore(),
            )
            headers = {
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            }
            with TestClient(
                application,
                base_url="http://127.0.0.1:8000",
                client=("127.0.0.1", 51000),
            ) as client:
                initialized = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                self.assertEqual(200, initialized.status_code, initialized.text)
                self.assertEqual(
                    "contract-review-agent",
                    initialized.json()["result"]["serverInfo"]["name"],
                )
                listed = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                self.assertEqual(200, listed.status_code, listed.text)
                self.assertEqual(
                    list(exposed_mcp_tool_names()),
                    [tool["name"] for tool in listed.json()["result"]["tools"]],
                )

    def test_runtime_cleanup_continues_when_runtime_close_fails(self):
        runtime = Mock()
        runtime.close.side_effect = RuntimeError("close failed")
        application = Mock()
        application.state.runtime = runtime
        application.state.review_agent = None
        with (
            patch("main.shutdown_observability") as shutdown_observability,
            patch("main.close_runtime_logging") as close_runtime_logging,
            self.assertRaisesRegex(RuntimeError, "close failed"),
        ):
            _close_runtime_resources(application)

        shutdown_observability.assert_called_once_with()
        close_runtime_logging.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
