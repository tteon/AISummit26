#!/usr/bin/env bash
# Serve one model with vLLM and wait until it answers, or fail loudly.
#
# Ported from the 2026-08-15 seocho<->vLLM wiring smoke (~/vllm-env/seocho_vllm_smoke.sh),
# which is the only recipe in this project that has actually served a model. Three parts of
# it are kept because each one caught a real failure:
#
#   * the GPU gate — this box shares its GPU, and a serve that starts while another process
#     holds the memory dies minutes later with an allocator error that reads like a bug
#   * the readiness poll against /v1/models — "the process is running" is not "the server
#     answers"; weights load for tens of seconds
#   * the log tail on death — vLLM's fatal errors are 200 lines above the exit
#
# What is new here is PREFIX_CACHING. vLLM V1 enables prefix caching by default, so the
# control arm of the experiment this testbed exists for MUST pass --no-enable-prefix-caching
# explicitly; a control that silently caches would show no effect and be read as "prefix
# reuse does not help".
#
# Usage:
#   testbed/serve_vllm.sh                              # serve, wait, stay in foreground
#   PREFIX_CACHING=off testbed/serve_vllm.sh           # the control arm
#   MODE=wait testbed/serve_vllm.sh                    # only wait for an existing server
set -uo pipefail

MODEL="${VLLM_MODEL:-openai/gpt-oss-120b}"
PORT="${VLLM_PORT:-8000}"
HOST="${VLLM_HOST:-0.0.0.0}"
SERVED_NAME="${VLLM_SERVED_NAME:-$MODEL}"
GPU_UTIL="${VLLM_GPU_UTIL:-0.90}"
MAX_LEN="${VLLM_MAX_MODEL_LEN:-32768}"
PREFIX_CACHING="${PREFIX_CACHING:-on}"
GPU_FREE_MIB="${GPU_FREE_MIB:-2000}"
GPU_WAIT_S="${GPU_WAIT_S:-1800}"
READY_WAIT_S="${READY_WAIT_S:-1800}"
LOG="${VLLM_LOG:-outputs/testbed/vllm_serve.log}"
FLAGS_OUT="${VLLM_FLAGS_OUT:-outputs/testbed/vllm_serve_flags.json}"
MODE="${MODE:-serve}"
VLLM_BIN="${VLLM_BIN:-vllm}"

mkdir -p "$(dirname "$LOG")" "$(dirname "$FLAGS_OUT")"

wait_ready() {
  local waited=0
  while [ "$waited" -lt "$READY_WAIT_S" ]; do
    if curl -sf -m 3 "http://127.0.0.1:${PORT}/v1/models" | grep -q "$SERVED_NAME"; then
      echo "[serve] ready after ${waited}s"; return 0
    fi
    if [ -n "${SERVER_PID:-}" ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "[serve] DIED — last 25 log lines:"; tail -25 "$LOG"; return 3
    fi
    sleep 5; waited=$((waited + 5))
  done
  echo "[serve] TIMEOUT after ${READY_WAIT_S}s"; tail -15 "$LOG"; return 4
}

if [ "$MODE" = "wait" ]; then wait_ready; exit $?; fi

# --- GPU gate -------------------------------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
  waited=0
  while :; do
    used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | sort -rn | head -1)
    # An empty answer means the driver is not responding (this happens on a host whose
    # kernel module was updated under a running container). Waiting 30 minutes for a
    # driver to heal is not a gate, it is a hang — fail while the rental is cheap.
    if ! [ "${used:-}" -eq "${used:-}" ] 2>/dev/null; then
      echo "[gate] nvidia-smi returned no usable memory reading — driver not responding"; exit 2
    fi
    if [ "$used" -lt "$GPU_FREE_MIB" ]; then
      echo "[gate] GPU free (${used}MiB used) after ${waited}s"; break
    fi
    if [ "$waited" -ge "$GPU_WAIT_S" ]; then
      echo "[gate] TIMEOUT: GPU still busy (${used}MiB used)"; exit 2
    fi
    sleep 30; waited=$((waited + 30))
  done
else
  echo "[gate] no nvidia-smi — refusing to serve on CPU (this measures GPU serving)"; exit 2
fi

# --- serve ----------------------------------------------------------------------------
# Tool calling is not optional for this repo: the episode loop *is* an agent with a tool, and
# vLLM refuses `tool_choice: auto` with a 400 unless both of these are set. Found by running
# the episode loop against a local vLLM for the first time — the probe never needed it, so a
# rented box would have failed the same way at full price.
#
# The parser is per model family (hermes covers Qwen2.5/3 function calling). gpt-oss needs
# NONE: its harmony output is parsed by HarmonyParser and passing a parser is wrong there, so
# TOOL_PARSER="" skips both flags for it.
TOOL_ARGS=()
if [ -n "${TOOL_PARSER:-}" ]; then
  TOOL_ARGS+=(--enable-auto-tool-choice --tool-call-parser "$TOOL_PARSER")
fi

CACHE_FLAG="--enable-prefix-caching"
[ "$PREFIX_CACHING" = "off" ] && CACHE_FLAG="--no-enable-prefix-caching"

