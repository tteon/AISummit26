# The rented-GPU testbed: running these arms on a self-hosted vLLM

Every number in `results/` was produced against a hosted, OpenAI-compatible API (MARA). That
endpoint answered every question this repo asks except one, and it is the question the
ontology work leads to: **if the ontology block is the same bytes in every prompt, does the
server stop paying for it?**

It cannot answer that, and not for want of trying. On 2026-08-08 the endpoint was probed at
both levels: `prompt_tokens_details.cached_tokens` is absent on every model it serves, and
TTFT is flat across repeated byte-identical prefixes (0.205 / 0.207 / 0.208 s). There is no
prefix cache to hit and no implicit one either. Prefill is simply fast (~21k tok/s), so
resending the ontology costs money, not latency.

A self-hosted vLLM has a prefix cache, reports hits per request, and exports its own
counters. This testbed is what puts these arms in front of one.

## Topology: one image, three processes

```
  rented instance (vast.ai) ── one container ──────────────────────────────┐
  │  DozerDB 5.26.3.0 (local process)   <── bolt ──┐                       │
  │  vLLM OpenAI server :8000           <── http ──┤  agent_interaction.py │
  │  the harness (this repo)                       ┘  vllm_probe.py        │
  └──────────────────── S3: datasets in, runs + resume log out ────────────┘
```

One container, not three, because a vast.ai instance *is* a container: a sibling-container
topology needs docker-in-docker on the rented host, which narrows the machines you can rent
and buys nothing. Both round trips the experiment measures — agent→graph and agent→model —
stay local either way.

The one thing this required from the harness is `bulk_load.py --exec-mode local`: the loader
used to reach the database with `docker exec`, which does not exist when the database is a
process in the same container.

## What goes where

| Object | Location | Lifetime |
| --- | --- | --- |
| FinBench snapshots (parquet, 40 MB) | `s3://<bucket>/<prefix>/datasets/finbench/sf<N>/<manifest_sha>/` | immutable, reused across rentals |
| Run artifacts (manifests, metrics, figures) | `s3://.../runs/<run_id>/` | pushed per step |
| Resume log (`episodes.jsonl`) | `s3://.../runs/<run_id>/episodes.jsonl` | synced **while the run is going** |
| Model weights | HF cache on the instance (optionally mirrored to S3) | per rental |

The dataset key is the sha256 of the snapshot's own `manifest.json`, which already contains
the generator seed and a sha256 per parquet file. The key changes if and only if the data
changes, so two runs naming the same key provably read the same graph.

`episodes.jsonl` is the part that matters most. A rented instance can be reclaimed mid-run,
and a run that only persists on completion loses every episode it already paid for.
`agent_interaction.py --resume` reads the log, skips the `(database, arm, question, repeat)`
cells it already contains, and appends as new ones settle.

## Sizing the rental

- **1×H200 141 GB** is the recommendation for model parity: `gpt-oss-120b` in MXFP4 is
  ~63 GB of weights, so 141 GB leaves real KV-cache headroom — and how many ontology
  prefixes stay resident *is* the independent variable. 1×H100 80 GB serves the same model
  but the cache budget is thin enough to become a confound.
- Disk: ≥ 200 GB, or weight download dominates the rental.
- Region: close to the S3 bucket; the 40 MB pull is nothing, the results push is not.

## Running it

```bash
# once, off the GPU box — generating SF100 on a rented GPU is paying inference rates for
# a DuckDB job
python3 scripts/data/gen_duckdb.py --sf 100 --out outputs/finbench
python3 scripts/testbed/s3_ckpt.py push-datasets --src outputs/finbench/sf100

# build the image (or build it on the instance — bandwidth there is cheaper)
docker build -f testbed/Dockerfile -t aisummit26-testbed .

# on the instance: the treatment arm
NEO4J_PASSWORD=... S3_BUCKET=... AWS_REGION=... \
VLLM_MODEL=openai/gpt-oss-120b SCALES="1 10" \
  testbed/bootstrap.sh

# the control arm — vLLM V1 enables prefix caching by default, so the control must turn it
# off explicitly or it will quietly measure the treatment twice
PREFIX_CACHING=off RUN_ID=20260821_h200_off NEO4J_PASSWORD=... S3_BUCKET=... \
  testbed/bootstrap.sh
```

