#!/usr/bin/env python3
"""Turn measured episodes into an LLMServingSim agentic workload.

Why this exists: the questions left over from the H200 run are all about scale — 100 tenants
instead of 36, a CXL tier that no rental offers, weights pushed off the accelerator so the KV
budget doubles. None of them can be bought; all of them are configuration in a serving
simulator. What the simulator cannot invent is the workload: how many LLM calls an episode
takes, how long each prompt is, how long the tool runs between calls, and — the part that
decides everything about caching — *which spans of tokens are shared* between calls and
between sessions.

That last point is why this writes token ids. LLMServingSim's docs are explicit: without
`input_tok_ids` prefix caching is disabled for the request, and `output_tok_ids` are what let
turn k+1 hit on turn k's generation. Emitting only counts would silently measure a world with
no cache at all.

Mapping, one line per episode:

    session_id      <sf>_<arm>_<question>_r<repeat>
    sub_requests[i] one LLM call: input_toks/output_toks from `turns[i]`
    tool_duration_ns the database time between calls — `calls[i]["ms"]`, the real measured
                    wait, not a guess
    arrival_time_ns a seeded Poisson process at --rate sessions/s

Arrival times are synthesised on purpose. The episodes were run under a concurrency
semaphore, not at a controlled arrival rate, and the harness does not record absolute start
times — so replaying "the original timing" would be inventing it. A seeded Poisson process is
both honest and more useful: load is the axis you want to sweep.

    python3 scripts/testbed/to_llmservingsim.py \\
        --episodes results/episodes/agent_interaction.json \\
        --conversations results/episodes/conversations.jsonl \\
        --rate 2.0 --out workloads/aisummit26_agentic.jsonl

Fidelity, stated plainly: the token ids come from re-rendering the recorded conversation and
tokenising it locally (o200k_base by default). They are not the exact ids the server saw —
chat templates, tool-call framing and reasoning channels differ. What is preserved exactly is
the *sharing structure*: the arm's instruction block is byte-identical across every session
that used it, and each turn's prompt is a strict prefix extension of the previous turn's.
That structure is what a prefix cache keys on, so it is the part that has to be right.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_ENCODING = "o200k_base"


def _encoder(name: str):
    import tiktoken
    return tiktoken.get_encoding(name)


def load_episodes(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    doc = json.loads(text)
    return doc["episodes"] if isinstance(doc, dict) else doc


def episode_key(e: Dict[str, Any]) -> Tuple[Any, ...]:
    return (e.get("database"), e.get("arm"), e.get("question_id"), e.get("repeat"))


def load_conversations(path: Optional[Path]) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
    if path is None:
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        out[(c.get("database"), c.get("arm"), c.get("question_id"), c.get("repeat"))] = c
    return out


def render_item(item: Any) -> str:
    """One conversation item as the text that would occupy the prompt.

    Deterministic and stable, not a chat template: what matters is that identical items
    render identically (so shared spans stay shared) and that lengths are of the right order.
    """
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    kind = item.get("type") or item.get("role") or "item"
    if kind == "function_call":
        return f"<tool_call name={item.get('name')}>{item.get('arguments','')}</tool_call>"
    if kind == "function_call_output":
        return f"<tool_result>{item.get('output','')}</tool_result>"
    content = item.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                          for part in content)
    return f"<{kind}>{content if content is not None else ''}</{kind}>"


def turn_texts(conv: Dict[str, Any], n_turns: int) -> List[Tuple[str, str]]:
    """(prompt, generation) text per turn, reconstructed from the recorded conversation.

    Turn 0's prompt is the instruction block plus the question; every later turn's prompt is
    the previous prompt plus whatever was appended since — which is exactly the nesting a
    prefix cache exploits, and why agentic sessions have such high overlap.
    """
    base = (conv.get("instructions") or "") + "\n\n" + (conv.get("prompt") or "")
    items = conv.get("conversation") or []
    # Drop the leading user message: it is already in `base`.
    tail = [it for it in items if not (isinstance(it, dict) and it.get("role") == "user"
                                      and it.get("content") == conv.get("prompt"))]

    out: List[Tuple[str, str]] = []
    prompt = base
    produced: List[Any] = []
    for it in tail:
        kind = (it.get("type") or it.get("role")) if isinstance(it, dict) else None
        if kind in ("function_call", "assistant", "message", "output_text"):
            produced.append(it)
            out.append((prompt, render_item(it)))
            prompt = prompt + "\n" + render_item(it)
        else:
            # tool output and anything else is context for the *next* turn, not a generation
            prompt = prompt + "\n" + render_item(it)
    # `turns` is authoritative for how many calls happened; pad or trim to match it.
    while len(out) < n_turns:
        out.append((prompt, ""))
    return out[:n_turns]


def build_session(e: Dict[str, Any], conv: Optional[Dict[str, Any]], enc,
                  arrival_ns: int, include_ids: bool) -> Optional[Dict[str, Any]]:
    turns = e.get("turns") or []
    if not turns:
        return None
    calls = e.get("calls") or []
    texts = turn_texts(conv, len(turns)) if conv else []

    subs = []
    for i, t in enumerate(turns):
        # The tool wait after this call: the database time of the i-th query, in ns. The last
        # call is followed by nothing, which is the convention their loader expects.
        tool_ns = int(round(float(calls[i]["ms"]) * 1e6)) if i < len(calls) else 0
        sub: Dict[str, Any] = {
            "input_toks": int(t.get("input_tokens") or 0),
            "output_toks": max(int(t.get("output_tokens") or 0), 1),
            "tool_duration_ns": tool_ns if i < len(turns) - 1 else 0,
        }
        if include_ids and i < len(texts):
            prompt_text, gen_text = texts[i]
            in_ids = enc.encode(prompt_text, disallowed_special=())
            out_ids = enc.encode(gen_text, disallowed_special=()) if gen_text else []
            # Keep the *measured* counts as the scheduler's truth and the ids for hashing;
            # when re-rendering drifts from the server's tokenisation, trust the measurement.
            sub["input_tok_ids"] = in_ids
            sub["output_tok_ids"] = out_ids or [0]
        subs.append(sub)

    return {
        "session_id": f"sf{e.get('sf')}_{e.get('arm')}_{e.get('question_id')}_r{e.get('repeat')}",
        "arrival_time_ns": arrival_ns,
        "sub_requests": subs,
    }


def poisson_arrivals(n: int, rate_per_s: float, seed: int) -> List[int]:
    rng = random.Random(seed)
    t = 0.0
    out = []
    for _ in range(n):
        t += rng.expovariate(rate_per_s) if rate_per_s > 0 else 0.0
        out.append(int(t * 1e9))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", required=True, type=Path)
    ap.add_argument("--conversations", type=Path, default=None,
                    help="the --log-conversations JSONL. Without it no token ids are emitted, "
                         "which disables prefix caching in the simulator")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--rate", type=float, default=2.0, help="sessions per second (Poisson)")
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--encoding", default=DEFAULT_ENCODING)
    ap.add_argument("--arms", nargs="*", default=None, help="restrict to these arms")
    ap.add_argument("--sf", type=int, default=None, help="restrict to one scale factor")
    ap.add_argument("--repeat-sessions", type=int, default=1,
                    help="emit each episode N times, as N tenants sharing the arm's "
                         "instruction prefix — the axis that produced the eviction cliff")
    ap.add_argument("--no-ids", action="store_true", help="counts only (no prefix caching)")
    args = ap.parse_args()

    episodes = load_episodes(args.episodes)
    convs = load_conversations(args.conversations)
    if args.arms:
        episodes = [e for e in episodes if e.get("arm") in set(args.arms)]
    if args.sf is not None:
        episodes = [e for e in episodes if e.get("sf") == args.sf]

    include_ids = not args.no_ids and bool(convs)
    enc = _encoder(args.encoding) if include_ids else None

    expanded: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]] = []
    for e in episodes:
        conv = convs.get(episode_key(e))
        for copy_idx in range(args.repeat_sessions):
            expanded.append((e, conv, copy_idx))
    arrivals = poisson_arrivals(len(expanded), args.rate, args.seed)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    with args.out.open("w") as fh:
        for (e, conv, copy_idx), arrival in zip(expanded, arrivals):
            s = build_session(e, conv, enc, arrival, include_ids)
            if s is None:
                skipped += 1
                continue
            if args.repeat_sessions > 1:
                s["session_id"] = f"{s['session_id']}_t{copy_idx}"
            fh.write(json.dumps(s) + "\n")
            written += 1

    meta = {
        "schema_version": "seocho.finbench.llmservingsim-workload.v1",
        "source_episodes": str(args.episodes),
        "source_conversations": str(args.conversations) if args.conversations else None,
        "sessions": written,
        "episodes_skipped_no_turns": skipped,
        "arrival_process": {"kind": "poisson", "rate_per_s": args.rate, "seed": args.seed},
        "token_ids": {"emitted": include_ids, "encoding": args.encoding if include_ids else None,
                      "fidelity": "re-rendered conversation, local tokeniser; sharing "
                                  "structure exact, absolute ids approximate"},
        "tenants_per_episode": args.repeat_sessions,
        "arms": sorted({e.get("arm") for e in episodes}),
        "scale_factors": sorted({e.get("sf") for e in episodes if e.get("sf") is not None}),
    }
    Path(str(args.out) + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))
    print(f"\nwrote {written} sessions -> {args.out}")


if __name__ == "__main__":
    main()
