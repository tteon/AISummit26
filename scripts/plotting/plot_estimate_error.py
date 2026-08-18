#!/usr/bin/env python3
"""Why the gate is drawn on elapsed time: the planner's row estimate against reality.

Every call in the `plan` arm runs EXPLAIN before it executes, so the planner's
`EstimatedRows` and the measured `DbHits` of the same query are both on the record. This
plots one against the other, log-log, with the identity line: a planner whose estimates
tracked reality would put its points along it.

It is the local reproduction of a diagnosis from the graph-database literature — Neo4j's
optimizer draws on low-order statistics (per-type counts) under an edge-independence
assumption, which under-counts exactly the multi-hop patterns that matter (Lyu et al.,
*Enhancing Neo4j Query Efficiency with Seamless Integration of the GOpt Optimization
Framework*, VLDB 2024 LSGDA). On this graph the ratio runs from 3× to 1,067,333×, and the
worst case is the planner estimating a single row for a query that goes on to touch a
million db hits. That is the whole argument for gating on the probe's elapsed time
instead: a budget drawn on this estimator would pass everything or block everything.

  python scripts/plot_estimate_error.py   # figures/estimate-error.svg
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# difficulty is ordered, so it gets one hue in three steps rather than three hues
DIFF_COLOR = {"easy": "#93c5fd", "medium": "#3b82f6", "hard": "#1e3a8a"}
DIFFS = ["easy", "medium", "hard"]
INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "figure.facecolor": "white", "savefig.facecolor": "white",
})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/agent_interaction.json")
    p.add_argument("--out", default="figures/estimate-error.svg")
    args = p.parse_args()

    eps = json.loads(Path(args.episodes).read_text())["episodes"]
    pts = []
    for e in eps:
        if e["arm"] != "plan":
            continue
        for c in e.get("calls", []):
            est, hits = c.get("estimated_rows"), c.get("db_hits")
            if est and hits and est > 0 and hits > 0:
                pts.append((float(est), float(hits), e["difficulty"], e["sf"],
                            e["question_id"], c["outcome"]))

    fig, ax = plt.subplots(figsize=(8.8, 6.2))
    fig.subplots_adjust(left=0.10, right=0.978, top=0.775, bottom=0.10)

    lo, hi = 0.5, 5e7
    ax.plot([lo, hi], [lo, hi], color=MUTED, linewidth=1.1, linestyle=(0, (5, 4)),
            zorder=1, alpha=0.8)
    for mult, label in ((1e3, "1,000×"), (1e6, "1,000,000×")):
        ax.plot([lo, hi / mult], [lo * mult, hi], color=GRID, linewidth=1.0,
                linestyle=(0, (2, 3)), zorder=1)
        ax.annotate(label, xy=(hi / mult, hi), fontsize=7.2, color=MUTED,
                    ha="right", va="top", xytext=(-2, -3), textcoords="offset points")
    ax.annotate("perfect estimate", xy=(3e5, 3e5), fontsize=7.6, color=MUTED,
                ha="right", va="top", xytext=(-2, -4), textcoords="offset points",
                rotation=38)

    for diff in DIFFS:
        sub = [q for q in pts if q[2] == diff]
        ax.scatter([q[0] for q in sub], [q[1] for q in sub], s=42,
                   facecolor=DIFF_COLOR[diff], edgecolor="white", linewidth=0.8,
                   alpha=0.9, zorder=3, label=f"{diff}  (n={len(sub)})")

    worst = max(pts, key=lambda q: q[1] / q[0])
    ax.annotate(f"{worst[4]} at SF{worst[3]}\nestimate {worst[0]:,.0f} row → "
                f"{worst[1]:,.0f} db hits\n({worst[1]/worst[0]:,.0f}×)",
                xy=(worst[0], worst[1]), xytext=(26, -6), textcoords="offset points",
                fontsize=8, color=INK, va="center",
                arrowprops=dict(arrowstyle="-", color=MUTED, linewidth=0.9))

    ratios = sorted(q[1] / q[0] for q in pts)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lo, 1e6)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("planner's EstimatedRows for the query (log)", fontsize=9,
                  color=MUTED)
    ax.set_ylabel("measured db hits when it ran (log)", fontsize=9, color=MUTED)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, labelsize=8)
    ax.legend(frameon=False, fontsize=8.4, loc="lower right",
              title="question difficulty", title_fontsize=8.4)

    fig.suptitle("The planner's estimate against what the query actually cost",
                 fontsize=13.5, weight="bold", x=0.028, ha="left", y=0.965, color=INK)
    fig.text(0.028, 0.905,
             f"Every EXPLAIN in the plan arm, paired with the db hits the same query went "
             f"on to spend ({len(pts)} calls).\n"
             f"Ratio actual/estimated: median {ratios[len(ratios)//2]:,.0f}×, worst "
             f"{ratios[-1]:,.0f}×. Points above the diagonal are under-estimates.\n"
             "This is why the gate is drawn on the probe's elapsed time: a budget written "
             "against this axis passes everything or blocks everything.",
             fontsize=8.3, color=MUTED, ha="left", va="top")
    Path(args.out).parent.mkdir(exist_ok=True)
    fig.savefig(args.out)
    fig.savefig("/tmp/claude-1000/-home-hadry-lab-AIsummit26/e438afef-9ffb-42c3-ae19-f7273ba469ed/scratchpad/estimate-error.png", dpi=110)
    plt.close(fig)
    print(f"wrote {args.out}  ({len(pts)} calls, median ratio "
          f"{ratios[len(ratios)//2]:,.0f}x, worst {ratios[-1]:,.0f}x)")


if __name__ == "__main__":
    main()