`STEPS=db,load` runs a subset; `DRY_RUN=1` prints the plan and touches nothing.

Before destroying the instance:

```bash
python3 scripts/testbed/s3_ckpt.py verify --run-id <run_id> --path results/runs/<run_id>
```

It exits non-zero unless every local file is in S3 at the same size. `bootstrap.sh` runs it
as its last step for the same reason.

## First benchmark on a rented box, step by step

The instance cannot build the image — a vast.ai instance *is* a container, started from an
image, with no docker of its own. So the image is built elsewhere and the instance is started
from it. Building it on GitHub is the cheap route: the vLLM base layers already live next to
Docker Hub, so nothing 12 GB has to leave your laptop.

**1. Build the image.** The workflow lives at `testbed/ci/testbed-image.yml` — copy it to
`.github/workflows/testbed-image.yml` in the fork first (paste it in the GitHub web UI, or
push with a token carrying the `workflow` scope; the credential this repo is normally pushed
with does not have it). Then Actions → `testbed image` → Run workflow. It is
`workflow_dispatch` only and prints the resulting tag:
`ghcr.io/<owner>/aisummit26-testbed:<sha>`. Make the package public, or give vast.ai a
registry credential. Local alternative, if you would rather not use CI:

```bash
docker build -f testbed/Dockerfile -t ghcr.io/<owner>/aisummit26-testbed:$(git rev-parse --short HEAD) .
docker push ghcr.io/<owner>/aisummit26-testbed:<sha>
```

**1b. What the image does and does not carry.** The 12 GB never passes through your machine:
GitHub builds from the Dockerfile in the repo and pushes to GHCR, and the *instance* pulls
from GHCR at creation using the datacenter's bandwidth. A registry is not optional — a vast
instance can only be started from an image, never build one — but which side builds is.

In the image: python 3.12, vLLM, nvcc and gcc (from the official vLLM base), DozerDB 5.26.3.0
copied out of the graphstack image, the harness, and seocho pinned to the measured tag. Not in
the image, on purpose:

| left out | why, and what happens instead |
| --- | --- |
| model weights (~63 GB for gpt-oss-120b) | would triple the image and put redistribution in scope; downloaded per rental (~4 min at 8.7 Gb/s here), or kept on a vast **volume** to survive across rentals |
| FinBench snapshots | `outputs/` is gitignored and `.dockerignore`d; `GENERATE_MISSING=1` reproduces the committed manifest's checksums exactly, or seed them with `SEED_SNAPSHOTS` |
| every secret | passed as env at run time, never baked |

`.dockerignore` exists for a specific reason: `COPY . .` obeys it and not `.gitignore`, so
without it a local build bakes in whatever is lying around — generated snapshots, past results,
a 97 MB `.git` — while a CI build checking out from git does not. Same Dockerfile, two different
images, and no manifest could tell them apart.

**2. Rent.** 1×H200 141 GB, disk ≥ 250 GB (63 GB of MXFP4 weights plus a ~12 GB image),
instance image = the tag above. Region near the S3 bucket if you are using one.

**3. Run.** Inside the instance:

```bash
cd /workspace/AIsummit26
GENERATE_MISSING=1 \
NEO4J_PASSWORD="$(openssl rand -hex 12)" \
SCALES="1 10" \
VLLM_MODEL=openai/gpt-oss-120b \
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)_h200_prefix_on \
  testbed/bootstrap.sh
```

`GENERATE_MISSING=1` matters because `outputs/` is gitignored: a fresh clone has no
snapshots. The generator is seeded, and regenerating SF1 reproduces the committed manifest's
checksums exactly — the same graph, not a similar one. SF100 is the slow one; seed that from
S3 rather than regenerating it on a metered GPU.

**4. The control arm.** Same command, `PREFIX_CACHING=off` and a different `RUN_ID`. Without
it there is no A/B: vLLM V1 enables prefix caching by default, so an unflagged "control"
measures the treatment a second time.

**5. Get the results off the box before destroying it.** `testbed/vast_run.sh` does the whole
rental as one command and this is the part it exists for — see below. With `S3_BUCKET` set,
`bootstrap.sh` also pushes each step and ends on a gate that exits non-zero unless every local
file is in S3; without a bucket it says so plainly and refuses to call the run safe.

