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
from contextlib import contextmanager
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
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # Tracing is never worth failing a run over; testbed/requirements.txt carries the SDK.
        return None

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
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
        if hasattr(provider, "shutdown"):
            provider.shutdown()
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
