#!/usr/bin/env python3
"""Measure whether a shared ontology prefix is actually reused by the server's KV cache.

This is the measurement the hosted API could not answer. On MARA, `cached_tokens` is absent
and TTFT is flat across repeated identical prefixes (0.205/0.207/0.208 s, 2026-08-08): there
is no prefix cache to hit, so resend amplification costs money, not latency. A self-hosted
vLLM has one, which turns "put the ontology in every prompt" from a token-cost question
into a cache-residency question.

The probe follows the paired cold/warm protocol from SEOCHO's ADR-0148:

  * the prompt is assembled stable-first — task contract, then the ontology block, then the
    tool/output contract, then the question. Nothing volatile (no timestamp, no request id)
    goes in front of the ontology, or the shared prefix stops being byte-identical and the
    cache cannot match it.
  * `stable` arm: N requests, same prefix, different question tail. Request 1 is cold,
    2..N should be warm.
  * `salted` arm: the same N requests with a unique salt prepended *before* the ontology.
    This is the control that proves an observed warm effect came from prefix reuse rather
    than from the server warming up in general.
  * per request: TTFT (first streamed token), total latency, prompt/completion tokens,
    and `usage.prompt_tokens_details.cached_tokens` when the server reports it.
  * per arm: the delta in the server's own prefix-cache counters, scraped from /metrics.

Raw per-request samples are kept, not just medians — a median without samples cannot be
re-analysed later.

    python3 scripts/testbed/vllm_probe.py --model openai/gpt-oss-120b --repeats 6 \\
        --out results/bench/vllm_prefix_$(date +%Y%m%d_%H%M%S).json

Run it twice — once against a server started with prefix caching on, once off (see
testbed/serve_vllm.sh, PREFIX_CACHING=off) — and the pair is the A/B.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from harness.environment import get_inference_info, scrape_vllm_metrics  # noqa: E402
from harness.llm import model_config, sync_client  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "runmeta", REPO_ROOT / "scripts" / "analysis" / "runmeta.py")
runmeta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runmeta)  # type: ignore[union-attr]

TASK_CONTRACT = (
    "You are a financial AML analyst. Answer only from the graph schema you are given. "
    "Do not invent labels, relationship types or property names.")
OUTPUT_CONTRACT = (
    "Reply with one line of JSON: {\"cypher\": \"<a single read-only query>\"}. "
    "No prose, no code fences.")
QUESTIONS = [
    "How many incoming transfers does account {a} have?",
    "What is the largest amount account {a} ever sent?",
    "Which accounts sent money to account {a} over a high-risk channel?",
    "How many distinct accounts are two transfer hops upstream of account {a}?",
    "Which channel carries the most transactions for account {a}?",
    "What share of accounts sharing a login device with account {a} are flagged?",
]


# ~chars per token for this kind of English/YAML text. Only used to *aim* at a length; the
# number reported for every measurement is the server's own `prompt_tokens`.
CHARS_PER_TOKEN = 3.6


def prefix_of_length(base_text: str, target_tokens: Optional[int]) -> str:
    """A byte-stable shared prefix of roughly `target_tokens` tokens.

    Longer prefixes are built by repeating the ontology block. That is not the same thing as
    a genuinely larger ontology — the content repeats — but it is exactly the same thing to
    the cache: one byte-identical span of N tokens that every request in the arm shares. The
    axis this sweeps is *prefix length*, which is what decides whether the shared span still
    fits in the GPU's KV budget, and the honest label for each point is the measured
    `prompt_tokens` rather than the target.

    Why the axis matters at all: extending a model's context window is cheap now (Position
    Interpolation, Chen et al. 2023 — linear down-scaling of RoPE position indices, which vLLM
    exposes as `--rope-scaling '{"rope_type":"linear","factor":N}'`), so long shared prefixes
    are practical. KV footprint grows linearly with them, which moves the binding constraint
    from "can the model read this much" to "does the server still have it cached".
    """
    if not target_tokens:
        return base_text
    target_chars = int(target_tokens * CHARS_PER_TOKEN)
    if len(base_text) >= target_chars:
        return base_text[:target_chars]
    reps = -(-target_chars // (len(base_text) + 32))
    joined = "\n\n# --- schema block repeat ---\n\n".join([base_text] * reps)
    return joined[:target_chars]


def build_messages(ontology_text: str, question: str, *, salt: Optional[str] = None) -> List[Dict[str, str]]:
    """Stable sections first. The salt, when present, deliberately breaks the shared prefix."""
    system_parts = []
    if salt is not None:
        system_parts.append(f"Session token: {salt}")   # the control's poison, up front
    system_parts.append(TASK_CONTRACT)
    system_parts.append("Graph schema (ontology):\n" + ontology_text)
    system_parts.append(OUTPUT_CONTRACT)
    return [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": question},
    ]


def one_request(client, model: str, messages: List[Dict[str, str]], *, max_tokens: int,
                stream: bool) -> Dict[str, Any]:
    t0 = time.perf_counter()
    ttft = None
    text_len = 0
    usage: Any = None

    if stream:
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0.0, max_tokens=max_tokens,
            stream=True, stream_options={"include_usage": True})
        for chunk in resp:
            if chunk.usage is not None:
                usage = chunk.usage
            for choice in chunk.choices or []:
                delta = getattr(choice.delta, "content", None)
                if delta:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    text_len += len(delta)
    else:
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0.0, max_tokens=max_tokens)
        usage = resp.usage
        text_len = len(resp.choices[0].message.content or "")

    total = time.perf_counter() - t0
    cached = created = None
    prompt_tokens = completion_tokens = None
    if usage is not None:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            def _field(name):
                value = getattr(details, name, None)
                if value is None and isinstance(details, dict):
                    value = details.get(name)
                return value
            cached = _field("cached_tokens")
            created = _field("created_cache_tokens")
    return {
        "ttft_s": ttft, "total_s": total, "chars": text_len,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        # None means the server did not report the field at all (the hosted API's
        # behaviour, and vLLM's too unless started with --enable-prompt-tokens-details);
        # 0 means it reported a miss. The distinction matters.
        "cached_tokens": cached,
        # vLLM 0.27.1 also reports what this request *contributed* to the cache, which is
        # how you tell "nobody will ever reuse this" from "this one paid for the next".
        "created_cache_tokens": created,
        "cache_hit_ratio": (round(cached / prompt_tokens, 4)
                            if cached is not None and prompt_tokens else None),
    }


def _counters(metrics: Dict[str, Any]) -> Dict[str, float]:
    """Pull prefix-cache query/hit counters out of a /metrics scrape, whatever they are named."""
    out: Dict[str, float] = {}
    for line in metrics.get("lines", []):
        if "prefix_cache" not in line:
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            continue
    return out


def run_arm(client, cfg, ontology_text: str, *, salted: bool, repeats: int,
            max_tokens: int, stream: bool) -> Dict[str, Any]:
    before = scrape_vllm_metrics(cfg.base_url)
    samples = []
    for i in range(repeats):
        question = QUESTIONS[i % len(QUESTIONS)].format(a=1001 + i)
        salt = f"{os.getpid()}-{i}-{time.time_ns()}" if salted else None
        rec = one_request(client, cfg.model_name, build_messages(ontology_text, question, salt=salt),
                          max_tokens=max_tokens, stream=stream)
        rec.update({"index": i, "question": question, "salted": salted})
        samples.append(rec)
        print(f"  [{'salted' if salted else 'stable'} {i}] "
              f"ttft={rec['ttft_s'] if rec['ttft_s'] is None else round(rec['ttft_s'], 4)}s "
              f"total={rec['total_s']:.3f}s prompt={rec['prompt_tokens']} "
              f"cached={rec['cached_tokens']}", flush=True)
    after = scrape_vllm_metrics(cfg.base_url)

    def _hist_counts(metrics: Dict[str, Any]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for line in metrics.get("lines", []):
            if "kv_block" not in line or "_sum" not in line and "_count" not in line:
                continue
            name, _, value = line.rpartition(" ")
            try:
                out[name.strip()] = float(value)
            except ValueError:
                continue
        return out

    def _residency_delta(before_h: Dict[str, float], after_h: Dict[str, float]) -> Dict[str, Any]:
        """Mean block lifetime/idle/gap *for this arm*, from the histogram sum/count deltas.

        The raw histograms are cumulative, so reading them after the arm mixes in every
        earlier arm's blocks — which is how a control arm can appear to inherit the
        treatment's residency.
        """
        out: Dict[str, Any] = {}
        for key in [k for k in after_h if k.endswith("_count")]:
            base = key[: -len("_count")]
            d_count = after_h[key] - before_h.get(key, 0.0)
            d_sum = after_h.get(base + "_sum", 0.0) - before_h.get(base + "_sum", 0.0)
            if d_count > 0:
                out[base.split("{")[0]] = {"count": d_count, "mean_s": round(d_sum / d_count, 4)}
        return out

    def med(key: str, rows) -> Optional[float]:
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(statistics.median(vals), 6) if vals else None

    cold, warm = samples[:1], samples[1:]
    # vllm 0.27.1 names these `vllm:prefix_cache_queries_total` / `..._hits_total` (not the
    # `gpu_prefix_cache_*` of earlier releases) and counts them in *tokens*, so their ratio
    # is a token-level hit rate directly comparable to `cached_tokens / prompt_tokens`.
    # There is also an `external_prefix_cache_*` pair — the offload tier (LMCache and
    # friends), zero unless one is configured.
    def _rate(delta: Dict[str, float], kind: str) -> Optional[float]:
        qs = sum(v for k, v in delta.items() if f"{kind}prefix_cache_queries_total" in k)
        hs = sum(v for k, v in delta.items() if f"{kind}prefix_cache_hits_total" in k)
        return round(hs / qs, 4) if qs else None
    # Counters get a delta; gauges get their final value — subtracting two rates produces a
    # number that looks like a measurement and is not one. Note the `_total` suffix is added
    # by the exposition layer, not the metric name: vLLM declares `vllm:prefix_cache_queries`
    # and Prometheus renders `vllm:prefix_cache_queries_total`, per the OpenMetrics rule the
    # vLLM metrics design doc calls out.
    before_c, after_c = _counters(before), _counters(after)
    delta = {k: (round(v - before_c.get(k, 0.0), 6) if "_total" in k else round(v, 6))
             for k, v in after_c.items()}
    return {
        "arm": "salted" if salted else "stable",
        "repeats": repeats,
        "cold": {"ttft_s": med("ttft_s", cold), "total_s": med("total_s", cold),
                 "cached_tokens": med("cached_tokens", cold)},
        "warm": {"ttft_s": med("ttft_s", warm), "total_s": med("total_s", warm),
                 "cached_tokens": med("cached_tokens", warm)},
        "prefix_cache_counter_delta": delta or None,
        "server_token_hit_rate": _rate(delta, "vllm:") if delta else None,
        "server_external_token_hit_rate": _rate(delta, "vllm:external_") if delta else None,
        "metrics_reachable": after.get("reachable", False),
        # vllm:kv_block_lifetime_seconds / _idle_before_evict_seconds / _reuse_gap_seconds —
        # only present when the server ran with --kv-cache-metrics.
        "kv_block_residency": _residency_delta(_hist_counts(before), _hist_counts(after)) or None,
        "samples": samples,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default=os.getenv("MODEL_PROVIDER", "vllm"),
                    choices=("vllm", "mara"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--prefix-file", default="ontology/finbench.ontology.yaml",
                    help="the stable block whose reuse is being measured")
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--no-stream", action="store_true",
                    help="skip TTFT (needed for endpoints that do not stream)")
    ap.add_argument("--skip-salted", action="store_true", help="run the stable arm only")
    ap.add_argument("--prefix-tokens", type=int, default=None,
                    help="pad the shared prefix to about this many tokens")
    ap.add_argument("--sweep", default=None,
                    help="comma-separated prefix token targets, e.g. 2000,8000,32000,65000 — "
                         "one stable+salted pair per size, in one process so the server's "
                         "cache state carries across the sweep the way a real workload's would")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = model_config(args, max_tokens=args.max_tokens)
    ontology_text = Path(args.prefix_file).read_text()
    client = sync_client(cfg)
    print(f"[probe] {cfg.provider} {cfg.model_name} @ {cfg.base_url} — "
          f"prefix {len(ontology_text)} chars from {args.prefix_file}", flush=True)

    sizes: List[Optional[int]] = ([int(x) for x in args.sweep.split(",")] if args.sweep
                                  else [args.prefix_tokens or None])
    arms = []
    for target in sizes:
        text = prefix_of_length(ontology_text, target)
        print(f"[probe] prefix target={target or 'native'} tokens "
              f"({len(text)} chars)", flush=True)
        arm = run_arm(client, cfg, text, salted=False, repeats=args.repeats,
                      max_tokens=args.max_tokens, stream=not args.no_stream)
        arm["prefix_target_tokens"] = target
        arm["prefix_chars"] = len(text)
        arms.append(arm)
        if not args.skip_salted:
            control = run_arm(client, cfg, text, salted=True, repeats=args.repeats,
                              max_tokens=args.max_tokens, stream=not args.no_stream)
            control["prefix_target_tokens"] = target
            control["prefix_chars"] = len(text)
            arms.append(control)

    report = {
        "schema_version": "seocho.finbench.vllm-prefix-probe.v1",
        "manifest": runmeta.manifest(),
        "endpoint": get_inference_info(cfg.base_url, model_descriptor=cfg.descriptor()),
        "prefix": {"file": args.prefix_file, "chars": len(ontology_text),
                   "sweep": args.sweep, "chars_per_token_assumed": CHARS_PER_TOKEN},
        "arms": arms,
    }
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n")
        print(f"\nwrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
