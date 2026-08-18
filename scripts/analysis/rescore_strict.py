#!/usr/bin/env python3
"""Re-score the run under a stricter rule, and measure how plausible the failures look.

Two things the published scoring does not do, both fixable without re-running anything:
the answers and the gold are on the record, so this is arithmetic over a JSON file.

1. **Strict scalar binding.** The run's `score()` flattens every number an answer carries
   and marks a key matched if *any* of them lands within tolerance. An answer holding
   eight numbers therefore has eight chances at each key. Strict scoring binds by
   position instead: the answer's scalars in order against the question's gold keys in
   order (models rename keys — `transfer_count` for `n` — so name matching is useless and
   order is what survives). Both verdicts are reported, so the leniency is quantified
   rather than argued about.

2. **How wrong is a wrong answer.** A list answer already carries F1; a scalar one
   carries nothing, so this adds relative error — the scalar analogue. Failures then
   classify: *plausible* (a list overlapping the gold at F1 >= 0.5, or a scalar within
   10% of it), *clearly wrong* (neither), *no answer* (the model never produced one).
   The distinction is the point: an answer that looks right and is not is a different
   operational problem from an empty result, and in an AML setting a much worse one.

  python scripts/rescore_strict.py   # -> results/analysis/rescore_strict.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_interaction import QUESTIONS, parse_answer, _flatten_scalars  # noqa: E402
from runmeta import manifest  # noqa: E402

PLAUSIBLE_F1 = 0.5
PLAUSIBLE_REL_ERR = 0.10
QMETA = {q["id"]: q for q in QUESTIONS}


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("₩", "").strip())
        except ValueError:
            return None
    return None


def strict_scalar(question, gold: dict, answer):
    """Bind the answer's scalars to the gold keys by position, and score only those.

    Returns (correct, matched, n_keys, worst_rel_err). A key whose gold value is missing
    counts as matched — the run could not compute it, so the episode cannot be blamed.
    """
    keys = question["keys"]
    vals = [v for v in (_num(x) for x in _flatten_scalars(answer)) if v is not None]
    matched, worst = 0, 0.0
    for i, key in enumerate(keys):
        want = gold.get(key)
        if want is None:
            matched += 1
            continue
        want = float(want)
        if i >= len(vals):
            worst = max(worst, 1.0)
            continue
        got = vals[i]
        tol = max(abs(want) * 0.001, 0.5)
        err = abs(got - want) / abs(want) if want else (0.0 if got == want else 1.0)
        worst = max(worst, err)
        if abs(got - want) <= tol:
            matched += 1
    return matched == len(keys), matched, len(keys), worst


def list_f1(gold_list, answer):
    gold_set = {str(v) for v in gold_list}
    got = {str(v) for v in _flatten_scalars(answer) if v is not None}
    if not gold_set:
        return (1.0 if not got else 0.0), (1.0 if not got else 0.0)
    tp = len(gold_set & got)
    prec = tp / len(got) if got else 0.0
    rec = tp / len(gold_set)
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return f1, rec


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/episodes/agent_interaction.json")
    p.add_argument("--out", default="results/analysis/rescore_strict.json")
    args = p.parse_args()

    eps = json.loads(Path(args.episodes).read_text())["episodes"]
    rows = []
    for e in eps:
        q = QMETA.get(e["question_id"])
        answer, parse_note = parse_answer(e.get("final_output") or "")
        rec = {k: e[k] for k in ("arm", "sf", "question_id", "repeat", "difficulty",
                                 "audience")}
        rec["published_correct"] = e["score_correct"]
        rec["published_f1"] = e.get("score_f1")
        rec["parse"] = parse_note

        if e.get("error") or answer is None:
            rec.update(strict_correct=False, plausibility=None, klass="no answer")
            rows.append(rec)
            continue

        gold = e.get("score_gold")
        if gold is None:
            rec.update(strict_correct=False, plausibility=None, klass="no gold")
            rows.append(rec)
            continue

        if q["shape"] == "scalar":
            ok, matched, n, worst = strict_scalar(q, gold, answer)
            rec.update(strict_correct=ok, matched_keys=matched, n_keys=n,
                       rel_err=round(worst, 6),
                       plausibility=round(max(0.0, 1.0 - worst), 4))
            plaus = worst <= PLAUSIBLE_REL_ERR
        else:
            f1, rec_score = list_f1(gold, answer)
            ok = rec_score == 1.0
            rec.update(strict_correct=ok, f1=round(f1, 4),
                       recall=round(rec_score, 4), plausibility=round(f1, 4))
            plaus = f1 >= PLAUSIBLE_F1

        rec["klass"] = ("correct" if ok else
                        "plausible wrong" if plaus else "clearly wrong")
        rows.append(rec)

    out = {
        "schema_version": "seocho.finbench.rescore-strict.v1",
        "manifest": manifest(
            rescoring="strict positional scalar binding + failure plausibility",
            source=args.episodes, plausible_f1=PLAUSIBLE_F1,
            plausible_rel_err=PLAUSIBLE_REL_ERR),
        "episodes": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=str))

    chain = [r for r in rows if r["arm"] in
             ("labels", "ontology", "guardrail", "plan")]
    print(f"chain episodes: {len(chain)}")
    print(f"{'arm':11s} {'published':>10s} {'strict':>8s} {'plausible':>10s} "
          f"{'clearly':>8s} {'no answer':>10s}")
    for arm in ("labels", "ontology", "guardrail", "plan"):
        r = [x for x in chain if x["arm"] == arm]
        print(f"{arm:11s} {sum(x['published_correct'] for x in r):>7d}/{len(r):<3d}"
              f" {sum(x['strict_correct'] for x in r):>5d}/{len(r):<3d}"
              f" {sum(1 for x in r if x['klass'] == 'plausible wrong'):>10d}"
              f" {sum(1 for x in r if x['klass'] == 'clearly wrong'):>8d}"
              f" {sum(1 for x in r if x['klass'] == 'no answer'):>10d}")
    flips = [x for x in chain if x["published_correct"] and not x["strict_correct"]]
    print(f"\nepisodes the published rule marked correct and strict binding does not: "
          f"{len(flips)}")
    for x in flips[:12]:
        print(f"  {x['arm']:10s} {x['question_id']:12s} SF{x['sf']:<4} "
              f"rel_err={x.get('rel_err')}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
