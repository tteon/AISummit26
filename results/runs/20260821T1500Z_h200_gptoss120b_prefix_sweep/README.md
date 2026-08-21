# gpt-oss-120b on one H200: what a shared ontology prefix costs

Measured 2026-08-21 on a rented vast.ai instance (1×H200 143GB, driver 580.159.03, Saudi
Arabia, $3.98/hr — full machine record in `vast_machine.json`). Model
`openai/gpt-oss-120b`, `--max-model-len 131072`, `--gpu-memory-utilization 0.92`, which the
server resolved to **63.22 GiB of KV = 1,635,338 tokens**. Serve flags for every run are in
`flags_*.json`; raw per-request samples are in the `probe_*.json` files.

This is the axis the hosted endpoint could not answer. On MARA, `cached_tokens` is absent and
TTFT is flat across byte-identical prefixes — there is no prefix cache to hit. Here there is.

## 1. A cached prefix is nearly free, and the benefit grows with its length

TTFT in the steady state (warm), same model, same server, one flag apart. `stable` = every
request shares the prefix; `salted` = a unique token in front of the same prefix, so no two
requests share a cache entry. The salted arm is what separates prefix reuse from a server
that merely warmed up.

| prompt tokens | ON · stable | ON · salted | OFF · stable | OFF · salted | reuse speedup | cached/prompt |
| --- | --- | --- | --- | --- | --- | --- |
| 1,847 | **0.041 s** | 0.098 s | 0.100 s | 0.100 s | 2.4× | 1,824 / 1,848 |
| 6,826 | **0.054 s** | 0.216 s | 0.215 s | 0.215 s | 4.0× | 6,800 / 6,827 |
| 26,889 | **0.100 s** | 0.952 s | 0.949 s | 0.937 s | 9.5× | 26,864 / 26,890 |
| 54,493 | **0.157 s** | 2.314 s | 2.319 s | 2.326 s | **14.8×** | 54,472 / 54,494 |

Three columns say the same thing three ways: caching off with a shared prefix, caching off
with a unique prefix, and caching **on** with a unique prefix all cost full prefill, and agree
to within 2%. Only the fourth combination — caching on *and* a genuinely shared prefix — is
cheap. That is the control working.

The trend is the finding. At 1.8k tokens prefix reuse is a 2.4× convenience; at 54k it is the
difference between 0.16 s and 2.3 s per call. Extending context windows is cheap now
(Position Interpolation, Chen et al. 2023 — linear down-scaling of RoPE position indices,
which vLLM exposes as `--rope-scaling '{"rope_type":"linear","factor":N}'` and this repo
passes through as `ROPE_SCALING`), so the long end of this table is the realistic end, and
prefix reuse stops being an optimisation there.

Caveat on the cold column, stated because the numbers show it: longer prefixes are built by
repeating the ontology block, so the 32k prefix literally begins with the 8k one and its first
request already reports 6,768 cached tokens. Cold points are therefore partly warm. The
warm-vs-salted comparison, which is what the table reports, is unaffected.

## 2. The cache is all-or-nothing: at capacity, reuse collapses to zero

`probe_PRESSURE_gpuonly.json` cycles 36 byte-unique prefixes of 45,314 tokens each —
**1,631,304 tokens against a 1,635,338-token KV budget**, i.e. 99.75% of capacity — three
times over.

| round | mean cached/prompt | median TTFT |
| --- | --- | --- |
| 0 | 0.002 | 1.834 s |
| 1 | 0.0021 | 1.831 s |
| 2 | 0.0021 | 1.829 s |

Round 2 is as expensive as round 0. Under cyclic access with a working set at capacity, LRU
evicts each prefix just before it is needed again, so a cache that holds 99.75% of the working
set returns **0.2%** of it. The cliff is not gradual: either the whole working set fits, or
prefix caching does nothing at all. Server counters agree — 4,893,912 queries, 10,272 hits.

For an agent workload, "working set" is the number of distinct tenants/sessions whose
prefixes are live, times the prefix length. One H200 holds ~30 concurrent 54k-token prefixes.
The 31st does not degrade the 30th a little; it starts a thrash.

## 3. The offload tier: attempted, not achieved

That cliff is what a host-RAM KV tier is for. This box had **1,026 GB usable** (cgroup
`memory.max`) against 63 GiB of GPU KV — roughly 16× the capacity at ~41.5 KB/token.
LMCache 0.5.3 was installed and initialised correctly against vLLM 0.27.1
(`LMCacheConnectorV1`, `kv_role=kv_both`, LRU policy, local CPU backend), and then engine
startup **hung for ten minutes** in `local_cpu_backend` and was killed by the readiness
timeout (`vllm_lmcache_tail.log`). The suspect is `LMCACHE_MAX_LOCAL_CPU_SIZE=600`, i.e.
asking for a 600 GB pinned host allocation, plus `lmcache.c_ops compiled extension not found`
falling back to a slower path.

Not a negative result about offloading — a configuration that was not tuned. The next attempt
belongs on the local RTX 3070 with a small model, where iterating on
`LMCACHE_MAX_LOCAL_CPU_SIZE` and the pinning flags costs nothing, and only then on a rental.
`vllm:external_prefix_cache_queries_total` / `_hits_total` stayed at 0 throughout, which is
exactly what "the tier never came up" should look like.

## Files

| file | what |
| --- | --- |
| `probe_ON.json` / `probe_OFF.json` | the sweep, with per-request samples, TTFT, cached_tokens, counter deltas, KV block residency |
| `probe_on.json` | the first ON sweep, before TTFT was fixed for the harmony reasoning channel — kept because its total-latency numbers agree |
| `probe_PRESSURE_gpuonly.json` | the eviction test |
| `flags_on2.json` / `flags_off.json` / `flags_press.json` | exact serve flags per run |
| `vllm_on2.log` / `vllm_off.log` / `vllm_press.log` | server logs, including the resolved KV size |
| `vllm_lmcache_tail.log` | the LMCache startup hang |
| `vast_machine.json` | the rented machine: GPU, host, price, network, driver, uptime, cost |
| `metrics_final_off.txt` | a full `/metrics` scrape |

Reproduce: `docs/testbed.md`. One gotcha found the hard way and now fixed in the probe —
gpt-oss streams its first tokens on `delta.reasoning_content`, so a TTFT that only watches
`delta.content` reports `null` on this entire model family.
