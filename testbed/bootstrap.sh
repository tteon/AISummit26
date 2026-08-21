#!/usr/bin/env bash
# Bring up the whole testbed on a rented GPU box and run the experiment, in order:
#
#   datasets (S3 or local) -> DozerDB -> bulk load -> vLLM -> episodes -> probe -> S3
#
# Sequencing is not cosmetic. Everything before "vLLM" is CPU work, and every minute of it
# is billed at GPU rates, so the dataset is pulled from S3 rather than regenerated and the
# graph is loaded while the model is still downloading. Everything after "episodes" exists
# because the instance is not storage: the resume log is synced *during* the run, and the
# destroy gate refuses to declare success until S3 holds every local artifact.
#
#   NEO4J_PASSWORD=... VLLM_MODEL=openai/gpt-oss-120b S3_BUCKET=... testbed/bootstrap.sh
#   STEPS=db,load testbed/bootstrap.sh            # just stand the graph up
#   DRY_RUN=1 testbed/bootstrap.sh                # print the plan, touch nothing
#   PREFIX_CACHING=off RUN_ID=..._off testbed/bootstrap.sh   # the control arm
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_${PREFIX_CACHING:-on}}"
STEPS="${STEPS:-datasets,db,load,serve,episodes,probe,push,verify}"
DRY_RUN="${DRY_RUN:-0}"

NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
NEO4J_URI="${NEO4J_URI:-bolt://127.0.0.1:7687}"
SCALES="${SCALES:-1 10}"
DB_PREFIX="${DB_PREFIX:-finbenchl}"
ARMS="${ARMS:-labels ontology guardrail plan in_context in_context_blind in_context_csv}"
REPEATS="${REPEATS:-3}"
CONCURRENCY="${CONCURRENCY:-4}"
VLLM_MODEL="${VLLM_MODEL:-openai/gpt-oss-120b}"
VLLM_PORT="${VLLM_PORT:-8000}"
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:${VLLM_PORT}/v1}"
export MODEL_PROVIDER=vllm
export VLLM_MODEL
export DB_EXEC_MODE="${DB_EXEC_MODE:-local}"
export OPENAI_AGENTS_DISABLE_TRACING="${OPENAI_AGENTS_DISABLE_TRACING:-1}"

RUN_DIR="results/runs/${RUN_ID}"
EPISODES_JSON="${RUN_DIR}/episodes.json"
EPISODES_LOG="${RUN_DIR}/episodes.jsonl"
PROBE_JSON="${RUN_DIR}/vllm_prefix_probe.json"
WATCH_PID=""
SAMPLER_PID=""

say() { echo -e "\n=== [$(date -u +%H:%M:%S)] $* ===" ; }
have() { [[ ",${STEPS}," == *",$1,"* ]]; }
run() { echo "+ $*"; [ "$DRY_RUN" = "1" ] || "$@"; }
s3_on() { [ -n "${S3_BUCKET:-}" ]; }

