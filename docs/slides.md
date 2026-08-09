# The deck — the charts

The committed figures go on slides. Everything else in this repo is evidence: it stays out of the
deck, regenerates on demand, and gets pulled up only if a question asks for it.

---

## 1 · The big frame — `figures/overview-p50.svg` and `figures/overview-p99.svg`

**The sentence: query latency against scale for the contract chain (conditions 1–4) —
p50 is what a user usually feels, p99 is what breaks the SLO. The in-context regime
(5–7) is a different question and lives in its own figure set.**

- Three panels per chart (easy · medium · hard — the category's questions pooled,
  4/4/5 questions × 3 repeats), SF across, latency up (log). Four lines per panel: the
  cumulative chain only. Controls are not designs — overlaying 5–7 here compared one
  expensive call against five cheap pages on one axis, and the blind control is an
  experimental instrument, not something anyone ships.
- **p50** = every executed live call's measured ms.
- **p99** = the stage-two replays: the query each design settled on, run **100× without
  a model**, first execution discarded, geometric mean over the difficulty's questions.
  Say out loud why: a live cell has as few as a dozen calls, and a p99 of twelve samples
  is the maximum wearing a costume; the replay gives every condition the same n. It also
  removes the plan arm's probe-warm bias — a query that passed the 2 s probe runs its
  live full execution cache-warm, so live tails are not comparable across arms.
- Marker fill carries live-episode correctness on both charts: filled = every episode in
  the cell matched gold, hollow = none, grey = some.
- Dashed SLO references: **200 ms** (interactive per-call budget) on p50, **1 s** (a
  common request p99 SLO) on p99 — industry conventions for orientation, not
  measurements of this system.
- Reading order for the talk:
  1. **easy, p50** — the chain bunches; per call, what you tell the agent barely matters.
  2. **easy, p99** — labels-only (orange ○) breaks the 1 s line while every informed
     design holds. The tail punishes the uninformed design first.
  3. **medium/hard** — the spread widens with SF; plan feedback (green ◇) is the p99
     lever, and the chart now measures that claim with equal n.
- Provenance: p50 computes from `results/agent_interaction.json` at render; p99 from
  `results/replay_p99.json`; `python scripts/check_chart_provenance.py` verifies both.
- The in-context trio (5 JSON · 6 blind · 7 CSV) appears only in its own set
  (`python scripts/plot_in_context.py` — the outcomes stack where the blind control IS
  the point), keeping regime and ablations out of the design ladder.

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

## 3 · The fallback's last rung — `figures/plan-hints-ab.svg`

**The sentence: give the agent the planner's steering wheel and it drives differently —
faster, and not more correct.**

- Condition 4b keeps everything from 4 and adds one thing: a refusal unlocks
  `engineer_query`, which probes a candidate (plan + the same 2 s elapsed budget)
  without committing, and planner hints (`USING INDEX / SCAN / JOIN`) are allowed.
  Hints are the one channel through which what the ontology knows about the data — the
  degree tail the planner's statistics miss by up to 4.6M× — reaches the execution plan.
- **Panel A** — 156 episodes at SF100 and SF1000. Latency falls hard (median episode
  96 s → 26 s at SF1000; median db hits 3.8M → 722k at SF100) and accuracy falls too
  (33→31, 23→18). The cost is 22 `max_turns_exceeded`: a new option to try is also a
  new way to spend the turn budget.
- **Panel B** — inside 4b, split by whether the settled query actually carried a hint:
  36/40 correct at a 4.6 s median where a hint landed, 13/38 at 80 s where none did.
  Say out loud that this is descriptive, not controlled — adoption concentrates on the
  anchored external questions where an index seek is the obvious steer.
- **The surprise worth telling**: the gate fired 93 times but only 13 episodes ever
  probed. The agent read the hint vocabulary in its rules and used it *before* being
  refused — 40 settled queries carry a real `USING` clause. Design consequence: the
  fallback's value here was the vocabulary, not the tool.
- One texture detail for the room: some probes tried `/*+ USING INDEX … */`, Oracle's
  hint syntax, which Cypher takes as a comment. Conventions travel across databases
  with the model.

---

## Backups (Q&A only — the repo commits the two charts above; regenerate the rest)

| Regenerate with | What you get | The question it defends |
|---|---|---|
| `python scripts/dump_conditions.py` | the conditions matrix | "exactly one thing differs between adjacent conditions" |
| `python scripts/plot_interaction.py` | replay-based p99 by difficulty/question | the model-free p99 for designs 1–4 (100 replays/query) |
| `python scripts/plot_overview.py --by-question` | the 13-panel per-question db-hits detail | any single question's scaling |
| `python scripts/plot_in_context.py` | outcomes (71 vs 11), per-question db hits for the trio | the causal claim about `more_available`, with its control |
| `results/in_context_deepseek.json` | the DeepSeek-V3.2 replication (234 episodes) | "is 71→11 model-general?" — no: DeepSeek pages regardless (11.6 trips blind) and burns the turn budget instead; the failure mode, not the field, is model-specific |
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
