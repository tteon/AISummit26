#!/usr/bin/env python3
"""A minimal OpenAI-compatible server, for verifying the vLLM wiring without a GPU.

The connector, the CLI plumbing, the manifest fields and the resume logic can all be wrong
in ways that only show up when a request is actually made — and finding that out on a
rented H200 costs money per minute. This stub answers the same surface vLLM does
(`GET /v1/models`, `POST /v1/chat/completions` with tool calls, `GET /metrics`), so the
whole episode loop can be exercised against `--provider vllm` on a box with no driver.

It also *simulates* prefix caching: it hashes the message prefix, remembers what it has
seen, and reports `usage.prompt_tokens_details.cached_tokens` accordingly. That field is
absent on MARA (verified 2026-08-08), so this is the only way to test the code path that
reads it before the real server exists.

    python3 scripts/testbed/stub_openai.py --port 8111 &
    python3 scripts/agents/agent_interaction.py --provider vllm \\
        --base-url http://127.0.0.1:8111/v1 --model stub-model ...

Not a mock of the model: the canned tool call is one fixed Cypher query. It proves the
loop runs, never that an answer is right.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

MODEL_ID = "stub-model"
DEFAULT_CYPHER = ("MATCH (:Account {acct_no:$a,_workspace_id:$ws})<-[t:TRANSFER]-"
                  "(:Account {_workspace_id:$ws}) RETURN count(t) AS n, sum(t.amount) AS total")

# prefix hash -> first-seen monotonic time; stands in for the KV blocks a real server keeps
_SEEN_PREFIXES: Dict[str, float] = {}
_STATS = {"requests": 0, "tool_calls": 0, "finals": 0, "cache_hits": 0,
          # Counting the header turns "the episodes ran" into "trace propagation
          # actually worked on the async path" — the distinction that hid a dead
          # episode loop for three hours (a sync httpx hook on an AsyncClient).
          "traceparent_seen": 0}


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _prefix_key(messages: List[Dict[str, Any]]) -> str:
    """Everything except the last message — the part a real server could reuse."""
    body = json.dumps(messages[:-1], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the episode output readable
        pass

    def _send(self, code: int, payload: Any, content_type: str = "application/json") -> None:
        body = (payload if isinstance(payload, bytes)
                else json.dumps(payload).encode() if content_type == "application/json"
                else str(payload).encode())
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [
                {"id": self.server.model_id, "object": "model", "owned_by": "stub"}]})
        if self.path.startswith("/metrics"):
            hit_rate = (_STATS["cache_hits"] / _STATS["requests"]) if _STATS["requests"] else 0.0
            text = (
                "# HELP vllm:gpu_prefix_cache_queries_total Prefix cache queries.\n"
                f'vllm:gpu_prefix_cache_queries_total{{model_name="{self.server.model_id}"}} '
                f'{_STATS["requests"]}\n'
                f'vllm:gpu_prefix_cache_hits_total{{model_name="{self.server.model_id}"}} '
                f'{_STATS["cache_hits"]}\n'
                f'vllm:gpu_prefix_cache_hit_rate{{model_name="{self.server.model_id}"}} '
                f'{hit_rate:.4f}\n'
                f'vllm:cache_config_info{{block_size="16",enable_prefix_caching="True"}} 1\n'
                f'stub:traceparent_seen_total {_STATS["traceparent_seen"]}\n'
                f'stub:requests_total {_STATS["requests"]}\n')
            return self._send(200, text.encode(), "text/plain; version=0.0.4")
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        messages = req.get("messages", [])
        _STATS["requests"] += 1
        if self.headers.get("traceparent"):
            _STATS["traceparent_seen"] += 1

        key = _prefix_key(messages) if len(messages) > 1 else None
        cached = 0
        if key is not None:
            if key in _SEEN_PREFIXES:
                _STATS["cache_hits"] += 1
                cached = _approx_tokens(json.dumps(messages[:-1], ensure_ascii=False))
            else:
                _SEEN_PREFIXES[key] = time.monotonic()

        already_ran = any(m.get("role") == "tool" for m in messages)
        prompt_tokens = _approx_tokens(json.dumps(messages, ensure_ascii=False))

        if not already_ran and req.get("tools"):
            _STATS["tool_calls"] += 1
            message = {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_stub_1", "type": "function",
                    "function": {"name": "run_cypher",
                                 "arguments": json.dumps({"cypher": self.server.cypher})},
                }],
            }
            finish = "tool_calls"
        else:
            _STATS["finals"] += 1
            rows = ""
            for m in reversed(messages):
                if m.get("role") == "tool":
                    rows = str(m.get("content") or "")[:400]
                    break
            numbers = re.findall(r"-?\d+(?:\.\d+)?", rows)
            answer = {"answer": numbers[:4] or ["0"], "note": "stub answer from tool rows"}
            message = {"role": "assistant",
                       "content": json.dumps(answer, ensure_ascii=False)}
            finish = "stop"

        completion_tokens = _approx_tokens(json.dumps(message, ensure_ascii=False))
        self._send(200, {
            "id": f"chatcmpl-stub-{_STATS['requests']}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", self.server.model_id),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                # The field MARA never sends and vLLM does.
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        })


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8111)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=MODEL_ID)
    ap.add_argument("--cypher", default=DEFAULT_CYPHER,
                    help="the query the stub always asks the tool to run")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.model_id = args.model
    server.cypher = args.cypher
    print(f"[stub] {args.model} on http://{args.host}:{args.port}/v1 (ctrl-c to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[stub] {_STATS}")


if __name__ == "__main__":
    main()