# Observability flags, added only when asked for.
#
# OTLP traces: vLLM emits one span per request carrying gen_ai.latency.time_in_queue,
# .time_in_model_prefill, .time_to_first_token and the token counts — and it *extracts*
# incoming traceparent/tracestate (vllm/tracing/otel.py:127), so a request made inside an
# episode span becomes a child of it. That is what makes one trace span agent -> graph ->
# model instead of three disconnected stories.
#
# KV residency: vllm:kv_block_lifetime_seconds / _idle_before_evict_seconds /
# _reuse_gap_seconds are the instrument for "is the ontology block a hot tier or does it get
# evicted between episodes" — a question a hit *rate* cannot answer. They are off by default
# and sampled at 1% (config/observability.py:48-53), which at our request volume would leave
# the histograms empty, so the sample rate is raised deliberately here.
# Position Interpolation, measurable rather than cited: PI (Chen et al. 2023) linearly
# down-scales RoPE position indices to extend a pretrained context window, and that is
# exactly what vLLM's `--rope-scaling '{"rope_type":"linear","factor":N}'` does. Setting it
# is how the long-prefix end of the sweep becomes reachable on a model whose native window is
# shorter — and the serving cost it exposes is the point: the KV footprint of a shared prefix
# grows linearly with its length, so the binding constraint stops being "can the model read
# this" and becomes "does the server still have it cached".
OBS_ARGS=()
# PI_FACTOR is the paper's knob spelled the way vLLM takes it: linear RoPE down-scaling.
# ROPE_SCALING stays available for anything more exotic (yarn, dynamic), but PI is linear by
# definition, so this is the form the experiment uses.
if [ -n "${PI_FACTOR:-}" ]; then
  OBS_ARGS+=(--rope-scaling "{\"rope_type\":\"linear\",\"factor\":${PI_FACTOR}}")
fi
[ -n "${ROPE_SCALING:-}" ] && OBS_ARGS+=(--rope-scaling "$ROPE_SCALING")
[ -n "${KV_TRANSFER_CONFIG:-}" ] && OBS_ARGS+=(--kv-transfer-config "$KV_TRANSFER_CONFIG")
if [ -n "${OTLP_ENDPOINT:-}" ]; then
  # Without this the serving spans land in Tempo as `unknown_service`, which makes the one
  # thing the joined trace is for — telling the agent side from the serving side — a guess.
  export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-vllm}"
  OBS_ARGS+=(--otlp-traces-endpoint "$OTLP_ENDPOINT")
  [ -n "${DETAILED_TRACES:-}" ] && OBS_ARGS+=(--collect-detailed-traces "$DETAILED_TRACES")
fi
if [ "${KV_CACHE_METRICS:-on}" != "off" ]; then
  OBS_ARGS+=(--kv-cache-metrics --kv-cache-metrics-sample "${KV_CACHE_METRICS_SAMPLE:-1.0}")
fi

# --enable-prompt-tokens-details is off by default (vllm 0.27.1 cli_args.py:132), and
# without it every response carries `prompt_tokens_details: null`. The server's own
# counters still move, but they are process-wide: they cannot say which episode's prompt
# hit the cache. Per-request attribution is the whole point of measuring arms separately.
set -- serve "$MODEL" --host "$HOST" --port "$PORT" \
  --served-model-name "$SERVED_NAME" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --enable-prompt-tokens-details \
  "$CACHE_FLAG" "${TOOL_ARGS[@]}" "${OBS_ARGS[@]}"
[ -n "${VLLM_EXTRA_ARGS:-}" ] && set -- "$@" ${VLLM_EXTRA_ARGS}

echo "[serve] $VLLM_BIN $*"
"$VLLM_BIN" "$@" > "$LOG" 2>&1 &
SERVER_PID=$!

# The flags ARE the experiment's independent variables, so they are written where the run
# manifest can pick them up even after the server is gone.
python3 - "$FLAGS_OUT" "$SERVER_PID" "$PREFIX_CACHING" "$MODEL" "$SERVED_NAME" "$PORT" \
         "$GPU_UTIL" "$MAX_LEN" "$*" <<'PYEOF'
import json, sys
out, pid, caching, model, served, port, util, maxlen, argv = sys.argv[1:10]
import os
pi = os.environ.get("PI_FACTOR") or None
json.dump({"pid": int(pid), "prefix_caching": caching, "model": model,
           "served_model_name": served, "port": int(port),
           "gpu_memory_utilization": float(util), "max_model_len": int(maxlen),
           # Position Interpolation, recorded next to the window it produced. Without this
           # pairing a results file cannot say whether 500k tokens of context were native.
           "position_interpolation_factor": float(pi) if pi else None,
           "rope_scaling": ({"rope_type": "linear", "factor": float(pi)} if pi else None),
           "argv": argv}, open(out, "w"), indent=2)
PYEOF

trap 'kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; echo "[serve] stopped"' EXIT INT TERM
wait_ready || exit $?

if [ "${MODE}" = "serve-detach" ]; then
  trap - EXIT INT TERM
  echo "[serve] pid $SERVER_PID detached; log $LOG"
  exit 0
fi
wait "$SERVER_PID"
