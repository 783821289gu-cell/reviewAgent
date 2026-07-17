import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx


APP_DIR = Path(__file__).resolve().parents[1] / "app"
DEEPSEEK_CONTRACT_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "deepseek"
    / "chat_completions_contracts.json"
)
sys.path.insert(0, str(APP_DIR))

from config import Settings
from models.review import ReviewPosition, ReviewStatus
from providers.llm_provider import (
    LLMOutputInvalidError,
    LLMProviderError,
    LLMRequest,
    LocalStructuredProvider,
    OpenAICompatibleProvider,
    create_llm_provider,
)
from services.clause_service import extract_key_fields
from services.evaluation_service import _docx_bytes_from_text
from services.event_service import ReviewEventStore
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from services.risk_analyzer import analyze_risk


class LLMProviderTest(unittest.TestCase):
    def test_deepseek_fixture_request_contract_and_success_response(self):
        fixture = _deepseek_contract_fixture()
        success_case = _deepseek_case(fixture, "success_json")
        captured_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return _fixture_http_response(request, success_case)

        provider = OpenAICompatibleProvider(
            _external_settings(
                llm_base_url=fixture["base_url"],
                llm_api_key="test-deepseek-key",
                llm_model=fixture["model"],
            ),
            transport=httpx.MockTransport(handler),
        )
        response = provider.generate_structured(
            LLMRequest(
                operation="analyze_risk",
                input_payload={"current_clause": {"clause_id": "CL-001"}},
                output_schema={"type": "object", "required": ["risk_type"]},
                local_output={},
            )
        )

        self.assertEqual(len(captured_requests), 1)
        captured = captured_requests[0]
        request_body = json.loads(captured.content.decode("utf-8"))
        self.assertEqual(str(captured.url), fixture["endpoint"])
        self.assertEqual(captured.method, fixture["request_contract"]["method"])
        self.assertEqual(captured.headers["authorization"], "Bearer test-deepseek-key")
        self.assertEqual(request_body["model"], fixture["model"])
        self.assertEqual(request_body["temperature"], 0)
        self.assertEqual(
            request_body["response_format"],
            fixture["request_contract"]["body"]["response_format"],
        )
        self.assertEqual(
            [message["role"] for message in request_body["messages"]],
            fixture["request_contract"]["body"]["message_roles"],
        )
        self.assertIn("JSON object", request_body["messages"][0]["content"])
        self.assertEqual(
            json.loads(request_body["messages"][1]["content"])["operation"],
            "analyze_risk",
        )
        self.assertEqual(response.output["risk_type"], "保密信息范围过宽")
        self.assertEqual(
            response.metadata.provider_request_id,
            success_case["current_behavior"]["provider_request_id"],
        )
        raw_fixture = DEEPSEEK_CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("test-deepseek-key", raw_fixture)
        self.assertNotIn("sk-", raw_fixture)

    def test_deepseek_fixture_provider_error_contracts(self):
        fixture = _deepseek_contract_fixture()
        cases = {case["id"]: case for case in fixture["cases"]}
        self.assertEqual(
            set(cases),
            {
                "success_json",
                "empty_content",
                "truncated_json",
                "schema_mismatch",
                "timeout",
                "rate_limit",
                "authentication_failure",
                "temporary_service_error",
            },
        )
        target_policy = fixture["task_2_target_retry_policy"]
        self.assertEqual(
            set(target_policy["retry_once_case_ids"]),
            {"timeout", "rate_limit", "temporary_service_error"},
        )
        self.assertEqual(
            set(target_policy["non_retryable_case_ids"]),
            {
                "empty_content",
                "truncated_json",
                "schema_mismatch",
                "authentication_failure",
            },
        )
        self.assertEqual(
            target_policy["maximum_attempts"],
            {"retryable": 2, "non_retryable": 1},
        )

        for case_id in (
            "empty_content",
            "truncated_json",
            "timeout",
            "rate_limit",
            "authentication_failure",
            "temporary_service_error",
        ):
            case = cases[case_id]

            def handler(request: httpx.Request, current_case=case) -> httpx.Response:
                if current_case["transport"] == "read_timeout":
                    raise httpx.ReadTimeout("synthetic timeout", request=request)
                return _fixture_http_response(request, current_case)

            provider = OpenAICompatibleProvider(
                _external_settings(
                    llm_base_url=fixture["base_url"],
                    llm_model=fixture["model"],
                ),
                transport=httpx.MockTransport(handler),
            )
            calls = []
            with self.subTest(case_id=case_id):
                with self.assertRaises(LLMProviderError) as raised:
                    provider.generate_structured(
                        LLMRequest("analyze_risk", {}, {"type": "object"}, {}),
                        call_records=calls,
                    )
                self.assertEqual(
                    raised.exception.error_type,
                    case["current_behavior"]["error_type"],
                )
                self.assertEqual(
                    raised.exception.retryable,
                    case["current_behavior"]["retryable"],
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    calls[0].error_type,
                    case["current_behavior"]["error_type"],
                )

    def test_deepseek_fixture_records_current_schema_retry_baseline(self):
        fixture = _deepseek_contract_fixture()
        schema_case = _deepseek_case(fixture, "schema_mismatch")
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return _fixture_http_response(request, schema_case)

        provider = OpenAICompatibleProvider(
            _external_settings(
                llm_base_url=fixture["base_url"],
                llm_model=fixture["model"],
            ),
            transport=httpx.MockTransport(handler),
        )
        with patch("services.llm_service.create_llm_provider", return_value=provider):
            with self.assertRaises(LLMOutputInvalidError):
                analyze_risk({"review_context": _review_context()})

        self.assertEqual(attempts, schema_case["current_behavior"]["attempt_count"])

    def test_openai_compatible_success_records_real_metadata_without_secret(self):
        api_key = "test-secret-key"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], f"Bearer {api_key}")
            return _success_response(request, _risk_output(), request_id="req-success")

        provider = OpenAICompatibleProvider(
            _external_settings(
                llm_api_key=api_key,
                llm_prompt_cost_per_million=1.0,
                llm_completion_cost_per_million=2.0,
            ),
            transport=httpx.MockTransport(handler),
        )
        logs = []
        with patch("services.llm_service.create_llm_provider", return_value=provider):
            finding = invoke_tool(
                "task-llm-success",
                {"analyze_risk": analyze_risk},
                "analyze_risk",
                {"review_context": _review_context()},
                logs,
            )

        self.assertEqual(finding["risk_type"], "保密信息范围过宽")
        summary = json.loads(logs[0].token_cost_summary)
        call = summary["calls"][0]
        self.assertEqual(call["mode"], "openai_compatible")
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["provider_request_id"], "req-success")
        self.assertEqual(call["prompt_tokens"], 100)
        self.assertEqual(call["completion_tokens"], 50)
        self.assertEqual(call["cost_status"], "calculated")
        self.assertEqual(call["estimated_cost"], "0.0002")
        self.assertNotIn(api_key, logs[0].token_cost_summary)
        self.assertNotIn("商业信息、技术资料", logs[0].input_summary)

    def test_timeout_is_retried_once_then_succeeds(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadTimeout("controlled timeout", request=request)
            return _success_response(request, _risk_output(), request_id="req-after-timeout")

        self._assert_retry_succeeds(handler)
        self.assertEqual(calls, 2)

    def test_rate_limit_is_retried_once_then_succeeds(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, request=request, headers={"x-request-id": "req-429"})
            return _success_response(request, _risk_output(), request_id="req-after-429")

        self._assert_retry_succeeds(handler)
        self.assertEqual(calls, 2)

    def test_invalid_structured_output_is_retried_once(self):
        outputs = [{"risk_type": "bad"}, _risk_output()]

        def handler(request: httpx.Request) -> httpx.Response:
            return _success_response(request, outputs.pop(0), request_id="req-schema")

        self._assert_retry_succeeds(handler)
        self.assertEqual(outputs, [])

    def test_external_failure_after_retry_is_explicit_without_local_fallback(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, request=request, headers={"x-request-id": f"req-{calls}"})

        provider = OpenAICompatibleProvider(
            _external_settings(),
            transport=httpx.MockTransport(handler),
        )
        logs = []
        with patch("services.llm_service.create_llm_provider", return_value=provider):
            with self.assertRaises(LLMOutputInvalidError):
                invoke_tool(
                    "task-llm-failed",
                    {"analyze_risk": analyze_risk},
                    "analyze_risk",
                    {"review_context": _review_context()},
                    logs,
                )

        self.assertEqual(calls, 2)
        self.assertEqual(logs[0].status, "failed")
        summary = json.loads(logs[0].token_cost_summary)
        self.assertEqual(len(summary["calls"]), 2)
        self.assertTrue(all(call["error_type"] == "rate_limit" for call in summary["calls"]))
        self.assertNotIn("local_structured", logs[0].token_cost_summary)

    def test_unconfigured_pricing_is_not_reported_as_zero(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _success_response(request, {"ok": True}, request_id="req-no-price")

        provider = OpenAICompatibleProvider(
            _external_settings(),
            transport=httpx.MockTransport(handler),
        )
        response = provider.generate_structured(
            LLMRequest("test", {}, {"type": "object"}, {})
        )

        self.assertEqual(response.metadata.cost_status, "未配置")
        self.assertIsNone(response.metadata.estimated_cost)

    def test_missing_external_configuration_is_non_retryable_and_recorded(self):
        provider = OpenAICompatibleProvider(_external_settings(llm_api_key=""))
        request = LLMRequest("test", {}, {"type": "object"}, {})

        calls = []
        with self.assertRaises(LLMProviderError) as raised:
            provider.generate_structured(request, call_records=calls)

        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.error_type, "configuration_error")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].error_type, "configuration_error")

    def test_provider_mode_factory_is_explicit(self):
        self.assertIsInstance(
            create_llm_provider(Settings(llm_mode="local_structured")),
            LocalStructuredProvider,
        )
        self.assertIsInstance(
            create_llm_provider(_external_settings()),
            OpenAICompatibleProvider,
        )

    def test_small_non_zero_cost_is_not_rounded_to_zero(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0},
                },
            )

        provider = OpenAICompatibleProvider(
            _external_settings(
                llm_prompt_cost_per_million=0.01,
                llm_completion_cost_per_million=0.01,
            ),
            transport=httpx.MockTransport(handler),
        )
        response = provider.generate_structured(LLMRequest("test", {}, {}, {}))

        self.assertEqual(response.metadata.cost_status, "calculated")
        self.assertEqual(response.metadata.estimated_cost, "0.00000001")

    def test_non_object_provider_body_is_recorded_as_schema_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request, json=[])

        provider = OpenAICompatibleProvider(
            _external_settings(),
            transport=httpx.MockTransport(handler),
        )
        calls = []
        with self.assertRaises(LLMProviderError) as raised:
            provider.generate_structured(
                LLMRequest("test", {}, {}, {}),
                call_records=calls,
            )

        self.assertEqual(raised.exception.error_type, "schema_error")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(calls[0].error_type, "schema_error")

    def test_key_field_provider_is_only_used_when_rules_find_nothing(self):
        with patch("services.clause_service.generate_structured_key_fields") as provider_call:
            deterministic = extract_key_fields(
                {"clause": {"clause_id": "CL-001", "text": "接收方应承担保密义务。"}}
            )
        self.assertIn("接收方", deterministic["obligation_subject"])
        provider_call.assert_not_called()

        with patch(
            "services.clause_service.generate_structured_key_fields",
            return_value={"use_purpose": ["项目评估"]},
        ) as provider_call:
            supplemented = extract_key_fields(
                {"clause": {"clause_id": "CL-002", "text": "信息仅限项目评估场景。"}}
            )
        self.assertEqual(supplemented["use_purpose"], ["项目评估"])
        provider_call.assert_called_once()

    def test_revision_failure_after_retry_never_enters_formal_risk_list(self):
        failure = LLMProviderError("rate_limit", "controlled rate limit", True)
        with patch(
            "services.risk_analyzer.generate_structured_revision",
            side_effect=failure,
        ) as provider_call:
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="nda.docx",
                file_type="docx",
                content=_docx_bytes_from_text(
                    "保密协议\n"
                    "1. 定义\n"
                    "保密信息包括披露方提供的全部商业信息和技术资料。\n"
                    "2. 保密义务\n"
                    "接收方不得向第三方披露保密信息。"
                ),
                review_position=ReviewPosition.PARTY_A,
            )

        self.assertEqual(provider_call.call_count, 2)
        self.assertEqual(state.status, ReviewStatus.LLM_OUTPUT_INVALID)
        self.assertEqual(state.risk_findings, [])

    def test_key_field_failure_after_retry_enters_llm_output_invalid(self):
        failure = LLMProviderError("timeout", "controlled timeout", True)
        with patch(
            "services.clause_service.generate_structured_key_fields",
            side_effect=failure,
        ) as provider_call:
            state = ReviewOrchestratorAgent(ReviewEventStore()).run_sync(
                file_name="nda.docx",
                file_type="docx",
                content=_docx_bytes_from_text(
                    "保密协议\n"
                    "1. 期限\n"
                    "披露方与接收方约定保密义务持续至项目结束。"
                ),
                review_position=ReviewPosition.PARTY_A,
            )

        self.assertEqual(provider_call.call_count, 2)
        self.assertEqual(state.status, ReviewStatus.LLM_OUTPUT_INVALID)
        self.assertEqual(state.risk_findings, [])

    def _assert_retry_succeeds(self, handler) -> None:
        provider = OpenAICompatibleProvider(
            _external_settings(),
            transport=httpx.MockTransport(handler),
        )
        with patch("services.llm_service.create_llm_provider", return_value=provider):
            finding = analyze_risk({"review_context": _review_context()})
        self.assertEqual(finding["review_status"], "CONFIRMED_RISK")