A first pass (SF1+SF10, 7 arms, 3 repeats) is a few hundred episodes; budget the rental for
weight download plus that, and expect the model download to dominate the first ten minutes.

### Driving the rental from the CLI (and why S3 is optional)

`testbed/vast_run.sh` runs the whole lifecycle: search under a price ceiling → create → poll
until `running` → seed snapshots → run `bootstrap.sh` over ssh → pull results → verify the
pull → destroy. `--dry-run` prints every command and needs nothing installed, because
reviewing what a script will spend money on should not itself require the tool that spends it.

```bash
vastai set api-key <key>
vastai create ssh-key ~/.ssh/id_ed25519.pub      # BEFORE creating; keys apply at creation
IMAGE=ghcr.io/<owner>/aisummit26-testbed:<sha> SEED_SNAPSHOTS="1 10" testbed/vast_run.sh
PREFIX_CACHING=off IMAGE=... testbed/vast_run.sh  # the control arm
```

Three properties worth knowing about:

- **The rental is recorded.** `vast_offers.json`, `vast_instance.json` and a trimmed
  `vast_machine.json` (GPU, host, `$/hr`, disk, network, driver, geolocation) land in the run
  directory *while the machine still exists*. A latency number whose machine is gone and
  unnamed is not reproducible, and on vast.ai the machine is gone minutes after the run.
- **Teardown is last and conditional.** The order is run → copy → verify the copy → destroy.
  Anything that fails leaves the instance alive and prints the destroy command: an unattended
  teardown that fires before the copy succeeded is data loss, and the difference in cost is
  cents. The poll loop also treats `exited` / `offline` / `unknown_error` as terminal, because
  polling through them burns disk charges forever while the script looks busy.
- **The price ceiling is enforced on the chosen offer**, not merely requested in the query. A
  query field that turns out not to exist is ignored by the API, and the first result of an
  unfiltered search can be an $8/hr box. This is not hypothetical: the query field for price
  is `dph`, while the price in the result JSON is `dph_total` — the first version of this
  script filtered on `dph_total` and the API silently ignored it.

  The default query also demands `reliability>0.98` (a flaky host ends a 40-minute run at
  minute 30), `inet_down>=500` (63 GB of weights is ~17 minutes at 500 Mb/s) and
  `duration>1` day of offered rental. `gpu_name` takes underscores for spaces
  (`RTX_4090`, not `RTX 4090`).

**So is S3 needed?** For this experiment, no. `vastai copy` moves results home and can seed
snapshots the other way (36 MB of SF100 parquet over the wire beats regenerating it at GPU
rates), and the driver pulls the run directory every `PULL_INTERVAL_S` (default 300s) *while
the run is going* — which is what the S3 watch loop was for. Lose the host at hour two and the
last pull still holds `episodes.jsonl`, so the next rental skips what this one already paid
for.

What S3 still buys, if it ever matters: content-addressed datasets reused across many
rentals without re-uploading, artifacts that outlive your laptop, and a destroy gate that
checks the remote rather than the local copy. `scripts/testbed/s3_ckpt.py` stays for that day;
it is not on the critical path for the first benchmark.

### Two things specific to gpt-oss on vLLM

- **Tool calls need no flag.** vLLM 0.27.1 routes gpt-oss output through `HarmonyParser`;
  the `GptOssToolParser` is only a capability declaration (`tool_parsers/gptoss_tool_parser.py:19`).
  Do not pass `--tool-call-parser` for it.
- **The empty-final-turn quirk may follow the model.** On long multi-page episodes the hosted
  endpoint deterministically spent the closing turn in the reasoning channel and returned
  empty content; the harness answers with one recorded nudge (`nudged` in the episode). If
  vLLM shows the same shape, that is the model's harmony channel, not the testbed.

## Verifying the wiring without a GPU

`scripts/testbed/stub_openai.py` answers the same surface vLLM does — `/v1/models`,
tool-calling `/v1/chat/completions`, `/metrics` — and *simulates* prefix caching by hashing
the message prefix, so `cached_tokens` and the cache counters move. The connector, the CLI
plumbing, the manifest fields and the resume logic can all be exercised on a box with no
driver, which is where they should be found broken rather than on a metered H200.

