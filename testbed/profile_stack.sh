#!/usr/bin/env bash
# One correlated capture across four layers: vLLM, PyTorch, CUDA kernels, and the device.
#
# The order these get read in matters more than the tools. The CPU-centric measurement
# (bench_cpu_gpu_split.py, after Raj et al. arXiv 2511.00739) already says the tool path is
# CPU-bound — client-side share climbs 68.6% -> 99.1% as concurrency grows, and throughput
# falls past two concurrent requests. So the first kernel-level question is not "is attention
# slow" but "is the GPU starving because the host cannot feed it", which is a timeline
# question (launch gaps, sync stalls, H2D/D2H), not a counter question. Nsight Systems answers
# it; Nsight Compute is for afterwards, on whichever single kernel the timeline indicts.
#
# Layers and what each one can say:
#   vLLM      /metrics + OTLP spans   queue vs prefill vs decode, prefix-cache hits, KV usage
#   PyTorch   torch profiler trace    which ops, CPU-side vs CUDA-side, per-op time
#   kernels   nsys timeline (+NVTX)   launch gaps, idle GPU, memcpy, stream serialisation
#   device    nvidia-smi dmon         SM/mem utilisation, clocks, power, throttle reasons
#
# NVTX matters: with VLLM_NVTX_SCOPES_FOR_PROFILING=1 the nsys timeline carries vLLM's own
# phase names, so a gap can be attributed to a phase instead of guessed at.
#
#   MODEL=Qwen/Qwen2.5-1.5B-Instruct testbed/profile_stack.sh
#   NSYS=0 MODEL=openai/gpt-oss-120b testbed/profile_stack.sh     # skip the timeline
#   DRY_RUN=1 testbed/profile_stack.sh                            # print the plan
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

MODEL="${MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_stackprofile}"
OUT="results/runs/${RUN_ID}/profile"
PORT="${VLLM_PORT:-8000}"
BASE="http://127.0.0.1:${PORT}/v1"
VLLM_BIN="${VLLM_BIN:-vllm}"
MAX_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
GPU_UTIL="${VLLM_GPU_UTIL:-0.80}"
NSYS="${NSYS:-1}"
NCU="${NCU:-0}"                       # off by default: needs profiling permission, and it
                                      # serialises kernels, so latency numbers become invalid
DETAILED_TRACES="${DETAILED_TRACES:-}"   # model|worker|all — costly, diagnostic runs only
WORKLOAD="${WORKLOAD:-probe}"         # probe | none
PROBE_SWEEP="${PROBE_SWEEP:-2000,8000}"
PROBE_REPEATS="${PROBE_REPEATS:-4}"
DRY_RUN="${DRY_RUN:-0}"

SERVER_PID=""; DMON_PID=""
run() { echo "+ $*"; [ "$DRY_RUN" = "1" ] || "$@"; }
say() { echo -e "\n=== [$(date -u +%H:%M:%S)] $* ==="; }
cleanup() {
  [ -n "$DMON_PID" ] && kill "$DMON_PID" 2>/dev/null
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

mkdir -p "$OUT"
echo "run id : $RUN_ID"
echo "model  : $MODEL"
echo "layers : vllm=/metrics+otlp torch=start_profile nsys=$NSYS ncu=$NCU device=dmon"
echo "out    : $OUT"

command -v nvidia-smi >/dev/null || { echo "no nvidia-smi — this needs a GPU"; exit 2; }
[ "$NSYS" = "1" ] && ! command -v nsys >/dev/null && { echo "nsys not found; set NSYS=0"; exit 2; }

# --- device inventory, before anything runs ------------------------------------------------
if [ "$DRY_RUN" != "1" ]; then
  nvidia-smi --query-gpu=index,name,driver_version,memory.total,clocks.max.sm,power.limit \
    --format=csv > "$OUT/device.csv"
fi

# --- 1. serve, with every profiling seam opened --------------------------------------------
say "start vLLM (NVTX scopes on, torch profiler armed)"
SERVE_ARGS=(serve "$MODEL" --host 127.0.0.1 --port "$PORT"
  --gpu-memory-utilization "$GPU_UTIL" --max-model-len "$MAX_LEN"
  --enable-prefix-caching --enable-prompt-tokens-details
  --kv-cache-metrics --kv-cache-metrics-sample 1.0
  --profiler-config.profiler=torch
  --profiler-config.torch_profiler_dir="$PWD/$OUT/torch")
[ -n "${OTLP_ENDPOINT:-}" ] && SERVE_ARGS+=(--otlp-traces-endpoint "$OTLP_ENDPOINT")
[ -n "$DETAILED_TRACES" ] && SERVE_ARGS+=(--collect-detailed-traces "$DETAILED_TRACES")

# NVTX ranges are what make the timeline readable per vLLM phase rather than per raw kernel.
export VLLM_NVTX_SCOPES_FOR_PROFILING=1 VLLM_CUSTOM_SCOPES_FOR_PROFILING=1
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-vllm}"

