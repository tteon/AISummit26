#!/usr/bin/env bash
# Rent a GPU, run the benchmark on it, bring the results home, then destroy it.
#
# The rental is part of the measurement. A p99 that cannot name the machine that produced it
# is not reproducible, and on vast.ai that machine is gone minutes after the run — so this
# script writes the offer and the instance record into the run directory before anything
# else happens, and the numbers arrive home with the price, GPU, host and network speed of
# the box that made them.
#
# The order is deliberate: run -> copy -> verify the copy -> only then destroy. Anything that
# fails leaves the instance alive and prints the destroy command, because an unattended
# teardown that fires before the copy succeeded is data loss, and the difference in cost is
# cents.
#
#   testbed/vast_run.sh --dry-run                       # print every command, touch nothing
#   testbed/vast_run.sh                                 # the treatment arm
#   PREFIX_CACHING=off testbed/vast_run.sh              # the control arm
#   INSTANCE_ID=12345678 testbed/vast_run.sh            # reuse a box you already rented
#
# Requires: `vastai` on PATH, an API key set (`vastai set api-key ...`), and an ssh key
# registered on the account BEFORE creation (`vastai create ssh-key ~/.ssh/id_ed25519.pub`) —
# keys are applied at container creation time, so a forgotten key means a new instance.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

IMAGE="${IMAGE:-}"                       # ghcr.io/<owner>/aisummit26-testbed:<sha>
DISK_GB="${DISK_GB:-250}"                # 63GB of MXFP4 weights + a ~12GB image + results
GPU_QUERY="${GPU_QUERY:-gpu_name=H200 num_gpus=1}"
MAX_DPH="${MAX_DPH:-4.0}"                # hard ceiling, $/hr — a typo in a query is expensive
SORT="${SORT:-dph_total}"                # cheapest first; use 'dlperf_usd-' for value-first
INSTANCE_ID="${INSTANCE_ID:-}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_$(echo "${GPU_QUERY}" | tr -cd 'A-Za-z0-9')_${PREFIX_CACHING:-on}}"
REMOTE_DIR="${REMOTE_DIR:-/workspace/AIsummit26}"
LOCAL_RUN_DIR="results/runs/${RUN_ID}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
DRY_RUN=0
DESTROY_ON_SUCCESS="${DESTROY_ON_SUCCESS:-1}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-900}"

# What the instance runs. Everything the experiment needs is env, so the remote command stays
# short — --onstart-cmd is capped at 16KB and a long heredoc there is a debugging trap.
REMOTE_ENV=(
  "GENERATE_MISSING=${GENERATE_MISSING:-1}"
  "SCALES=${SCALES:-1 10}"
  "ARMS=${ARMS:-labels ontology guardrail plan in_context in_context_blind in_context_csv}"
  "REPEATS=${REPEATS:-3}"
  "CONCURRENCY=${CONCURRENCY:-4}"
  "VLLM_MODEL=${VLLM_MODEL:-openai/gpt-oss-120b}"
  "PREFIX_CACHING=${PREFIX_CACHING:-on}"
  "KV_CACHE_METRICS=${KV_CACHE_METRICS:-on}"
  "KV_CACHE_METRICS_SAMPLE=${KV_CACHE_METRICS_SAMPLE:-1.0}"
  "RUN_ID=${RUN_ID}"
)
[ -n "${S3_BUCKET:-}" ] && REMOTE_ENV+=("S3_BUCKET=${S3_BUCKET}" "S3_PREFIX=${S3_PREFIX:-aisummit26}"
                                        "AWS_REGION=${AWS_REGION:-}" )

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --keep) DESTROY_ON_SUCCESS=0 ;;
    *) echo "unknown argument: $arg"; exit 64 ;;
  esac
done

PULL_PID=""
PULL_INTERVAL_S="${PULL_INTERVAL_S:-300}"

cleanup() { [ -n "$PULL_PID" ] && kill "$PULL_PID" 2>/dev/null; return 0; }
trap cleanup EXIT INT TERM

say() { echo -e "\n=== [$(date -u +%H:%M:%S)] $* ==="; }
run() { echo "+ $*"; [ "$DRY_RUN" = "1" ] || "$@"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "$1 not found on PATH"; exit 69; }; }

# --dry-run prints the plan and must work before anything is installed: reviewing what a
# script will spend money on should not itself require the tool that spends it.
[ "$DRY_RUN" = "1" ] || need vastai
mkdir -p "$LOCAL_RUN_DIR"

echo "run id   : $RUN_ID"
echo "image    : ${IMAGE:-<unset — required unless INSTANCE_ID is given>}"
echo "query    : $GPU_QUERY (disk ${DISK_GB}GB, ceiling \$${MAX_DPH}/hr, sort $SORT)"
echo "local dir: $LOCAL_RUN_DIR"
echo "destroy  : $([ "$DESTROY_ON_SUCCESS" = 1 ] && echo "after a verified copy" || echo "never (--keep)")"