```bash
python3 scripts/testbed/stub_openai.py --port 8111 &
python3 scripts/agents/agent_interaction.py --provider vllm --model stub-model \
    --base-url http://127.0.0.1:8111/v1 --uri bolt://localhost:7687 --password "$PW" \
    --databases finbenchl1:1 --arms labels --repeats 1 --only ext_easy_1 \
    --out /tmp/stub.json
python3 scripts/testbed/vllm_probe.py --provider vllm --model stub-model \
    --base-url http://127.0.0.1:8111/v1 --repeats 3 --no-stream
```

It is not a model mock: the canned tool call is one fixed Cypher query. It proves the loop
runs, never that an answer is right.

## What the probe measures

`scripts/testbed/vllm_probe.py` follows the paired cold/warm protocol from SEOCHO's
ADR-0148, with two arms:

- **stable** — N requests sharing a byte-identical prefix (task contract → ontology →
  output contract), only the question tail differs. Request 1 is cold; 2..N should be warm.
- **salted** — the same N requests with a unique token prepended *before* the ontology. This
  is the control that separates prefix reuse from the server merely warming up.

Per request it records TTFT (first streamed token), total latency, prompt/completion tokens
and `cached_tokens`; per arm, the delta in the server's own prefix-cache counters scraped
from `/metrics`. `cached_tokens: null` (field absent) and `cached_tokens: 0` (reported miss)
are kept distinct — the first is what the hosted API does, the second is a real miss.

Raw per-request samples are kept, not just medians, per the repo's manifest rule.

## Observability: one trace, two producers

The metrics half of SEOCHO's ADR-0144 was explicitly deferred with the words "LMCache's
reference metrics do not apply to us — we run no local vLLM+LMCache serving layer; we call
external APIs". This testbed *is* that serving layer, so they apply now, and the topology it
already chose (OTLP Collector → Prometheus for metrics, Tempo for traces, Grafana on top) is
what `testbed/observability/docker-compose.yml` brings up.

```bash
docker compose -f testbed/observability/docker-compose.yml up -d   # collector 4317/4318,
                                                                   # prom 9094, tempo 3200,
                                                                   # grafana 3004
OTLP_ENDPOINT=http://127.0.0.1:4317 testbed/serve_vllm.sh
OTLP_ENDPOINT=http://127.0.0.1:4317 python3 scripts/agents/agent_interaction.py \
    --via-seocho --seocho-src /path/to/seocho --seocho-otlp http://127.0.0.1:4317 ...
```

Traces and metrics take different routes, and not by accident:

- **Traces — OTLP, both sides, one trace.** vLLM 0.27.1 extracts `traceparent`/`tracestate`
  from the request (`vllm/tracing/otel.py:127`) and parents its span to that context, so
  `harness/llm.py` stamps the active OTel context onto every outgoing request through an
  httpx send hook. Verified: one trace id holding `episode.ext_easy_1` →
  `run_cypher` (harness) → `llm_request` (vLLM, carrying
  `gen_ai.latency.time_to_first_token`, `.time_in_queue`, `.time_in_model_prefill`,
  `.time_in_model_decode`, `.e2e`, plus `gen_ai.usage.prompt_tokens`). Without it the agent
  side and the serving side are two traces nobody can join — the same defect this repo
  argues against for the graph interface.
  `TRACE_PROPAGATION=off` disables the hook; `OTEL_SERVICE_NAME` is set to `vllm` by the
  serve script, or the serving spans arrive as `unknown_service`.
- **Metrics — Prometheus, scraped, then forwarded.** vLLM 0.27.1 has no OTLP *metrics*
  exporter (only `vllm/v1/metrics/ray_wrappers.py` touches OpenTelemetry), so the collector
  scrapes `:8000/metrics` and forwards it as OTLP, and Prometheus scrapes it directly as
  well — if the collector dies mid-run the serving metrics should not die with it.

### The middle tier: seocho as the orchestrator, and its telemetry

