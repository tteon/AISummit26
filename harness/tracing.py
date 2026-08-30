"""Spans for the episode loop, so the joined trace exists in a real run.

`harness/llm.py` stamps the active OpenTelemetry context onto every model request, and vLLM
parents its own span to whatever arrives — but only if something *created* a span first. The
harness never did, which meant the "one trace holds episode -> Cypher -> model" property was
demonstrable with a hand-written script and absent from every actual run. This module is the
missing half.

Deliberately inert unless asked for: with no OTLP endpoint configured, `init_tracing` returns
a tracer whose spans go nowhere, `span()` costs a context manager, and no dependency is
required. Tracing must never be the reason a measurement run fails.

    from harness.tracing import init_tracing, span
    init_tracing("aisummit26-harness")          # once, at startup
    with span("episode", arm=arm, question_id=qid, sf=sf):
        ...
"""

from __future__ import annotations

import os
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

_TRACER: Any = None
_INITIALISED = False

# The same names the OTel SDK and the testbed's collector already use, so one export is
# enough to configure both sides.
_ENDPOINT_ENV = ("OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT", "SEOCHO_METRICS_OTLP_ENDPOINT")


def _endpoint() -> Optional[str]:
    for name in _ENDPOINT_ENV:
        value = os.getenv(name)
        if value:
            return value
    return None


def init_tracing(service_name: str = "aisummit26-harness") -> Any:
    """Configure an OTLP tracer if an endpoint is set; otherwise stay a no-op.

    Idempotent: the episode runner and a benchmark script in the same process must not
    install two providers.
    """
    global _TRACER, _INITIALISED
    if _INITIALISED:
        return _TRACER
    _INITIALISED = True

    endpoint = _endpoint()
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult,
        )
    except ImportError:
        # Tracing is never worth failing a run over; testbed/requirements.txt carries the SDK.
        return None

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))

    jsonl_path = os.getenv("TRACE_JSONL_PATH")
    if jsonl_path:
        class _JSONLSpanExporter(SpanExporter):
            """Durable neutral trace artifact, independent of collector availability."""

            def export(self, spans: Any) -> Any:
                path = Path(jsonl_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as out:
                    for item in spans:
                        context = item.context
                        parent = item.parent
                        out.write(json.dumps({
                            "schema_version": "seocho.local-otel-span.v1",
                            "trace_id": f"{context.trace_id:032x}",
                            "span_id": f"{context.span_id:016x}",
                            "parent_span_id": (f"{parent.span_id:016x}" if parent else None),
                            "name": item.name,
                            "start_time_unix_nano": item.start_time,
                            "end_time_unix_nano": item.end_time,
                            "duration_ms": ((item.end_time - item.start_time) / 1_000_000
                                            if item.end_time and item.start_time else None),
                            "attributes": dict(item.attributes or {}),
                            "status": str(item.status.status_code),
                            "resource": dict(item.resource.attributes or {}),
                        }, default=str) + "\n")
                    out.flush()
                    os.fsync(out.fileno())
                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                return None

        # SimpleSpanProcessor makes every completed stage durable before the next paid call.
        provider.add_span_processor(SimpleSpanProcessor(_JSONLSpanExporter()))
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("aisummit26")
    return _TRACER


def shutdown_tracing() -> None:
    """Flush before the process exits, or the last episodes' spans are lost."""
    if _TRACER is None:
        return
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()
        # TracerProvider registers its own atexit shutdown. Calling shutdown here and again
        # at process exit produces "shutdown can only be called once" and can race the last
        # exporter batch. A force-flush is sufficient for run durability.
    except Exception:
        pass


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span when tracing is on, a no-op context manager when it is not."""
    if _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as sp:
        for key, value in attributes.items():
            if value is not None:
                sp.set_attribute(key, value)
        yield sp
