import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from math import isfinite
from time import perf_counter
from typing import Protocol

import httpx

from config import Settings, settings
from services.prompt_service import (
    PROMPT_VERSION,
    PromptPackage,
    build_prompt_package,
    prompt_token_report,
)


@dataclass(frozen=True)
class LLMRequest:
    operation: str
    input_payload: dict
    output_schema: dict
    local_output: dict


@dataclass
class LLMCallMetadata:
    mode: str
    operation: str
    model: str
    provider_request_id: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    cost_status: str
    estimated_cost: str | None
    prompt_cost_per_million: float | None
    completion_cost_per_million: float | None
    error_type: str
    prompt_version: str
    estimated_prompt_tokens: int | None
    prompt_token_breakdown: dict
    prompt_token_budget: int | None
    prompt_reduction_trace: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LLMResponse:
    output: dict
    metadata: LLMCallMetadata


LLM_CALL_RECORDS_INPUT_KEY = "_llm_call_records"


class LLMProvider(Protocol):
    def generate_structured(
        self,
        request: LLMRequest,
        call_records: list[LLMCallMetadata] | None = None,
    ) -> LLMResponse: ...


class LLMProviderError(RuntimeError):
    def __init__(self, error_type: str, message: str, retryable: bool):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


class LLMOutputInvalidError(ValueError):
    pass


def llm_call_records_from_tool_input(tool_input: dict) -> list[LLMCallMetadata] | None:
    records = tool_input.get(LLM_CALL_RECORDS_INPUT_KEY)
    return records if isinstance(records, list) else None


def mark_latest_llm_call_schema_error(
    call_records: list[LLMCallMetadata] | None,
) -> None:
    if call_records:
        call_records[-1].error_type = "schema_error"


class LocalStructuredProvider:
    def generate_structured(
        self,
        request: LLMRequest,
        call_records: list[LLMCallMetadata] | None = None,
    ) -> LLMResponse:
        start = perf_counter()
        metadata = LLMCallMetadata(
            mode="local_structured",
            operation=request.operation,
            model="no_external_llm",
            provider_request_id="",
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=_elapsed_ms(start),
            cost_status="no_external_llm",
            estimated_cost=None,
            prompt_cost_per_million=None,
            completion_cost_per_million=None,
            error_type="",
            prompt_version="",
            estimated_prompt_tokens=None,
            prompt_token_breakdown={},
            prompt_token_budget=None,
            prompt_reduction_trace=[],
        )
        _record_call(metadata, call_records)
        return LLMResponse(output=dict(request.local_output), metadata=metadata)


class OpenAICompatibleProvider:
    def __init__(
        self,
        app_settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = app_settings
        self.transport = transport

    def generate_structured(
        self,
        request: LLMRequest,
        call_records: list[LLMCallMetadata] | None = None,
    ) -> LLMResponse:
        start = perf_counter()
        request_id = ""
        prompt_report = None
        try:
            self._validate_settings()
            prompt = build_prompt_package(
                request.operation,
                request.input_payload,
                request.output_schema,
            )
            prompt_report = prompt_token_report(prompt)
            self._validate_prompt_budget(prompt_report)
            response = self._post(prompt)
            request_id = response.headers.get("x-request-id", "")
            if response.status_code >= 400:
                raise _http_error(response.status_code)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("LLM response body must be an object")
            body_request_id = body.get("id")
            if not request_id and body_request_id is not None:
                request_id = str(body_request_id)
            output = _structured_output(body)
            prompt_tokens, completion_tokens = _usage(body)
            metadata = self._metadata(
                request,
                request_id,
                prompt_tokens,
                completion_tokens,
                _elapsed_ms(start),
                "",
                prompt_report,
            )
            _record_call(metadata, call_records)
            return LLMResponse(output=output, metadata=metadata)
        except LLMProviderError as exc:
            self._record_error(
                request,
                request_id,
                start,
                exc.error_type,
                prompt_report,
                call_records,
            )
            raise
        except httpx.TimeoutException as exc:
            error = LLMProviderError("timeout", "LLM request timed out", True)
            self._record_error(
                request,
                request_id,
                start,
                error.error_type,
                prompt_report,
                call_records,
            )
            raise error from exc
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            error = LLMProviderError("temporary_error", "LLM request failed temporarily", True)
            self._record_error(
                request,
                request_id,
                start,
                error.error_type,
                prompt_report,
                call_records,
            )
            raise error from exc
        except (OSError, RuntimeError) as exc:
            error = LLMProviderError(
                "configuration_error",
                "DeepSeek tokenizer is unavailable or invalid",
                False,
            )
            self._record_error(
                request,
                request_id,
                start,
                error.error_type,
                prompt_report,
                call_records,
            )
            raise error from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            error = LLMProviderError(
                "schema_error",
                "LLM response is not valid structured JSON",
                False,
            )
            self._record_error(
                request,
                request_id,
                start,
                error.error_type,
                prompt_report,
                call_records,
            )
            raise error from exc

    def _validate_settings(self) -> None:
        if not self.settings.llm_base_url:
            raise LLMProviderError("configuration_error", "LLM Base URL is not configured", False)
        if not self.settings.llm_api_key:
            raise LLMProviderError("configuration_error", "LLM API Key is not configured", False)
        if not self.settings.llm_model:
            raise LLMProviderError("configuration_error", "LLM model is not configured", False)
        if not isfinite(self.settings.llm_timeout_seconds) or self.settings.llm_timeout_seconds <= 0:
            raise LLMProviderError("configuration_error", "LLM timeout must be positive", False)
        for price in (
            self.settings.llm_prompt_cost_per_million,
            self.settings.llm_completion_cost_per_million,
        ):
            if price is not None and (not isfinite(price) or price < 0):
                raise LLMProviderError(
                    "configuration_error",
                    "LLM token price must be a finite non-negative number",
                    False,
                )

    def _validate_prompt_budget(self, report: dict) -> None:
        if report["prompt_tokens"] > self.settings.llm_context_budget_tokens:
            raise LLMProviderError(
                "context_budget_error",
                "LLM prompt exceeds the configured token budget",
                False,
            )

    def _post(self, prompt: PromptPackage) -> httpx.Response:
        base_url = self.settings.llm_base_url.rstrip("/")
        with httpx.Client(
            timeout=self.settings.llm_timeout_seconds,
            transport=self.transport,
        ) as client:
            return client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.llm_model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": prompt.messages,
                },
            )

    def _metadata(
        self,
        request: LLMRequest,
        request_id: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
        error_type: str,
        prompt_report: dict | None,
    ) -> LLMCallMetadata:
        cost_status, estimated_cost = _cost(
            prompt_tokens,
            completion_tokens,
            self.settings.llm_prompt_cost_per_million,
            self.settings.llm_completion_cost_per_million,
        )
        token_budget = request.input_payload.get("token_budget")
        if not isinstance(token_budget, dict):
            token_budget = {}
        reduction_trace = token_budget.get("reduction_trace")
        if not isinstance(reduction_trace, list):
            reduction_trace = []
        prompt_token_budget = token_budget.get("max_prompt_tokens")
        if isinstance(prompt_token_budget, bool) or not isinstance(prompt_token_budget, int):
            prompt_token_budget = self.settings.llm_context_budget_tokens
        return LLMCallMetadata(
            mode="openai_compatible",
            operation=request.operation,
            model=self.settings.llm_model,
            provider_request_id=request_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_status=cost_status,
            estimated_cost=estimated_cost,
            prompt_cost_per_million=self.settings.llm_prompt_cost_per_million,
            completion_cost_per_million=self.settings.llm_completion_cost_per_million,
            error_type=error_type,
            prompt_version=PROMPT_VERSION,
            estimated_prompt_tokens=(prompt_report or {}).get("prompt_tokens"),
            prompt_token_breakdown=dict((prompt_report or {}).get("category_tokens") or {}),
            prompt_token_budget=prompt_token_budget,
            prompt_reduction_trace=[str(item) for item in reduction_trace],
        )

    def _record_error(
        self,
        request: LLMRequest,
        request_id: str,
        start: float,
        error_type: str,
        prompt_report: dict | None,
        call_records: list[LLMCallMetadata] | None,
    ) -> None:
        _record_call(
            self._metadata(
                request,
                request_id,
                None,
                None,
                _elapsed_ms(start),
                error_type,
                prompt_report,
            ),
            call_records,
        )