The thesis is three-tier — a graph database on the CPU, a model server on the GPU, and an
orchestrator deciding what crosses between them — and for most of this work the middle tier
was the only one with no telemetry. The harness used seocho as a library of pure functions
(ontology, policy, guardrail) and called the Bolt driver itself, so nothing in the exchange
reported on the layer the experiment is actually about.

`--via-seocho` fixes that by routing each Cypher through `seocho.query.query_proxy.QueryProxy`
— seocho's instrumented execution path — over an adapter that keeps the harness's own driver,
session, `PROFILE`, row cap and stage timers. Same query, same caps, same guardrail decisions;
what changes is that the orchestrator emits. Behind a flag and recorded in the manifest
(`executed_via`), because the published arms ran without it and a silent change of execution
path would leave old and new runs incomparable while looking identical.

```bash
OTLP_ENDPOINT=http://127.0.0.1:4317 python3 scripts/agents/agent_interaction.py \
    --via-seocho --seocho-src /home/hadry/lab/seocho-room/seocho \
    --seocho-otlp http://127.0.0.1:4317 ...
```

Verified end to end against the local collector: one trace id holding `episode` (harness) →
`run_cypher` (harness) → **`db.query` (seocho)**, and 44 `seocho.*` series in Prometheus
including `seocho_retrieval_duration_seconds`, `seocho_retrieval_candidate_count` and
`seocho_retrieval_selected_count`. With a real vLLM behind `--otlp-traces-endpoint`, its
`llm_request` span joins the same trace, so one trace spans all three tiers.

Two things worth knowing. The env names are **`SEOCHO_TRACE_BACKEND`** /
`SEOCHO_TRACE_OTLP_ENDPOINT` for tracing and `SEOCHO_METRICS_BACKEND` /
`SEOCHO_METRICS_OTLP_ENDPOINT` for metrics — not the `SEOCHO_TRACING_*` an earlier version of
this document guessed at — and the metrics backend accepts only `none` or `otlp` (`console`
raises). And **which seocho answered matters**: the pip build on this box is 0.2.0 with no
`seocho.query` at all, while the working tree at `/home/hadry/lab/seocho-room/seocho` is 0.6.0
with 119 spec'd instruments. `--seocho-src` selects the tree and the manifest records the
version and path that actually loaded, because "seocho" is not a version.

### The metric names that matter (0.27.1)

Names moved between releases — earlier versions used `vllm:gpu_prefix_cache_*` — so nothing
in this repo hardcodes them: `harness/environment.py` keeps whatever `/metrics` lines match a
keyword set, verbatim, in the run manifest.

| Metric | Why it is here |
| --- | --- |
| `vllm:prefix_cache_queries_total` / `vllm:prefix_cache_hits_total` | token-denominated, so their ratio is a token hit rate directly comparable to `cached_tokens / prompt_tokens` (verified: 2 requests × 515 prompt tokens → 1030 queries, 512 hits) |
| `vllm:prompt_tokens_cached_total` | "cached prompt tokens (local + external)" as a counter |
| `vllm:external_prefix_cache_queries_total` / `_hits_total` | the offload tier — LMCache/Mooncake — zero until one is configured, which is exactly the next axis |
| `vllm:kv_block_lifetime_seconds`, `vllm:kv_block_idle_before_evict_seconds`, `vllm:kv_block_reuse_gap_seconds` | how long a block *stays*. A hit rate says the ontology block was there; these say whether it survives between episodes, which is the actual "shared hot tier" question. Gated behind `--kv-cache-metrics` and sampled at 1% by default (`config/observability.py:48-53`) — the serve script raises the sample rate, or the histograms stay empty at our volume |
| `vllm:time_to_first_token_seconds`, `vllm:request_queue_time_seconds`, `vllm:request_prefill_time_seconds`, `vllm:request_decode_time_seconds` | the prefill/decode split the hosted endpoint only gave us through a vendor SDK |
| `vllm:kv_cache_usage_perc`, `cache_config_info` | how much KV budget exists at all, and under what block size |

`--enable-prompt-tokens-details` is **off by default** (`vllm/entrypoints/openai/cli_args.py:132`).
Without it every response carries `prompt_tokens_details: null`; the server counters still
move, but they are process-wide and cannot attribute a cache hit to one episode. The serve
script passes it always. With it, responses also carry `created_cache_tokens` — what this
request *contributed* to the cache, which separates "nobody will reuse this" from "this one
paid for the next".

