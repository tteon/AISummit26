# The deck — two charts

Two charts go on slides. Everything else in this repo is evidence: it stays out of the
deck, regenerates on demand, and gets pulled up only if a question asks for it.

---

## 1 · The big frame — `figures/overview-p50.svg` and `figures/overview-p99.svg`

**The sentence: query latency against scale, easy/medium/hard pooled, all seven designs
labelled — p50 is what a user usually feels, p99 is what breaks the SLO.**

- Three panels per chart (easy · medium · hard — the category's questions pooled,
  4/4/5 questions × 3 repeats), SF across, per-call DB latency up (log).
- Marker fill carries correctness: filled = every episode in the cell matched gold,
  hollow = none, grey = some.
- Each chart carries a dashed SLO reference: **200 ms** (interactive per-call budget) on
  p50, **1 s** (a common request p99 SLO) on p99 — say out loud that these are industry
  conventions for orientation, not measurements of this system.
- Reading order for the talk:
  1. **easy, p50** — the designs bunch; what you tell the agent barely matters per call.
  2. **easy, p99** — labels-only (orange ○) crosses the 1 s line by SF10 and ends near
     10 s; every informed design holds under it until SF100. The tail punishes the
     uninformed design first — and now the chart says when.
  3. **medium/hard, both charts** — the spread widens with SF; the in-context page
     queries (violet/pink) are cheap *per call* while their cost lives in call count and
     tokens — which is the hand-off to the engineering chart.
  4. **The blind control** (light violet +) is cheap and hollow — one page, wrong.
- Both charts compute from `results/agent_interaction.json` at render time — every
  executed call's measured ms, nothing hardcoded
  (`python scripts/check_chart_provenance.py` verifies).

## 2 · The engineering detail — `figures/engineering-detail.svg`

**The sentence: a row is charged four times on its way to the model — encoding,
transport, runtime, concurrency. Every number is measured.**

| Panel | The number | The talking point |
|---|---|---|
| Encoding | same 200 rows: JSON 9,017 vs CSV 5,211 tokens | the data is ~2,100 tokens in any encoding; the rest is per-row keys |
| Transport | 100k rows produced, 50 wanted: HTTP 398 ms vs Bolt 12 ms vs LIMIT 1.4 ms | the 276× spread closes with one clause in the query — the contract, not the transport |
| Runtime | CPU per row: consume 20.8 µs vs produce 2.9 µs (7×); 346 B/row in both decoder builds | the cost is the representation (Python objects), not the codec |
| Concurrency | 8-worker p50: threads 769 → processes 81 → native 7.7 ms | the ceiling was the GIL; conclusion: Python control plane, native data plane |

- If asked for evidence: per-iteration samples and machine manifests live in
  `results/bench/`, the 819 episodes in `results/agent_interaction.json`.

---

## Backups (Q&A only — the repo commits the two charts above; regenerate the rest)

| Regenerate with | What you get | The question it defends |
|---|---|---|
| `python scripts/dump_conditions.py` | the conditions matrix | "exactly one thing differs between adjacent conditions" |
| `python scripts/plot_interaction.py` | replay-based p99 by difficulty/question | the model-free p99 for designs 1–4 (100 replays/query) |
| `python scripts/plot_overview.py --by-question` | the 13-panel per-question db-hits detail | any single question's scaling |
| `python scripts/plot_in_context.py` | outcomes (71 vs 11), per-question db hits for the trio | the causal claim about `more_available`, with its control |
| `python scripts/plot_depth.py` | full-size versions of each engineering panel | any single panel, in depth |
| `python scripts/plot_levers.py` | the eight levers in two labelled blocks | the closing line: "two kinds of fix, neither substitutes for the other" |

**Number audit**: `python scripts/check_chart_provenance.py` traces every constant on both
deck charts to its recorded measurement in `results/bench/*.json` and
`results/agent_interaction.json`, and exits nonzero on drift. It has already paid for
itself once: two rust-thread constants from a pre-instrumentation run disagreed with the
recorded measurement and were corrected to it.

## The arc in one line

As the graph grows 100×, agent designs pull apart → half the split is what the model is
told (the contract), half is how rows reach the model (the runtime) → so there are two
kinds of fix, and neither substitutes for the other.
