import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from math import isfinite, sqrt
import re
from time import perf_counter
from typing import Protocol

import httpx

from config import Settings, settings


LOCAL_SPARSE_DIMENSION = 256
TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.I)
EMBEDDING_CALL_RECORDS_INPUT_KEY = "_embedding_call_records"


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: list[str]


@dataclass
class EmbeddingCallMetadata:
    mode: str
    model: str
    provider_request_id: str
    input_count: int
    vector_dimension: int | None
    latency_ms: int
    error_type: str
    cost_status: str = "未配置"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    metadata: EmbeddingCallMetadata


class EmbeddingProvider(Protocol):
    mode: str
    model: str

    def embed(
        self,
        request: EmbeddingRequest,
        call_records: list[EmbeddingCallMetadata] | None = None,
    ) -> EmbeddingResponse: ...


class EmbeddingProviderError(RuntimeError):
    def __init__(self, error_type: str, message: str, retryable: bool):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


def embedding_call_records_from_tool_input(
    tool_input: dict,
) -> list[EmbeddingCallMetadata] | None:
    records = tool_input.get(EMBEDDING_CALL_RECORDS_INPUT_KEY)
    return records if isinstance(records, list) else None


class LocalSparseEmbeddingProvider:
    mode = "local_sparse"
    model = "local_sparse_hash_v1"

    def embed(
        self,
        request: EmbeddingRequest,
        call_records: list[EmbeddingCallMetadata] | None = None,
    ) -> EmbeddingResponse:
        start = perf_counter()
        vectors = [_local_sparse_vector(text) for text in request.texts]
        metadata = EmbeddingCallMetadata(
            mode=self.mode,
            model=self.model,
            provider_request_id="",
            input_count=len(request.texts),
            vector_dimension=LOCAL_SPARSE_DIMENSION,
            latency_ms=_elapsed_ms(start),
            cost_status="no_external_embedding",
            error_type="",
        )
        _record_call(metadata, call_records)
        return EmbeddingResponse(vectors=vectors, metadata=metadata)


