"""One LLM connector for every script in the repo.

Five call sites each grew their own copy of "read MARA_API_KEY, fall back to parsing
.env, build an AsyncOpenAI against api.cloud.mara.com" — which meant pointing the
experiment at a self-hosted vLLM would have taken five edits, and any one of them
missed would have quietly kept talking to the hosted API while the run's manifest
claimed otherwise. The endpoint is a measured variable in this experiment (MARA has no
prefix caching; a local vLLM does), so it has to be resolved in one place and recorded.

Usage in a script with argparse:

    from harness.llm import add_provider_args, model_config, agents_model
    add_provider_args(parser)
    ...
    cfg = model_config(args)                      # provider/model/base-url resolved once
    model = agents_model(cfg)                     # Agents SDK
    client = async_client(cfg)                    # plain chat-completions

`ModelConfig.descriptor()` is what belongs in the run manifest — provider, model,
base_url, sampling, and whether a key was used, never the key itself.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, Optional

from harness.config import PROVIDER_SPECS, ModelConfig

__all__ = [
    "PROVIDER_SPECS", "ModelConfig", "add_provider_args", "model_config",
    "default_config", "async_client", "sync_client", "agents_model",
]

_DEFAULT: Optional[ModelConfig] = None


def default_config() -> ModelConfig:
    """The environment's endpoint, resolved once per process.

    For the scripts that take no provider flag of their own — the single-question demos and
    the two proposal benchmarks — MODEL_PROVIDER/*_MODEL/*_BASE_URL is the whole interface.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = model_config()
    return _DEFAULT


def add_provider_args(parser: argparse.ArgumentParser, *, default_model: Optional[str] = None) -> None:
    """Add --provider/--model/--base-url. Defaults come from the environment, so a
    testbed exports MODEL_PROVIDER once instead of threading a flag through every call."""
    parser.add_argument("--provider", choices=sorted(PROVIDER_SPECS),
                        default=os.getenv("MODEL_PROVIDER", "mara"),
                        help="endpoint family (default from MODEL_PROVIDER, else mara)")
    parser.add_argument("--model", default=default_model,
                        help="served model name; for vllm it must match GET /v1/models")
    parser.add_argument("--base-url", default=None,
                        help="override the provider's base_url (default from *_BASE_URL)")


def model_config(args: Any = None, /, **overrides: Any) -> ModelConfig:
    """Build a ModelConfig from parsed args (any subset of provider/model/base_url) plus
    keyword overrides. Unset fields fall back to the provider spec and the environment."""
    fields: Dict[str, Any] = {}
    if args is not None:
        for src, dest in (("provider", "provider"), ("model", "model_name"),
                          ("base_url", "base_url"), ("max_tokens", "max_tokens"),
                          ("temperature", "temperature")):
            value = getattr(args, src, None)
            if value is not None:
                fields[dest] = value
    fields.update({k: v for k, v in overrides.items() if v is not None})
    return ModelConfig(**fields)


def _propagating_http_client(async_: bool):
    """An httpx client that stamps the active OTel context onto every outgoing request.

    vLLM extracts `traceparent`/`tracestate` from the request headers
    (`vllm/tracing/otel.py:127`) and parents its own request span to that context. So if the
    episode is running inside a span, the model call — with its queue time, prefill time and
    TTFT as span attributes — lands *inside the same trace* as the Cypher the episode ran.
    Without this, the agent side and the serving side are two traces that no one can join,
    which is the exact failure this repo keeps arguing against for the graph interface.

    Injection happens at send time, not client construction, because a static
    `default_headers` would freeze one trace id for the whole process. Returns None when
    OpenTelemetry or httpx is unavailable — the caller then builds a plain client.
    """
    if os.getenv("TRACE_PROPAGATION", "on") == "off":
        return None
    try:
        import httpx
        from opentelemetry import propagate, trace
    except ImportError:
        return None

    def inject(request) -> None:
        if trace.get_current_span().get_span_context().is_valid:
            propagate.inject(request.headers)

    hooks = {"request": [inject]}
    return httpx.AsyncClient(event_hooks=hooks) if async_ else httpx.Client(event_hooks=hooks)


def async_client(cfg: ModelConfig):
    from openai import AsyncOpenAI
    http_client = _propagating_http_client(async_=True)
    kwargs = cfg.client_kwargs()
    if http_client is not None:
        kwargs["http_client"] = http_client
    return AsyncOpenAI(**kwargs)


def sync_client(cfg: ModelConfig):
    from openai import OpenAI
    http_client = _propagating_http_client(async_=False)
    kwargs = cfg.client_kwargs()
    if http_client is not None:
        kwargs["http_client"] = http_client
    return OpenAI(**kwargs)


def agents_model(cfg: ModelConfig):
    """An Agents SDK model bound to the resolved endpoint.

    vLLM serves the same chat-completions surface including tool calls, so no adapter
    branch is needed here — the same reasoning SEOCHO's own vLLM preset relies on
    (ADR-0098): the provider difference is a base_url and a key policy.
    """
    from agents import OpenAIChatCompletionsModel
    return OpenAIChatCompletionsModel(model=cfg.model_name, openai_client=async_client(cfg))
