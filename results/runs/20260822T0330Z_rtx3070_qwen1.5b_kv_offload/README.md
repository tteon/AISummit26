# Is a host-RAM KV tier worth it? Three arms, and one of them dies

Measured 2026-08-22 on the local box (RTX 3070 8GB, driver 595.84, vLLM 0.27.1,
Qwen2.5-1.5B-Instruct, `--num-gpu-blocks-override 2048` → **GPU KV = 32,768 tokens**), so
eviction is cheap to provoke and the config hunt costs nothing. LMCache 0.5.4.

The premise being tested is *not* "does the cache hit". It is the warning that an offload tier
can be the bottleneck: fetching an evicted prefix over PCIe competes with recomputing it, and
every request pays the write path whether or not anything is ever reused. So each arm reports
five points, and the one that decides the question is `evicted_fetch` vs `absent`.

Prefix ≈ 6,869 prompt tokens; flooded with 5 unique prefixes (≈34k tokens > 32,768) to force
the subject out of GPU blocks.

| point | GPU only | LMCache, layerwise **off** | LMCache, layerwise **on** |
| --- | --- | --- | --- |
| `insert` (first sight, pays the write) | 1.244 s | 1.033 s | — |
| `gpu_warm` (ceiling) | 0.055 s | 0.051 s | — |
| flood requests (unrelated traffic) | 0.652–0.672 s | 0.686–0.699 s | — |
| **`evicted_fetch`** (the question) | **0.650 s** | **0.094 s** | — |
| `absent` (recompute floor) | 0.665 s | 0.693 s | — |
| verdict `evicted_fetch / absent` | 0.978 | **0.135** | — |
| `external_prefix_cache_hits_total` | 0 | 6,624 | — |

## What the numbers say

**The tier wins on the traffic it was built for.** An evicted 6.9k-token prefix comes back in
0.094 s instead of being recomputed in 0.693 s — **7.4×** — and 6,656 of 6,869 tokens are
served from host RAM. Without the tier, eviction is indistinguishable from never having seen
the prefix (0.650 vs 0.665, ratio 0.978), which is the baseline behaving exactly as it should.

**And it taxes everything else.** Compare the columns on traffic that cannot benefit: `absent`
goes 0.665 → 0.693 s and the five flood requests go ~0.66 → ~0.69 s, a consistent **+4%**.
That is the write path — every request's blocks are copied to host RAM on the way past. A
workload with no prefix reuse pays that 4% for nothing. Whether 7.4× on reused traffic is
worth 4% on the rest is a property of the workload's reuse rate, not of LMCache.

**Layerwise, the thing that is supposed to make the fetch cheap, does not run here.** With
`LMCACHE_USE_LAYERWISE=True` the engine dies on the *first request* — not at startup:

```
lmcache/v1/gpu_connector/utils.py:449 assert_is_vllm_mla_or_flash_attn_or_flash_infer
AssertionError            → vllm.v1.engine.exceptions.EngineDeadError
```

vLLM 0.27.1 hands LMCache a KV layout of `EngineKVFormat.NL_X_NB_BS_NH_CS` — K and V
combined — while the layerwise path accepts only the six layouts where K and V are separate
(flash-attention NHD/HND, flash-infer NHD/HND, MLA, and one more). Forcing
`VLLM_ATTENTION_BACKEND=FLASHINFER` changed nothing: the log still shows `Using FLASH_ATTN`,
so the override did not take and the layout was identical. `lmcache_native` is compiled in the
wheel, but `lmcache.c_ops` is not (`CudaDeviceOps stays o…` warning), which is a second reason
to expect the fast paths to be missing from a pip install.

So on this stack the only usable configuration is the **sequential** one: the whole span
transfers before compute starts. That it still wins 7.4× says the transfer is far cheaper than
recompute *at this size and this KV width* — 28 KB/token for Qwen2.5-1.5B, ~190 MB for the
subject prefix. It does not follow that it wins on the H200 case, where KV is 41.5 KB/token and
a 54k-token prefix is ~2.2 GB: the transfer grows linearly while recompute also grows, and the
ratio has to be measured, not extrapolated.

## What this changes about the earlier H200 attempt

The 2026-08-21 hang (`LMCACHE_MAX_LOCAL_CPU_SIZE=600`, ten minutes in `local_cpu_backend`) is
consistent with an oversized host allocation: 8 GB here initialises in seconds. The rented run
should use a pool sized to the working set, not to the machine.

## Honest limits

- One repetition per point. These are single-shot latencies on an idle box, adequate to
  separate 0.09 s from 0.69 s and *not* adequate for a 4% claim to be called precise — the
  overhead figure is a direction, not a measurement.
- `insert` also carries first-request warmup, which is why the LMCache column looks *faster*
  there. Do not read that cell as an insert-cost measurement.
- 1.5B model, 8 GB card, 32k KV. The mechanism transfers; the numbers do not.

Raw per-request samples, serve flags and the two failure logs are in this directory.