### Reading these metrics without fooling yourself

vLLM's own metrics design document settles several things that are easy to get wrong, and
the dashboard in `testbed/observability/dashboards/` is built to its guidance:

- **Hit rate is not a metric, it is a query.** vLLM deliberately exposes *queries* and
  *hits* as counters instead of a hit-rate gauge, so the averaging window belongs to the
  reader: `rate(vllm:prefix_cache_hits_total[5m]) / rate(vllm:prefix_cache_queries_total[5m])`.
  The probe does the same thing over an arm's counter delta rather than reading a gauge.
- **Two different hit rates exist. They are not comparable.** The Prometheus counters are
  **token**-denominated (verified: 2 requests × 515 prompt tokens → 1030 queries, 512 hits).
  The 5-second INFO log line reports the hit rate over the most recent **1k block** queries.
  Same words, different denominator and different window.
- **The `_total` suffix is added by the exposition layer**, not part of the metric name:
  vLLM declares `vllm:prefix_cache_queries`, Prometheus renders
  `vllm:prefix_cache_queries_total`. Code that greps the scrape must expect the suffix while
  code that greps vLLM's source must not. `_created` lines are OpenMetrics birth timestamps,
  not measurements — the manifest filter drops them, or a unix epoch ends up sitting next to
  real counters.
- **Server-side TTFT ≠ client-side TTFT.** vLLM measures TTFT from the frontend's
  `arrival_time`, which includes tokenisation but not the client's network hop, so the
  probe's number reads slightly higher. Queue/prefill/decode intervals come from engine-core
  monotonic clocks — comparable to each other, meaningless against any wall clock or against
  timestamps from another process.
- **Keep `--api-server-count` at 1 for measurement runs.** Above 1, metrics move to
  multiprocess mode and the `python_*`/`process_*` series disappear entirely; `cache_config_info`
  survives only as a `mostrecent` gauge. Scaling the API server is a different experiment
  from the one being run here.
- **Don't reach for V0 names.** `vllm:num_requests_swapped` and `vllm:cpu_cache_usage_perc`
  describe a swap-to-CPU preemption mode that V1 removed along with `--swap-space`;
  `vllm:time_in_queue_requests` is the duplicated queue metric — use
  `vllm:request_queue_time_seconds`. `--show-hidden-metrics-for-version` is the escape hatch
  if a removed series is genuinely needed.
- **`--collect-detailed-traces=model|worker|all` is diagnostic-only.** It adds
  `gen_ai.latency.time_in_model_forward` / `.time_in_model_execute` to the spans (and the
  `vllm:model_*_time_milliseconds` histograms), and vLLM's own documentation warns it uses
  "possibly costly and or blocking operations". Enable it for one diagnostic run, never for
  the arms being compared. (Note the official docs spell the endpoint flag
  `--oltp-traces-endpoint`; the implementation is `--otlp-traces-endpoint`,
  `engine/arg_utils.py:1417`.)

The provisioned dashboard (Grafana → *AIsummit26 — ontology prefix on vLLM*) carries six
panels, each validated against live data from this box: token hit rate including the
`external_prefix_cache` offload tier, TTFT p50/p99, the queue/prefill/decode split, KV block
lifetime against idle-before-evict (on one chart, per vLLM's own advice, because together
they show stranded cache), KV usage with queue depth, and prompt tokens total against
prompt tokens served from cache.

### Measured on this hardware (RTX 3070, Qwen2.5-1.5B, 2.2k-token ontology prefix)

Wiring validation, not a talk figure — the point is that the instrument reads:

| server | arm | warm TTFT | `cached_tokens` | server token hit rate |
| --- | --- | --- | --- | --- |
| `--enable-prefix-caching` | stable prefix | **0.024 s** | 2,240 / 2,258 | 0.824 |
| `--enable-prefix-caching` | salted control | 0.203 s | 0 | 0.000 |
| `--no-enable-prefix-caching` | stable prefix | 0.216 s | 0 | metric absent |
| `--no-enable-prefix-caching` | salted control | 0.219 s | 0 | metric absent |