# --- 1. offer ------------------------------------------------------------------------------
if [ -z "$INSTANCE_ID" ]; then
  say "search"
  [ -z "$IMAGE" ] && { echo "IMAGE is required to create an instance"; exit 64; }
  QUERY="$GPU_QUERY verified=true rentable=true direct_port_count>=1 disk_space>=${DISK_GB} dph_total<=${MAX_DPH}"
  echo "+ vastai search offers '$QUERY' -o '$SORT' --raw"
  if [ "$DRY_RUN" = "1" ]; then
    OFFER_ID="<offer>"
  else
    OFFERS=$(vastai search offers "$QUERY" -o "$SORT" --raw)
    echo "$OFFERS" > "${LOCAL_RUN_DIR}/vast_offers.json"
    # The ceiling is enforced on the chosen offer, not merely asked for in the query: a
    # query field that turns out not to exist is silently ignored by the API, and the first
    # result of an unfiltered search can be an $8/hr box.
    OFFER_ID=$(python3 -c "
import json, sys
offers = json.load(open('${LOCAL_RUN_DIR}/vast_offers.json'))
offers = offers if isinstance(offers, list) else offers.get('offers', [])
ok = [o for o in offers if float(o.get('dph_total', 1e9)) <= ${MAX_DPH}]
if not ok:
    sys.exit('no offer under the price ceiling — relax GPU_QUERY or raise MAX_DPH')
o = ok[0]
print(o['id'])
sys.stderr.write(f\"chose offer {o['id']}: {o.get('gpu_name')} x{o.get('num_gpus')} \"
                 f\"\\\${o.get('dph_total'):.3f}/hr disk={o.get('disk_space')}GB \"
                 f\"down={o.get('inet_down')}Mbps {o.get('geolocation')}\\n\")
") || exit 1
  fi

  say "create"
  run vastai create instance "$OFFER_ID" --image "$IMAGE" --disk "$DISK_GB" --ssh --direct --raw
  if [ "$DRY_RUN" != "1" ]; then
    echo "Paste the new_contract id from the output above:"
    read -r INSTANCE_ID
    [ -z "$INSTANCE_ID" ] && { echo "no instance id given"; exit 65; }
  else
    INSTANCE_ID="<instance>"
  fi
fi
echo "$INSTANCE_ID" > "${LOCAL_RUN_DIR}/vast_instance_id"

# --- 2. wait for running -------------------------------------------------------------------
say "wait for running"
if [ "$DRY_RUN" != "1" ]; then
  waited=0
  while :; do
    vastai show instance "$INSTANCE_ID" --raw > "${LOCAL_RUN_DIR}/vast_instance.json" 2>/dev/null
    STATUS=$(python3 -c "
import json
d = json.load(open('${LOCAL_RUN_DIR}/vast_instance.json'))
d = d[0] if isinstance(d, list) and d else d
print(d.get('actual_status') or d.get('cur_state') or 'unknown')" 2>/dev/null || echo unknown)
    case "$STATUS" in
      running) echo "[vast] running after ${waited}s"; break ;;
      # The docs are explicit that these never reach running. Polling through them burns
      # disk charges forever while the script looks busy.
      exited|offline|unknown_error) echo "[vast] terminal status '$STATUS' — destroying and stopping"
                                    vastai destroy instance "$INSTANCE_ID"; exit 70 ;;
    esac
    if [ "$waited" -ge "$POLL_TIMEOUT_S" ]; then
      echo "[vast] TIMEOUT at status '$STATUS' after ${waited}s. Instance left alive:"
      echo "       vastai destroy instance $INSTANCE_ID"; exit 71
    fi
    sleep 15; waited=$((waited + 15))
  done
  # The machine that produced the numbers, recorded while it still exists.
  python3 -c "
import json
d = json.load(open('${LOCAL_RUN_DIR}/vast_instance.json'))
d = d[0] if isinstance(d, list) and d else d
keep = ('id','machine_id','gpu_name','num_gpus','gpu_ram','cpu_name','cpu_cores_effective',
        'disk_space','dph_total','inet_down','inet_up','geolocation','cuda_max_good',
        'driver_version','image_uuid','ssh_host','ssh_port')
json.dump({k: d.get(k) for k in keep}, open('${LOCAL_RUN_DIR}/vast_machine.json','w'), indent=2)
print('[vast]', d.get('gpu_name'), 'x', d.get('num_gpus'), '\$'+str(d.get('dph_total')), '/hr')"
fi

# --- 2b. seed datasets from local, if we have them -----------------------------------------
# The alternative to regenerating SF100 on a metered GPU is 36MB of parquet over the wire.
# S3 is the better answer across many rentals; for one, this is the whole of it.
if [ -n "${SEED_SNAPSHOTS:-}" ]; then
  say "seed snapshots"
  for sf in $SEED_SNAPSHOTS; do
    [ -d "outputs/finbench/sf${sf}" ] || { echo "outputs/finbench/sf${sf} not present locally"; exit 66; }
    run vastai copy "local:./outputs/finbench/sf${sf}/" "${INSTANCE_ID}:${REMOTE_DIR}/outputs/finbench/sf${sf}/"
  done
fi

# --- 3. run --------------------------------------------------------------------------------
say "bootstrap on the instance"
SSH_URL=$([ "$DRY_RUN" = "1" ] && echo "ssh://root@host:22" || vastai ssh-url "$INSTANCE_ID")
SSH_HOST=$(echo "$SSH_URL" | sed -E 's#ssh://[^@]+@([^:]+):.*#\1#')
SSH_PORT=$(echo "$SSH_URL" | sed -E 's#.*:([0-9]+)$#\1#')
REMOTE_CMD="cd ${REMOTE_DIR} && ${REMOTE_ENV[*]} NEO4J_PASSWORD=\$(openssl rand -hex 12) bash testbed/bootstrap.sh"
echo "+ ssh -p $SSH_PORT root@$SSH_HOST '<bootstrap>'"
# Pull the run directory every few minutes *while it runs*. `vastai copy` at the end is
# enough for a run that finishes; it is nothing for a run whose host disconnects at hour two,
# and it is the resume log that matters most — with it, the next rental skips what this one
# already paid for. This is what S3's watch loop was for; without a bucket, a periodic pull
# buys the same property for the price of some bandwidth.
if [ "$DRY_RUN" != "1" ]; then
  ( while :; do
      sleep "$PULL_INTERVAL_S"
      vastai copy "${INSTANCE_ID}:${REMOTE_DIR}/results/runs/${RUN_ID}/" "local:./${LOCAL_RUN_DIR}/" \
        >/dev/null 2>&1 && echo "[pull] synced $(date -u +%H:%M:%S)"
    done ) &
  PULL_PID=$!
fi
if [ "$DRY_RUN" != "1" ]; then
  # Under `script` so the remote log survives a dropped connection, and the whole thing in
  # one ssh invocation so there is no half-configured shell to reason about.
  ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i "$SSH_KEY" \
      -p "$SSH_PORT" "root@$SSH_HOST" "$REMOTE_CMD" 2>&1 | tee "${LOCAL_RUN_DIR}/bootstrap.log"
  RC=${PIPESTATUS[0]}
  echo "[vast] bootstrap exit=$RC"
else
  RC=0
fi

[ -n "$PULL_PID" ] && { kill "$PULL_PID" 2>/dev/null; PULL_PID=""; }

# --- 4. bring it home ----------------------------------------------------------------------
say "copy results home"
run vastai copy "${INSTANCE_ID}:${REMOTE_DIR}/results/runs/${RUN_ID}/" "local:./${LOCAL_RUN_DIR}/"

COPY_OK=0
if [ "$DRY_RUN" = "1" ]; then
  COPY_OK=1
else
  # "The copy ran" is not "the results are here". Check for the artifacts that make a run
  # analysable at all; anything missing means the instance must stay alive.
  MISSING=""
  for f in episodes.jsonl metrics.jsonl vllm_serve_flags.json; do
    [ -s "${LOCAL_RUN_DIR}/${f}" ] || MISSING="$MISSING $f"
  done
  if [ -z "$MISSING" ] && [ "$RC" = "0" ]; then
    COPY_OK=1
    echo "[vast] results verified locally:"
    du -sh "$LOCAL_RUN_DIR"; ls "$LOCAL_RUN_DIR"
  else
    echo "[vast] INCOMPLETE — missing:${MISSING:- (none)} bootstrap rc=$RC"
  fi
fi

# --- 5. teardown ---------------------------------------------------------------------------
if [ "$COPY_OK" = "1" ] && [ "$DESTROY_ON_SUCCESS" = "1" ]; then
  say "destroy"
  run vastai destroy instance "$INSTANCE_ID"
  echo "[vast] destroyed $INSTANCE_ID — results are in $LOCAL_RUN_DIR"
else
  say "instance left alive on purpose"
  echo "  ssh -p $SSH_PORT root@$SSH_HOST"
  echo "  vastai copy ${INSTANCE_ID}:${REMOTE_DIR}/results/runs/${RUN_ID}/ local:./${LOCAL_RUN_DIR}/"
  echo "  vastai stop instance $INSTANCE_ID     # pauses compute, disk still billed"
  echo "  vastai destroy instance $INSTANCE_ID  # stops all billing, deletes everything"
  [ "$COPY_OK" = "1" ] || exit 72
fi