def _external_settings(**overrides) -> Settings:
    values = {
        "llm_mode": "openai_compatible",
        "llm_base_url": "https://llm.example.test/v1",
        "llm_api_key": "test-key",
        "llm_model": "test-model",
        "llm_timeout_seconds": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _deepseek_contract_fixture() -> dict:
    return json.loads(DEEPSEEK_CONTRACT_PATH.read_text(encoding="utf-8"))


def _deepseek_case(fixture: dict, case_id: str) -> dict:
    return next(case for case in fixture["cases"] if case["id"] == case_id)


def _fixture_http_response(request: httpx.Request, case: dict) -> httpx.Response:
    response = case["response"]
    return httpx.Response(
        response["status_code"],
        request=request,
        headers=response.get("headers") or {},
        json=response.get("body") or {},
    )


def _success_response(request: httpx.Request, output: dict, request_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        headers={"x-request-id": request_id},
        json={
            "id": "body-request-id",
            "choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )


def _review_context() -> dict:
    risk_focus = "确保甲方保密信息定义可识别且可执行。"
    return {
        "context_id": "CL-001:NDA-R001",
        "contract_type": "NDA",
        "review_position": "甲方",
        "current_clause": {
            "clause_id": "CL-001",
            "clause_type": "定义",
            "text": "保密信息包括商业信息、技术资料。",
        },
        "matched_rule": {
            "rule_id": "NDA-R001",
            "risk_type": "保密信息范围过宽",
            "review_position": "甲方",
            "check_point": "检查定义范围",
            "risk_focus": risk_focus,
            "position_config": {
                "severity_default": "中",
                "risk_focus": risk_focus,
                "revision_template": "限定保密信息范围。",
            },
        },
    }


def _risk_output() -> dict:
    return {
        "risk_type": "保密信息范围过宽",
        "severity": "中",
        "confidence": 0.9,
        "risk_reason": "证据文本命中定义范围检查。",
        "clause_id": "CL-001",
        "evidence_text": "保密信息包括商业信息、技术资料。",
        "matched_rule_ids": ["NDA-R001"],
        "review_position": "甲方",
        "risk_focus": "确保甲方保密信息定义可识别且可执行。",
        "revision_suggestion": "限定保密信息范围。",
        "review_status": "CONFIRMED_RISK",
    }


if __name__ == "__main__":
    unittest.main()
