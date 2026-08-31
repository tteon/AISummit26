# Topology-conditioned Agentic FinBench

Scale factor is not a topology descriptor.  It says how much data was generated, but it does
not say whether a request reaches a hub, a dense neighbourhood, a long-tail component or a
mostly tree-like payment graph.  Those properties decide both the database work and the
context a graph-aware agent needs.

## Measured SF1 and SF100 baseline

The same bounded measurement was run over both committed Parquet snapshots.  Local clustering
is sampled only among nodes of simple degree 2–200; directed three-cycles exclude endpoints
above out-degree 40.  These bounds are part of the result, because an unbounded topology
measurement at a hub would itself be an uncontrolled benchmark workload.

| Metric | SF1 | SF100 | Interpretation |
| --- | ---: | ---: | --- |
| transfer edges | 10,895 | 1,000,895 | 91.9× more edges |
| nodes incident to transfer | 2,470 | 200,463 | 81.2× more nodes |
| mean total transfer degree | 8.822 | 9.986 | modest increase |
| p99 total degree | 19 | 18 | no tail increase at this percentile |
| maximum total degree | 421 | 421 | the planted hub cap is constant |
| average sampled local clustering | 0.007677 | 0.000099830 | 76.9× lower at SF100 |
| directed 3-cycle lower bound | 57 | 45 | no motif-density growth claim |

The conclusion is deliberately negative: **SF100 is not evidence that hub intensity or
clustering grew.**  This generator scales volume while keeping the maximum planted hub fixed
and makes the sampled transfer graph less locally clustered.  A talk should state that rather
than let scale masquerade as network complexity.

Raw topology receipts are [SF1](../results/analysis/topology_sf1.json) and
[SF100](../results/analysis/topology_sf100.json).

## Pair requests to topology, not merely SF

`scripts/analysis/pair_request_topology.py` joins each raw agent episode on `(sf, anchor)` to:

- global graph profile: edge count, sampled local clustering and cycle lower bound;
- anchor-local profile: transfer in/out degree, direct counterparty count and a capped
  outgoing two-hop reach; and
- the already recorded request outcome: correctness, graph trips, PROFILE DB hits and DB ms.

The first pairing of the 18 MARA repair episodes has two different selected anchors:

| SF | anchor | in / out transfer degree | direct peers | bounded 2-hop reach |
| --- | ---: | ---: | ---: | ---: |
| 1 | 108 | 5 / 13 | 18 | 64 |
| 100 | 7 | 6 / 11 | 17 | 49 |

They are ordinary, not hub, anchors.  This pairing is therefore a **receipt and a diagnostic,
not a causal estimate**: two scales and one anchor per scale cannot identify whether any cost
difference comes from size, topology or endpoint variation.  The raw join is retained at
[MARA topology pairing](../results/analysis/mara_repair_topology_pairing_v1.json).

## Story and next controlled sweep

Classify request difficulty by its topology-conditioned query exposure rather than prompt
wording:

| Tier | Request shape | Primary topology exposure | Middleware implication |
| --- | --- | --- | --- |
| easy | indexed anchored scalar / one hop | anchor in/out degree | bind subject and cap rows before model execution |
| medium | group aggregate, ownership/device join | fan-out, counterparty diversity, endpoint direction | return typed cardinality/degree hints in RequestContract |
| hard | bounded layering, intersections, motif/cycle query | 2-hop reach, closure/cycle density, component membership | require hop/cost budget, plan admission and PROFILE receipt |

For a causal topology arm, keep the same model, endpoint, physical schema, request text and
query class, then sweep **curated anchor bands** (median, p99, p99.9, hub) within each SF.
Pair the same request across matched bands where possible and retain a per-episode topology
row.  Fit or simply stratify DB hits, DB ms, graph trips, context tokens, repair admission and
correctness by these descriptors.  Do not regress on SF alone, and do not claim that a global
clustering coefficient explains an anchored request without its local descriptor.

This gives the middleware narrative: a graph database should expose a bounded topology receipt
(anchor degree percentile, estimated fan-out and permissible hop budget) to the agent.  The
agent should return a typed plan request under that budget, while the database returns
PROFILE/Bolt evidence.  The contract lets a service refuse an unsafe hub expansion before it
becomes an expensive repair loop.

## Frozen anchor-band preflight

The first DB-only workload has now frozen 88 executable cells: 2 scales × 4 curated account
bands (median, p99, p99.9, hub) × 11 account-anchored requests.  Its raw manifest and every
per-cell parameter/result-shape receipt are in
[topology_anchor_workload_sf1_sf100_v1.json](../results/analysis/topology_anchor_workload_sf1_sf100_v1.json).
All cells completed; this is an admission gate for a later paid agent arm, not an agent result.

There is an important generator constraint: the curated hub is account `9000100` with transfer
degree 421 in **both** SF1 and SF100.  Consequently, this workload can contrast topology bands
within each scale, but cannot attribute a hub effect to scale.  To make the latter claim, a new
generator regime must vary hub degree/multiplicity/closure while holding total scale and query
template fixed.  That would be a new dataset and a new experimental arm, never a replacement
for the existing scale results.