Cold TTFT was 0.54 s in both servers. So the ~9x warm prefill win is prefix reuse, and the
salted arm inside the *same* server is what rules out "the server just warmed up". Note the
last two rows: with caching off vLLM stops exporting the prefix-cache counters entirely,
which makes the flag itself verifiable from the manifest rather than trusted.

### On the rented box, the dashboards are optional

A vast.ai instance is itself a container, so the four-service compose above needs
docker-in-docker there. It is not worth choosing machines by that: metrics are the live view,
and the durable record is the run directory. `scripts/testbed/metrics_sampler.py` walks the
same `/metrics` endpoint on the same 5s interval and appends each sample to
`results/runs/<id>/metrics.jsonl` (wall clock *and* monotonic clock per sample, because
intervals derived from a wall clock are wrong across an NTP step). `bootstrap.sh` starts it
for the duration of the episodes, so the whole run is analysable — and replayable into a
local Prometheus — after the instance is gone.

Bring the Grafana stack up locally for analysis, or on the instance only if the machine you
rented can run docker.

## Four stages, and why stage 2 cannot come before stage 3

The questions this repo ends on are about scale, and scale is expensive to rent. So the work
splits into four stages with very different costs — but the order that looks obvious is wrong.

| # | Stage | Endpoint / tool | Cost | Answers | Cannot answer |
| --- | --- | --- | --- | --- | --- |
| 1 | **Workload extraction** | MARA **or** a self-hosted vLLM, through `harness/llm.py` | API tokens only | how many LLM calls an episode takes, how long each prompt and generation is, how long the tool runs between them, which spans are shared | anything about caching, if the endpoint has none |
| 2 | **Simulation** | LLMServingSim, local, CPU-only | free | 100 tenants, weights off the accelerator, TP/PP/PD layouts — every counterfactual | absolute latency, unless calibrated against stage 3 |
| 3 | **Real measurement** | rented GPU | \$4–15/hr | what the hardware actually does, and the constants stage 2 needs | anything the rental cannot hold: more GPUs, 100 tenants |
| 4 | **Kernel work** | Nsight + CUDA docs | rental time | where a kernel's time goes and whether it can be moved | whether moving it changes the system-level answer |

**Stage 1 works from either endpoint, and that is the point.** MARA is the cheap way to get
workload *shape*: per-turn tokens, tool waits, the real prompt text. It cannot report cache
behaviour — verified, `cached_tokens` is absent and TTFT is flat across identical prefixes —
so every turn it records shows `cached_tokens: 0`. A self-hosted vLLM gives the same shape
*plus* real cache behaviour, at GPU prices. Use MARA to build workloads, vLLM to check them.

For Agent API ↔ Bolt experiments, `bench_agent_topology.py` also writes the complete PROFILE
operator tree for every executed query and starts a durable `system_metrics.jsonl` sampler by
default. The sampler covers the client host and the explicitly named database container; pass
the actual container with `--db-container`, then require a non-empty metrics file and a
`system_monitor_receipt.complete=true` before accepting the run. Use
`--no-system-metrics` only for a deliberately named no-monitor control. Hosted endpoint GPU,
KV-cache and scheduler state remains unavailable and must be measured in a separate
self-hosted vLLM arm.

**Stage 2 is not a measurement.** Its own documentation is blunt about this: with the default
`mem_util: 0.9` its TTFT was off by −20.7%, and it landed at +0.6% only after `mem_util` was
matched to the real run's resolved block count. And it warns that KV capacity is invisible
until a run actually *fills* it. Our entire question lives in the saturating regime, so the
order is:

```
1 (workload, cheap)  →  3 (short: profile + anchor)  →  2 (sweeps, free)  →  3' (verify the
one configuration the sweep chose)  →  4 (kernel)  →  back to 2 with the new profile
```

Stage 4 closes the loop rather than ending it: a kernel improvement shows up in the profile
bundle's `attention.csv` / `dense.csv` / `moe.csv`, so re-profiling (or hand-editing the
bundle to model the win) lets stage 2 answer the only question that matters about it — whether
a faster kernel changes the system-level outcome, or whether the run was bound by cache
capacity all along. On this workload the second is the live hypothesis: the 14.8× we measured
came from *not* prefilling, and no kernel beats work that is skipped. Where kernel work does
bite for us is narrower and specific: the MXFP4 path for gpt-oss, the attention backend at
long context, and the KV transfer path an offload tier depends on.

