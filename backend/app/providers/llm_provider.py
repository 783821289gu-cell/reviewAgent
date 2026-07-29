"""在本地确定性模式和真实 DeepSeek 请求之间提供统一调用接口。"""

import json
from contextvars import ContextVar, Token
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
    """角色服务交给 Provider 的统一请求对象。"""

    operation: str
    input_payload: dict
    output_schema: dict
    local_output: dict


@dataclass
class LLMCallMetadata:
    """一次模型调用的审计记录，不包含合同正文和完整提示词。"""

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
        """转换为日志和持久化可以使用的普通字典。"""

        return asdict(self)


@dataclass(frozen=True)
class LLMResponse:
    """Provider 返回的结构化对象和本次调用审计信息。"""

    output: dict
    metadata: LLMCallMetadata


LLM_CALL_RECORDS_INPUT_KEY = "_llm_call_records"
_LLM_MODE_OVERRIDE: ContextVar[str | None] = ContextVar(
    "review_agent_llm_mode",
    default=None,
)


class LLMProvider(Protocol):
    """本地与远程 Provider 必须实现的共同接口。"""

    def generate_structured(
        self,
        request: LLMRequest,
        call_records: list[LLMCallMetadata] | None = None,
    ) -> LLMResponse:
        """根据统一请求返回 JSON 对象，并可向审计列表追加调用记录。"""

        ...


class LLMProviderError(RuntimeError):
    """可以区分错误类型及是否允许上层重试的 Provider 异常。"""

    def __init__(self, error_type: str, message: str, retryable: bool):
        """保存对用户稳定的错误类型和重试属性，原始密钥/响应不进入消息。"""

        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


class LLMOutputInvalidError(ValueError):
    """Provider JSON 合法，但不满足具体角色领域约束。"""

    pass


def llm_call_records_from_tool_input(tool_input: dict) -> list[LLMCallMetadata] | None:
    """取出 ``invoke_tool`` 注入的模型调用审计列表。"""

    # 这是运行时附加字段，不属于 input_payload，因此不会进入 Prompt。
    # Provider 向同一列表追加 Metadata，invoke_tool 随后就能写入 StepLog。
    records = tool_input.get(LLM_CALL_RECORDS_INPUT_KEY)
    return records if isinstance(records, list) else None


def mark_latest_llm_call_schema_error(
    call_records: list[LLMCallMetadata] | None,
) -> None:
    """领域校验失败时，把最近一次调用标记为 schema_error。"""

    if call_records:
        call_records[-1].error_type = "schema_error"


def bind_llm_mode(llm_mode: str) -> Token:
    """把当前任务选择的模型模式绑定到并发上下文。"""

    # ContextVar 让两个并发任务各自保留模式；返回 Token 供结束时恢复旧值。
    if llm_mode not in {"local_structured", "openai_compatible"}:
        raise ValueError(f"unsupported task llm_mode: {llm_mode}")
    return _LLM_MODE_OVERRIDE.set(llm_mode)


def reset_llm_mode(token: Token) -> None:
    """使用 ``bind_llm_mode`` 返回的 Token 恢复先前模型模式。"""

    _LLM_MODE_OVERRIDE.reset(token)


def effective_llm_mode() -> str:
    """优先返回当前任务绑定模式，没有绑定时才使用应用默认配置。"""

    return _LLM_MODE_OVERRIDE.get() or settings.llm_mode