LAUNCH=("$VLLM_BIN" "${SERVE_ARGS[@]}")
if [ "$NSYS" = "1" ]; then
  # --capture-range=cudaProfilerApi would tie the capture to the torch profiler window; the
  # simpler form captures the whole run, which is what we want for launch-gap analysis.
  LAUNCH=(nsys profile --output "$PWD/$OUT/timeline" --force-overwrite true
          --trace cuda,nvtx,osrt,cudnn,cublas --sample cpu --cudabacktrace none
          --python-sampling true "${LAUNCH[@]}")
fi
echo "+ ${LAUNCH[*]}"
if [ "$DRY_RUN" != "1" ]; then
  "${LAUNCH[@]}" > "$OUT/vllm_serve.log" 2>&1 &
  SERVER_PID=$!
  for i in $(seq 1 300); do
    curl -sf -m 3 "${BASE}/models" >/dev/null 2>&1 && { echo "[serve] ready after $((i*5))s"; break; }
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "[serve] died:"; tail -20 "$OUT/vllm_serve.log"; exit 3; }
    [ "$i" = 300 ] && { echo "[serve] timeout"; exit 4; }
    sleep 5
  done
fi

# --- 2. device sampler + a metrics snapshot before ------------------------------------------
say "device sampler"
if [ "$DRY_RUN" != "1" ]; then
  nvidia-smi dmon -s pucvmet -o DT > "$OUT/dmon.txt" 2>&1 &
  DMON_PID=$!
  curl -s "http://127.0.0.1:${PORT}/metrics" > "$OUT/metrics_before.txt"
fi

# --- 3. torch profiler window around a fixed workload ---------------------------------------
say "torch profiler window + workload"
run curl -s -X POST "http://127.0.0.1:${PORT}/start_profile"
if [ "$WORKLOAD" = "probe" ]; then
  run python3 scripts/testbed/vllm_probe.py --provider vllm --model "$MODEL" \
      --base-url "$BASE" --repeats "$PROBE_REPEATS" --sweep "$PROBE_SWEEP" \
      --out "$OUT/probe.json"
fi
run curl -s -X POST "http://127.0.0.1:${PORT}/stop_profile"

# --- 4. close the layers ------------------------------------------------------------------
say "collect"
if [ "$DRY_RUN" != "1" ]; then
  curl -s "http://127.0.0.1:${PORT}/metrics" > "$OUT/metrics_after.txt"
  kill "$DMON_PID" 2>/dev/null; DMON_PID=""
  # nsys needs the process to exit to finalise the report.
  kill -INT "$SERVER_PID" 2>/dev/null
  for i in $(seq 1 60); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 2; done
  kill -9 "$SERVER_PID" 2>/dev/null; SERVER_PID=""
  [ "$NSYS" = "1" ] && [ -f "$OUT/timeline.nsys-rep" ] && \
    nsys stats --report cuda_gpu_kern_sum,cuda_gpu_trace,nvtx_sum \
      --format csv --output "$OUT/timeline" "$OUT/timeline.nsys-rep" >/dev/null 2>&1
  python3 - "$OUT" "$MODEL" "$RUN_ID" <<'PYEOF'
import json, subprocess, sys, pathlib
out, model, run_id = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, str(out.parents[2].parent))
meta = {
    "schema_version": "seocho.finbench.stack-profile.v1",
    "run_id": run_id, "model": model,
    "layers": {
        "vllm": ["metrics_before.txt", "metrics_after.txt", "vllm_serve.log"],
        "torch": sorted(p.name for p in (out / "torch").glob("*")) if (out / "torch").exists() else [],
        "kernels": sorted(p.name for p in out.glob("timeline*")),
        "device": ["device.csv", "dmon.txt"],
        "workload": ["probe.json"] if (out / "probe.json").exists() else [],
    },
    "read_order": [
        "1. metrics_after minus metrics_before: where did the time go at the serving layer",
        "2. probe.json: TTFT and cached_tokens per request",
        "3. timeline_nvtx_sum.csv: which vLLM phase owns the wall clock",
        "4. timeline_cuda_gpu_kern_sum.csv: which kernels, and how much GPU idle between them",
        "5. torch/*.pt.trace.json: op-level CPU vs CUDA split inside the indicted phase",
    ],
}
(out / "stack_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
print(json.dumps(meta["layers"], indent=1))
PYEOF
fi

say "done: $OUT"