## Hardware available to the simulator

Three tiers, and one distinction that matters more than the list: **memory is free to
change, compute is not.** `npu_mem.mem_size` / `mem_bw` / `mem_util`, `cpu_mem` and the whole
`placement` block are plain config. Compute latency comes only from a profile
bundle under `profiler/perf/<HARDWARE>/`. Raising `mem_size` from 96 to 143 and calling it an
H200 gives H200 memory with RTX PRO 6000 compute — fine for a capacity study, not something
to label with a GPU's name.

**Ready now (bundled profiles, zero cost).** RTX 4090 with Llama-3.1-8B (tp1);
RTX PRO 6000 with Llama-3.1-8B, Qwen3-30B-A3B-Instruct-2507 and Qwen3-32B (tp1, tp2).

**One profiling rental each** — the profiler works as-is wherever vLLM does: A100, H100,
**H200**, RTX 6000 Ada, L40S, Blackwell B100/B200 (needs the `cu130` image), Hopper SXM,
AMD MI300X (ROCm). `HARDWARE` is just a folder label that `cluster_config.hardware` must
match. Budget from the docs' own estimates: dense and per-sequence in seconds, attention
5–15 min, MoE 10–30 min, skew 10–25 min — so **30–70 minutes per (model, TP)**, about \$3–5
on a single H200.

*But not gpt-oss.* The profiler pins vLLM `0.19.0`, which predates gpt-oss, and
`profiler/models/` has no `gpt_oss.yaml`. The practical target is
**H200 × Qwen3-30B-A3B-Instruct-2507** — MoE like gpt-oss, `model_type: qwen3_moe` with an
architecture YAML already present and its HF config already vendored at
`configs/model/Qwen/`. Rent with the image `vllm/vllm-openai:v0.19.0` so the instance *is*
the profiler's container, and no docker-in-docker is needed.

**Synthesizable without silicon** — for anything vLLM cannot serve (TPU, a custom NPU, a PIM
part), the CSV bundle *is* the contract: `dense.csv` (`layer, tokens, time_us`),
`per_sequence.csv`, `attention.csv` (`prefill_chunk, kv_prefill, n_decode, kv_decode,
time_us` — a 4D grid, and our axis is two of its dimensions), and `moe.csv` for MoE. Write
those four tables from any source and the simulator treats the part as real. PIM memory types
ship as `configs/pim/*.ini` (DDR4-3200, HBM2-2000, LPDDR4X-4266, LPDDR5-6400).

## Parity, and what these runs are not

- **These are new arms, not replacements.** The published figures come from this repo's
  hosted-endpoint runs. A vLLM run measures a different serving stack; putting it in the
  same figure without saying so would make numbers appear to change between iterations.
- **Model parity is deliberate.** `gpt-oss-120b` is the model the published arms used, which
  is why the sizing above insists on a GPU that can serve it rather than a smaller model
  that would confound serving effects with model effects.
- **Library parity is imperfect and visible.** The image pins seocho to the tag this repo
  declares (`aisummit26-interface-v1`), which builds as `0.5.0`; the published runs'
  `software.json` records `0.2.0`, because the tag was re-pointed after those runs. Every run
  records what actually loaded, so the drift stays legible instead of being assumed away.

## Gotchas that cost real time

- **Prefix caching is on by default** in vLLM V1. A control arm that does not pass
  `--no-enable-prefix-caching` measures the treatment.
- **`Account._out_degree` is load-bearing.** The episode harness picks each run's anchor from
  it (p99 of out-degree, then the lowest `acct_no` at or above it). A graph loaded without
  that property runs every question against anchor `None` — a silently empty run, not a
  failure. `export_import_csv.py` computes it during export.
- **The GPU gate fails fast on a broken driver.** An `nvidia-smi` that returns nothing means
  the driver is not responding; waiting 30 minutes for it to heal is not a gate, it is a
  hang.
- **Agents SDK tracing** tries to upload to OpenAI and 401s on every episode. The image sets
  `OPENAI_AGENTS_DISABLE_TRACING=1`.
