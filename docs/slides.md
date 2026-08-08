# The deck — two charts

Two charts go on slides. Everything else in this repo is evidence: it stays out of the
deck, regenerates on demand, and gets pulled up only if a question asks for it.

---

## 1 · The big frame — `figures/overview-by-question.svg`

**The sentence: this is the whole experiment — 13 questions × SF1→100 × 7 designs,
819 episodes, and the designs pull apart as the graph grows.**

- y-axis is median db hits per episode (log) — the one cost unit unaffected by whatever
  else runs on the box.
- Marker fill carries correctness: filled = all three repeats matched gold, hollow = none,
  grey = some.
- Reading order for the talk:
  1. **The easy panels** — every design sits on top of the others. For a one-hop question,
     nothing you tell the agent matters.
  2. **ext_hard / int_med** — labels-only (orange ○) floats to the top and goes hollow:
     wrong and expensive at once as scale grows.
  3. **The blind control's flat, hollow line** (light violet +) — one page fetched,
     cheapest on the panel, wrong at every scale. Silent failure, drawn.
  4. **int_hard_2** — the in-context lines reach 10⁸ db hits: the top-end price of taking
     aggregation away from the database.
- The legend is the story: designs 1–4 let the database aggregate; 5–7 pull the rows into
  the model's context. Seven colors + seven marker shapes, CVD-validated.
- Nothing on this chart is hardcoded — every point is computed from
  `results/agent_interaction.json` at render time
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
| `python scripts/plot_interaction.py` | p99 by difficulty/question, accuracy, cost | the replay-based p99 results for designs 1–4 |
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
