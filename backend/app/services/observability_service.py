from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from urllib.parse import unquote

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Status, StatusCode

from config import Settings


_LOCK = Lock()
_RUNTIME: "ObservabilityRuntime | None" = None


@dataclass
class ObservabilityRuntime:
    enabled: bool
    provider: TracerProvider | None = None
    tracer: trace.Tracer | None = None

    def shutdown(self) -> None:
        if self.provider is not None:
            self.provider.shutdown()


def configure_observability(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
) -> ObservabilityRuntime:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is not None:
            _RUNTIME.shutdown()
            _RUNTIME = None
        if not settings.observability_enabled:
            _RUNTIME = ObservabilityRuntime(enabled=False)
            return _RUNTIME
        if span_exporter is None and not settings.otel_exporter_otlp_traces_endpoint:
            raise ValueError(
                "REVIEW_AGENT_OBSERVABILITY_ENABLED requires "
                "REVIEW_AGENT_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
            )

        exporter = span_exporter or OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_traces_endpoint,
            headers=_parse_otlp_headers(settings.otel_exporter_otlp_headers),
            timeout=settings.otel_export_timeout_seconds,
        )
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.otel_service_name
                    or "contract-review-agent",
                }
            ),
            shutdown_on_exit=False,
        )
        processor = (
            SimpleSpanProcessor(exporter)
            if span_exporter is not None
            else BatchSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)
        _RUNTIME = ObservabilityRuntime(
            enabled=True,
            provider=provider,
            tracer=provider.get_tracer("contract-review-agent.tools"),
        )
        return _RUNTIME


def shutdown_observability() -> None:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is not None:
            _RUNTIME.shutdown()
        _RUNTIME = None


@contextmanager
def trace_tool_call(
    *,
    task_id: str,
    trace_id: str,
    step_id: str,
    step_name: str,
    tool_name: str,
    retry_index: int,
):
    runtime = _RUNTIME
    if runtime is None or not runtime.enabled or runtime.tracer is None:
        with nullcontext(None) as span:
            yield span
        return

    started_at = perf_counter()
    with runtime.tracer.start_as_current_span(
        f"tool.{tool_name}",
        record_exception=False,
        set_status_on_exception=False,
        attributes={
            "review.task_id": task_id,
            "review.trace_id": trace_id,
            "review.step_id": step_id,
            "review.step_name": step_name,
            "review.tool_name": tool_name,
            "review.retry_index": retry_index,
        },
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.set_attribute("review.status", "failed")
            span.set_attribute("error.type", exc.__class__.__name__)
            span.set_status(Status(StatusCode.ERROR, exc.__class__.__name__))
            raise
        else:
            span.set_attribute("review.status", "success")
            span.set_status(Status(StatusCode.OK))
        finally:
            span.set_attribute(
                "review.latency_ms",
                max(0, int((perf_counter() - started_at) * 1000)),
            )


def _parse_otlp_headers(value: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "REVIEW_AGENT_OTEL_EXPORTER_OTLP_HEADERS entries must use key=value"
            )
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(
                "REVIEW_AGENT_OTEL_EXPORTER_OTLP_HEADERS contains an empty key"
            )
        headers[key] = unquote(raw_value.strip())
    return headers