cleanup() {
  [ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null
  [ -n "$SAMPLER_PID" ] && kill "$SAMPLER_PID" 2>/dev/null
  return 0
}
trap cleanup EXIT

mkdir -p "$RUN_DIR" outputs/testbed
echo "run_id       : $RUN_ID"
echo "steps        : $STEPS"
echo "model        : $VLLM_MODEL @ $VLLM_BASE_URL (prefix caching ${PREFIX_CACHING:-on})"
echo "scales       : $SCALES -> ${DB_PREFIX}<sf>"
echo "s3           : ${S3_BUCKET:-<disabled>} / ${S3_PREFIX:-aisummit26}"
echo "otlp         : ${OTLP_ENDPOINT:-<disabled>} (kv metrics ${KV_CACHE_METRICS:-on} @ ${KV_CACHE_METRICS_SAMPLE:-1.0})"
echo "run dir      : $RUN_DIR"

# --- 1. datasets ----------------------------------------------------------------------
if have datasets; then
  say "datasets"
  for sf in $SCALES; do
    if [ -f "outputs/finbench/sf${sf}/manifest.json" ]; then
      echo "sf${sf}: present locally"
    elif s3_on; then
      run python3 scripts/testbed/s3_ckpt.py pull-datasets --sf "$sf" --dest outputs/finbench
    elif [ "${GENERATE_MISSING:-0}" = "1" ]; then
      # `outputs/` is gitignored, so a fresh clone has no snapshots. The generator is
      # seeded (manifest records seed 0.42), so regenerating reproduces the same graph
      # rather than a similar one — but it is CPU work billed at GPU rates, and SF100 is
      # the slow one. Prefer S3 for anything above SF10.
      echo "sf${sf}: generating (GENERATE_MISSING=1)"
      run python3 scripts/data/gen_duckdb.py --sf "$sf" --out outputs/finbench
    else
      echo "sf${sf}: MISSING. Either seed it from S3, or regenerate on this box:"
      echo "    GENERATE_MISSING=1 testbed/bootstrap.sh          # seeded, same graph"
      echo "    # or, off the GPU box, once:"
      echo "    python3 scripts/data/gen_duckdb.py --sf ${sf} --out outputs/finbench"
      echo "    python3 scripts/testbed/s3_ckpt.py push-datasets --src outputs/finbench/sf${sf}"
      exit 1
    fi
  done
fi

# --- 2. database ----------------------------------------------------------------------
if have db; then
  say "dozerdb"
  [ -z "$NEO4J_PASSWORD" ] && { echo "NEO4J_PASSWORD is required"; exit 1; }
  if [ "$DB_EXEC_MODE" = "local" ]; then
    if [ ! -d /data/databases/neo4j ]; then
      run bash -c "chown -R neo4j:neo4j /data /logs 2>/dev/null; \
                   su neo4j -s /bin/bash -c 'neo4j-admin dbms set-initial-password \"$NEO4J_PASSWORD\"'"
    fi
    run bash -c "su neo4j -s /bin/bash -c 'neo4j start'"
    if [ "$DRY_RUN" != "1" ]; then
      for i in $(seq 1 60); do
        if python3 - <<PY
import sys
from neo4j import GraphDatabase
try:
    d = GraphDatabase.driver("$NEO4J_URI", auth=("neo4j", "$NEO4J_PASSWORD"))
    d.verify_connectivity(); d.close()
except Exception:
    sys.exit(1)
PY
        then echo "[db] bolt up after $((i*3))s"; break; fi
        [ "$i" = 60 ] && { echo "[db] TIMEOUT; last log lines:"; tail -30 /logs/neo4j.log 2>/dev/null; exit 1; }
        sleep 3
      done
    fi
  else
    echo "[db] exec mode $DB_EXEC_MODE — expecting an already-running container"
  fi
fi

# --- 3. bulk load ---------------------------------------------------------------------
if have load; then
  say "bulk load"
  for sf in $SCALES; do
    db="${DB_PREFIX}${sf}"
    run python3 scripts/data/bulk_load.py --src "outputs/finbench/sf${sf}" --database "$db" \
        --uri "$NEO4J_URI" --password "$NEO4J_PASSWORD" --exec-mode "$DB_EXEC_MODE" \
        --out "${RUN_DIR}/bulk_load_${db}.json"
  done
fi

# --- 4. serve -------------------------------------------------------------------------
if have serve; then
  say "vllm"
  export VLLM_LOG="${RUN_DIR}/vllm_serve.log"
  export VLLM_FLAGS_OUT="${RUN_DIR}/vllm_serve_flags.json"
  # OTLP_ENDPOINT / KV_CACHE_METRICS pass through to the serve script; when a collector is
  # running (testbed/observability), the model's spans join the episode's own trace.
  run env MODE=serve-detach VLLM_PORT="$VLLM_PORT" \
      OTLP_ENDPOINT="${OTLP_ENDPOINT:-}" DETAILED_TRACES="${DETAILED_TRACES:-}" \
      KV_CACHE_METRICS="${KV_CACHE_METRICS:-on}" \
      KV_CACHE_METRICS_SAMPLE="${KV_CACHE_METRICS_SAMPLE:-1.0}" \
      bash testbed/serve_vllm.sh
fi

# --- 5. episodes ----------------------------------------------------------------------
if have episodes; then
  say "episodes"
  # The serving metrics for the whole run, into the run directory. The Grafana stack is a
  # live view and needs docker-in-docker to run on a rented instance; this needs nothing,
  # and it is what makes "what was the cache doing during episode 412" answerable later.
  if [ "$DRY_RUN" != "1" ]; then
    python3 scripts/testbed/metrics_sampler.py --base-url "$VLLM_BASE_URL" \
        --out "${RUN_DIR}/metrics.jsonl" --interval "${METRICS_INTERVAL:-5}" &
    SAMPLER_PID=$!
  fi
  dbs=""
  for sf in $SCALES; do dbs="$dbs ${DB_PREFIX}${sf}:${sf}"; done
  if s3_on && [ "$DRY_RUN" != "1" ]; then
    # The resume log goes to S3 while episodes are still running, because that is the only
    # window in which losing the instance is recoverable.
    python3 scripts/testbed/s3_ckpt.py watch --run-id "$RUN_ID" --path "$EPISODES_LOG" \
        --interval "${WATCH_INTERVAL:-60}" &
    WATCH_PID=$!
  fi
  run python3 scripts/agents/agent_interaction.py \
      --provider vllm --model "$VLLM_MODEL" --base-url "$VLLM_BASE_URL" \
      --uri "$NEO4J_URI" --password "$NEO4J_PASSWORD" \
      --databases $dbs --arms $ARMS --repeats "$REPEATS" --concurrency "$CONCURRENCY" \
      --episodes-log "$EPISODES_LOG" --resume --out "$EPISODES_JSON"
  [ -n "$WATCH_PID" ] && { kill "$WATCH_PID" 2>/dev/null; WATCH_PID=""; }
  s3_on && [ "$DRY_RUN" != "1" ] && python3 scripts/testbed/s3_ckpt.py watch --once \
      --run-id "$RUN_ID" --path "$EPISODES_LOG"
fi

# --- 6. prefix-cache probe ------------------------------------------------------------
if have probe; then
  say "prefix cache probe"
  run python3 scripts/testbed/vllm_probe.py --provider vllm --model "$VLLM_MODEL" \
      --base-url "$VLLM_BASE_URL" --repeats "${PROBE_REPEATS:-8}" --out "$PROBE_JSON"
fi

[ -n "$SAMPLER_PID" ] && { kill "$SAMPLER_PID" 2>/dev/null; SAMPLER_PID=""; }

# --- 7. checkpoint --------------------------------------------------------------------
if have push && s3_on; then
  say "push to s3"
  run python3 scripts/testbed/s3_ckpt.py push --run-id "$RUN_ID" --path "$RUN_DIR" \
      --receipt "${RUN_DIR}/s3_receipt.json"
fi

if have verify && s3_on; then
  say "destroy gate"
  run python3 scripts/testbed/s3_ckpt.py verify --run-id "$RUN_ID" --path "$RUN_DIR"
  echo "[gate] artifacts are in S3 — this instance can be destroyed"
elif have verify; then
  echo "[gate] S3 disabled: ${RUN_DIR} exists ONLY on this instance. Do not destroy it."
fi

say "done: $RUN_ID"