def create_llm_provider(
    app_settings: Settings = settings,
    transport: httpx.BaseTransport | None = None,
) -> LLMProvider:
    if app_settings.llm_mode == "local_structured":
        return LocalStructuredProvider()
    if app_settings.llm_mode == "openai_compatible":
        return OpenAICompatibleProvider(app_settings, transport=transport)
    raise LLMProviderError(
        "configuration_error",
        f"unsupported REVIEW_AGENT_LLM_MODE: {app_settings.llm_mode}",
        False,
    )


def _record_call(
    metadata: LLMCallMetadata,
    call_records: list[LLMCallMetadata] | None,
) -> None:
    if call_records is not None:
        call_records.append(metadata)


def _structured_output(body: dict) -> dict:
    choices = body["choices"]
    if not isinstance(choices, list) or not choices:
        raise ValueError("choices must not be empty")
    content = choices[0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("message content must be a JSON string")
    output = json.loads(content)
    if not isinstance(output, dict):
        raise ValueError("structured output must be an object")
    return output


def _usage(body: dict) -> tuple[int | None, int | None]:
    usage = body.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    return _optional_non_negative_int(prompt_tokens), _optional_non_negative_int(completion_tokens)


def _optional_non_negative_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("token usage must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise ValueError("token usage must be an integer")
    if parsed < 0:
        raise ValueError("token usage must not be negative")
    return parsed


def _cost(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    prompt_price: float | None,
    completion_price: float | None,
) -> tuple[str, str | None]:
    if prompt_price is None or completion_price is None:
        return "未配置", None
    if prompt_tokens is None or completion_tokens is None:
        return "token_usage_unavailable", None
    total = (
        Decimal(prompt_tokens) * Decimal(str(prompt_price))
        + Decimal(completion_tokens) * Decimal(str(completion_price))
    ) / Decimal(1_000_000)
    formatted = format(total.quantize(Decimal("0.000000000001")), "f").rstrip("0").rstrip(".")
    return "calculated", formatted or "0"


def _http_error(status_code: int) -> LLMProviderError:
    if status_code == 429:
        return LLMProviderError("rate_limit", "LLM provider rate limit exceeded", True)
    if status_code in {408, 409, 425} or status_code >= 500:
        return LLMProviderError("temporary_error", "LLM provider is temporarily unavailable", True)
    if status_code in {401, 403}:
        return LLMProviderError("authentication_error", "LLM provider authentication failed", False)
    return LLMProviderError("provider_error", f"LLM provider returned HTTP {status_code}", False)


def _elapsed_ms(start: float) -> int:
    return max(0, int((perf_counter() - start) * 1000))