class LocalStructuredProvider:
    """不访问网络，直接返回角色服务准备的确定性 ``local_output``。"""

    def generate_structured(
        self,
        request: LLMRequest,
        call_records: list[LLMCallMetadata] | None = None,
    ) -> LLMResponse:
        """返回本地结果并记录一次明确标记为 no_external_llm 的调用。"""

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
    """通过 OpenAI 兼容 ``chat/completions`` 接口调用 DeepSeek。"""

    def __init__(
        self,
        app_settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ):
        """保存应用配置；``transport`` 主要供测试注入 MockTransport。"""

        self.settings = app_settings
        self.transport = transport

    def generate_structured(
        self,
        request: LLMRequest,
        call_records: list[LLMCallMetadata] | None = None,
    ) -> LLMResponse:
        """完成一次真实 DeepSeek 结构化调用并记录审计信息。"""

        start = perf_counter()
        request_id = ""
        prompt_report = None
        try:
            # 第 1 步：配置不完整时立即失败，不能产生看似成功的模型结果。
            self._validate_settings()

            # 第 2 步：operation 决定角色指令；input_payload 决定可见数据；
            # output_schema 决定允许返回的 JSON 形状。
            prompt = build_prompt_package(
                request.operation,
                request.input_payload,
                request.output_schema,
            )
            # 第 3 步：发送前用固定 tokenizer 计算预算。超预算不会发 HTTP。
            prompt_report = prompt_token_report(prompt)
            self._validate_prompt_budget(prompt_report)

            # 第 4 步：真正发送 HTTP；_post 固定 temperature=0 和 json_object。
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
            # 第 5 步：把 choices[0].message.content 从 JSON 字符串变成普通 dict。
            # 数组、Markdown 或非法 JSON 都在这里失败。
            output = _structured_output(body)
            prompt_tokens, completion_tokens = _usage(body)
            # 第 6 步：记录实际/估算 token、耗时、成本和 Prompt 版本。
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
            # 返回的 output 还不是最终 RiskFinding/PlannerDecision；
            # 对应角色 service 会继续执行字段白名单和领域约束。
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
        """发送前验证必需配置及数值范围，不测试远端连通性。"""

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
        """Prompt 超出应用上下文预算时在 HTTP 请求前终止。"""

        if report["prompt_tokens"] > self.settings.llm_context_budget_tokens:
            raise LLMProviderError(
                "context_budget_error",
                "LLM prompt exceeds the configured token budget",
                False,
            )

    def _post(self, prompt: PromptPackage) -> httpx.Response:
        """向 OpenAI 兼容端点发送固定参数的请求。"""

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
                    # 降低角色判断波动，但不代表结果一定正确。
                    "temperature": 0,
                    # 服务端要求 JSON；字段级权限仍由 Schema 和 Python 校验保证。
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
        """把 Provider 响应和上下文预算信息整理为不含正文的审计记录。"""

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
        """失败时也写入调用记录，防止 Trace 只看见成功请求。"""

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
    llm_mode: str | None = None,
) -> LLMProvider:
    """根据当前任务模式创建本地或真实模型 Provider。

    ``llm_mode`` 显式值优先；否则使用 ``app_settings.llm_mode``。未知模式直接
    报配置错误，不会悄悄回退到另一种执行路径。
    """

    resolved_mode = llm_mode or app_settings.llm_mode
    if resolved_mode == "local_structured":
        return LocalStructuredProvider()
    if resolved_mode == "openai_compatible":
        return OpenAICompatibleProvider(app_settings, transport=transport)
    raise LLMProviderError(
        "configuration_error",
        f"unsupported REVIEW_AGENT_LLM_MODE: {resolved_mode}",
        False,
    )


def _record_call(
    metadata: LLMCallMetadata,
    call_records: list[LLMCallMetadata] | None,
) -> None:
    """在调用方提供审计列表时追加 Metadata；没有列表则保持无副作用。"""

    if call_records is not None:
        call_records.append(metadata)


def _structured_output(body: dict) -> dict:
    """从 OpenAI 兼容响应中解析唯一 JSON 对象。

    空 choices、非字符串 content、非法 JSON 或数组输出都会失败，不能进入角色
    service 的下一层领域校验。
    """

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
    """读取 Provider 返回的 prompt/completion token，并做非负整数校验。"""

    usage = body.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    return _optional_non_negative_int(prompt_tokens), _optional_non_negative_int(completion_tokens)


def _optional_non_negative_int(value) -> int | None:
    """接受非负整数或纯数字字符串；缺失返回 None，其他类型拒绝。"""

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
    """根据 token 和每百万 token 单价计算估算成本。

    缺价格或缺 token 时返回明确状态，不伪造为 0 成本。
    """

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
    """把 HTTP 状态映射成稳定错误类型，并声明是否可重试。"""

    if status_code == 429:
        return LLMProviderError("rate_limit", "LLM provider rate limit exceeded", True)
    if status_code in {408, 409, 425} or status_code >= 500:
        return LLMProviderError("temporary_error", "LLM provider is temporarily unavailable", True)
    if status_code in {401, 403}:
        return LLMProviderError("authentication_error", "LLM provider authentication failed", False)
    return LLMProviderError("provider_error", f"LLM provider returned HTTP {status_code}", False)


def _elapsed_ms(start: float) -> int:
    """把高精度计时转换为非负毫秒数。"""

    return max(0, int((perf_counter() - start) * 1000))
