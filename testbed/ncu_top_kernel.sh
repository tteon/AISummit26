#!/usr/bin/env bash
# Profile ONE kernel out of a real vLLM run with Nsight Compute.
#
# Needs GPU performance counters, which are root-only by default:
# `ncu` fails with ERR_NVGPUCTRPERM for a normal user. Either run this with sudo, or lift the
# restriction once and for all:
#
#     echo 'options nvidia NVreg_RestrictProfilingToAdminUsers=0' \
#       | sudo tee /etc/modprobe.d/nvidia-profiling.conf
#     sudo update-initramfs -u && sudo reboot
#
# Why one kernel and not `--set full` over everything: Nsight Compute serialises and *replays*
# each kernel to collect counters, so a whole-server profile takes minutes per launch and the
# latencies it reports are no longer the latencies the system has. The timeline (nsys) says
# which kernel deserves the question; this answers the question for that kernel only.
#
# The default target is the GEMM that owned 22% of GPU time in the 2026-08-22 local capture:
# ampere_bf16_s16816gemm_bf16_256x128_ldg8_f2f_stages_32x3_tn
#
#   sudo -E testbed/ncu_top_kernel.sh
#   KERNEL_REGEX=flash_fwd_splitkv sudo -E testbed/ncu_top_kernel.sh
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

MODEL="${MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
VLLM_BIN="${VLLM_BIN:-/home/hadry/vllm-env/bin/vllm}"
NCU="${NCU_BIN:-/usr/local/cuda/bin/ncu}"
PORT="${VLLM_PORT:-8000}"
BASE="http://127.0.0.1:${PORT}/v1"
KERNEL_REGEX="${KERNEL_REGEX:-ampere_bf16_s16816gemm_bf16_256x128}"
LAUNCH_COUNT="${LAUNCH_COUNT:-2}"
LAUNCH_SKIP="${LAUNCH_SKIP:-20}"     # skip warmup/graph-capture launches
MAX_LEN="${VLLM_MAX_MODEL_LEN:-4096}"
GPU_UTIL="${VLLM_GPU_UTIL:-0.75}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_ncu}"
OUT="results/runs/${RUN_ID}"
SECTIONS="${SECTIONS:---section SpeedOfLight --section MemoryWorkloadAnalysis --section Occupancy --section ComputeWorkloadAnalysis}"

mkdir -p "$OUT"
command -v "$NCU" >/dev/null || { echo "ncu not found at $NCU"; exit 2; }

echo "kernel : $KERNEL_REGEX  (skip $LAUNCH_SKIP, collect $LAUNCH_COUNT)"
echo "model  : $MODEL"
echo "out    : $OUT"

# A counter check first, so a permissions failure costs one second instead of a model load.
if ! "$NCU" --set basic --launch-count 1 /home/hadry/vllm-env/bin/python -c \
      "import torch; a=torch.randn(64,64,device='cuda'); (a@a).sum().item()" >/dev/null 2>&1; then
  echo "ERROR: no access to GPU performance counters (ERR_NVGPUCTRPERM)."
  echo "       Run this under sudo, or set NVreg_RestrictProfilingToAdminUsers=0 (see header)."
  exit 3
fi

"$NCU" --target-processes all \
  --kernel-name "regex:${KERNEL_REGEX}" \
  --launch-skip "$LAUNCH_SKIP" --launch-count "$LAUNCH_COUNT" \
  $SECTIONS \
  --export "$PWD/$OUT/ncu_report" --force-overwrite \
  "$VLLM_BIN" serve "$MODEL" --host 127.0.0.1 --port "$PORT" \
    --gpu-memory-utilization "$GPU_UTIL" --max-model-len "$MAX_LEN" \
    --enable-prefix-caching --enable-prompt-tokens-details \
  > "$OUT/ncu_serve.log" 2>&1 &
NCU_PID=$!

for i in $(seq 1 180); do
  curl -sf -m 3 "${BASE}/models" >/dev/null 2>&1 && { echo "[serve] ready after $((i*5))s"; break; }
  kill -0 "$NCU_PID" 2>/dev/null || { echo "[serve] died:"; tail -20 "$OUT/ncu_serve.log"; exit 4; }
  sleep 5
done

# A couple of prefill-heavy requests: the target GEMM runs in prefill, so a long prompt is
# what makes it appear. Kept small because every matching launch is replayed.
python3 scripts/testbed/vllm_probe.py --provider vllm --model "$MODEL" --base-url "$BASE" \
    --repeats 2 --sweep 2000 --no-stream --skip-salted \
    --out "$OUT/probe.json" >/dev/null 2>&1 || true

kill -INT "$NCU_PID" 2>/dev/null
for i in $(seq 1 90); do kill -0 "$NCU_PID" 2>/dev/null || break; sleep 2; done
kill -9 "$NCU_PID" 2>/dev/null

REP="$OUT/ncu_report.ncu-rep"
[ -f "$REP" ] || { echo "no report written; tail of log:"; tail -25 "$OUT/ncu_serve.log"; exit 5; }
"$NCU" --import "$REP" --page details --csv > "$OUT/ncu_details.csv" 2>/dev/null
"$NCU" --import "$REP" --page details 2>/dev/null | tee "$OUT/ncu_details.txt" \
  | grep -iE "Duration|Compute \(SM\)|Memory \[%\]|DRAM Throughput|Achieved Occupancy|L1/TEX|L2 |Bound" | head -20
echo
echo "wrote $REP and $OUT/ncu_details.{txt,csv}"