class OpenAICompatibleEmbeddingProvider:
    mode = "openai_compatible"

    def __init__(
        self,
        app_settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = app_settings
        self.transport = transport
        self.model = app_settings.embedding_model

    def embed(
        self,
        request: EmbeddingRequest,
        call_records: list[EmbeddingCallMetadata] | None = None,
    ) -> EmbeddingResponse:
        start = perf_counter()
        request_id = ""
        try:
            self._validate_settings()
            response = self._post(request)
            request_id = response.headers.get("x-request-id", "")
            if response.status_code >= 400:
                raise _http_error(response.status_code)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Embedding response body must be an object")
            if not request_id and body.get("id") is not None:
                request_id = str(body["id"])
            vectors = _vectors_from_body(body, len(request.texts))
            dimension = len(vectors[0]) if vectors else 0
            metadata = self._metadata(
                request=request,
                request_id=request_id,
                vector_dimension=dimension,
                latency_ms=_elapsed_ms(start),
                error_type="",
            )
            _record_call(metadata, call_records)
            return EmbeddingResponse(vectors=vectors, metadata=metadata)
        except EmbeddingProviderError as exc:
            self._record_error(request, request_id, start, exc.error_type, call_records)
            raise
        except httpx.TimeoutException as exc:
            error = EmbeddingProviderError("timeout", "Embedding request timed out", True)
            self._record_error(request, request_id, start, error.error_type, call_records)
            raise error from exc
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            error = EmbeddingProviderError(
                "temporary_error",
                "Embedding request failed temporarily",
                True,
            )
            self._record_error(request, request_id, start, error.error_type, call_records)
            raise error from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            error = EmbeddingProviderError(
                "schema_error",
                "Embedding response is invalid",
                False,
            )
            self._record_error(request, request_id, start, error.error_type, call_records)
            raise error from exc

    def _validate_settings(self) -> None:
        if not self.settings.embedding_base_url:
            raise EmbeddingProviderError(
                "configuration_error",
                "Embedding Base URL is not configured",
                False,
            )
        if not self.settings.embedding_api_key:
            raise EmbeddingProviderError(
                "configuration_error",
                "Embedding API Key is not configured",
                False,
            )
        if not self.settings.embedding_model:
            raise EmbeddingProviderError(
                "configuration_error",
                "Embedding model is not configured",
                False,
            )
        if (
            not isfinite(self.settings.embedding_timeout_seconds)
            or self.settings.embedding_timeout_seconds <= 0
        ):
            raise EmbeddingProviderError(
                "configuration_error",
                "Embedding timeout must be positive",
                False,
            )

    def _post(self, request: EmbeddingRequest) -> httpx.Response:
        base_url = self.settings.embedding_base_url.rstrip("/")
        with httpx.Client(
            timeout=self.settings.embedding_timeout_seconds,
            transport=self.transport,
        ) as client:
            return client.post(
                f"{base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.settings.embedding_api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.settings.embedding_model, "input": request.texts},
            )

    def _metadata(
        self,
        request: EmbeddingRequest,
        request_id: str,
        vector_dimension: int | None,
        latency_ms: int,
        error_type: str,
    ) -> EmbeddingCallMetadata:
        return EmbeddingCallMetadata(
            mode=self.mode,
            model=self.settings.embedding_model,
            provider_request_id=request_id,
            input_count=len(request.texts),
            vector_dimension=vector_dimension,
            latency_ms=latency_ms,
            cost_status="未配置",
            error_type=error_type,
        )

    def _record_error(
        self,
        request: EmbeddingRequest,
        request_id: str,
        start: float,
        error_type: str,
        call_records: list[EmbeddingCallMetadata] | None,
    ) -> None:
        _record_call(
            self._metadata(
                request=request,
                request_id=request_id,
                vector_dimension=None,
                latency_ms=_elapsed_ms(start),
                error_type=error_type,
            ),
            call_records,
        )


def create_embedding_provider(
    app_settings: Settings = settings,
    transport: httpx.BaseTransport | None = None,
) -> EmbeddingProvider:
    if app_settings.embedding_mode == "local_sparse":
        return LocalSparseEmbeddingProvider()
    if app_settings.embedding_mode == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(app_settings, transport=transport)
    raise EmbeddingProviderError(
        "configuration_error",
        f"unsupported REVIEW_AGENT_EMBEDDING_MODE: {app_settings.embedding_mode}",
        False,
    )


def tokenize_text(text: str) -> list[str]:
    tokens: list[str] = []
    for raw_token in TOKEN_PATTERN.findall(str(text or "").lower()):
        if _is_chinese(raw_token):
            tokens.append(raw_token)
            tokens.extend(
                raw_token[index : index + 2]
                for index in range(max(0, len(raw_token) - 1))
            )
        else:
            tokens.append(raw_token)
    return [token for token in tokens if token]


def _local_sparse_vector(text: str) -> list[float]:
    counts = Counter(tokenize_text(text))
    vector = [0.0] * LOCAL_SPARSE_DIMENSION
    for token, count in counts.items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % LOCAL_SPARSE_DIMENSION
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign * count
    length = sqrt(sum(value * value for value in vector))
    if length:
        vector = [value / length for value in vector]
    return vector


def _vectors_from_body(body: dict, expected_count: int) -> list[list[float]]:
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise ValueError("Embedding response count does not match request")
    indexed_vectors: dict[int, list[float]] = {}
    expected_dimension: int | None = None
    for fallback_index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("Embedding data item must be an object")
        index = item.get("index", fallback_index)
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("Embedding index must be an integer")
        raw_vector = item.get("embedding")
        if not isinstance(raw_vector, list) or not raw_vector:
            raise ValueError("Embedding vector must be a non-empty list")
        vector = []
        for value in raw_vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Embedding values must be numeric")
            parsed = float(value)
            if not isfinite(parsed):
                raise ValueError("Embedding values must be finite")
            vector.append(parsed)
        if expected_dimension is None:
            expected_dimension = len(vector)
        elif len(vector) != expected_dimension:
            raise ValueError("Embedding vectors must have a fixed dimension")
        if index in indexed_vectors or index < 0 or index >= expected_count:
            raise ValueError("Embedding index is invalid")
        indexed_vectors[index] = vector
    if set(indexed_vectors) != set(range(expected_count)):
        raise ValueError("Embedding response indices are incomplete")
    return [indexed_vectors[index] for index in range(expected_count)]


def _record_call(
    metadata: EmbeddingCallMetadata,
    call_records: list[EmbeddingCallMetadata] | None,
) -> None:
    if call_records is not None:
        call_records.append(metadata)


def _http_error(status_code: int) -> EmbeddingProviderError:
    if status_code == 429:
        return EmbeddingProviderError(
            "rate_limit",
            "Embedding provider rate limit exceeded",
            True,
        )
    if status_code in {408, 409, 425} or status_code >= 500:
        return EmbeddingProviderError(
            "temporary_error",
            "Embedding provider is temporarily unavailable",
            True,
        )
    if status_code in {401, 403}:
        return EmbeddingProviderError(
            "authentication_error",
            "Embedding provider authentication failed",
            False,
        )
    return EmbeddingProviderError(
        "provider_error",
        f"Embedding provider returned HTTP {status_code}",
        False,
    )


def _is_chinese(token: str) -> bool:
    return all("\u4e00" <= char <= "\u9fff" for char in token)


def _elapsed_ms(start: float) -> int:
    return max(0, int((perf_counter() - start) * 1000))
