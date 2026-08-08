#!/usr/bin/env python3
"""The trade-off, on one canvas: accuracy against tail latency, inside the SLO.

The overview pair shows each axis separately; this chart is where they meet. One point
per (design, scale): y is the share of the cell's 39 episodes that matched gold, x is the
**slowest question's** replayed p99 (client-observed, 100 model-free executions, first
discarded — same source as the overview p99 chart). Worst-question, not an average,
because an SLO breaks on the worst path: the geometric mean over 13 questions never
crosses 1 s and would report a safety that production does not have. The dashed vertical
is the 1 s request-p99 SLO. Lines connect SF1 → SF10 → SF100 per design: scale drags
every design rightward; the chain delays the SLO breach by an order of scale (labels
exits at SF10, the informed designs at SF100) and narrows it (7.6 s vs 3.3 s) — it does
not repeal it, which is the hand-off to the data-plane chart.

gpt-oss-120b only — the chain arms were measured on one model family; the model-facet
companion (AIEngineerNY26 figures/model-tradeoff.svg) carries the second family.

  python scripts/plot_tradeoff.py   # figures/slo-tradeoff.svg
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

CHAIN = ["labels", "ontology", "guardrail", "plan"]
ARM_LABEL = {"labels": "1 · labels only", "ontology": "2 · + ontology",
             "guardrail": "3 · + guardrail", "plan": "4 · + plan feedback"}
ARM_COLOR = {"labels": "#c2410c", "ontology": "#ca8a04", "guardrail": "#2a78d6",
             "plan": "#15803d"}                     # the deck's validated set
ARM_MARKER = {"labels": "o", "ontology": "s", "guardrail": "^", "plan": "D"}
SFS = [1, 10, 100]
INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"
SLO_MS = 1000.0

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "figure.facecolor": "white", "savefig.facecolor": "white",
})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/agent_interaction.json")
    p.add_argument("--replay", default="results/replay_p99.json")
    p.add_argument("--out", default="figures/slo-tradeoff.svg")
    args = p.parse_args()

    eps = json.loads(Path(args.episodes).read_text())["episodes"]
    cells = json.loads(Path(args.replay).read_text())["cells"]

    fig, ax = plt.subplots(figsize=(8.6, 5.4))
    fig.subplots_adjust(left=0.09, right=0.975, top=0.775, bottom=0.11)

    ax.axvline(SLO_MS, color="#b91c1c", linewidth=1.1, linestyle=(0, (5, 4)),
               zorder=1, alpha=0.75)
    ax.annotate("1 s — request p99 SLO", xy=(SLO_MS, 0.735), fontsize=7.8,
                color="#b91c1c", ha="right", va="bottom", rotation=90,
                xytext=(-4, 0), textcoords="offset points")

    for arm in CHAIN:
        xs, ys = [], []
        for sf in SFS:
            vals = [max(float(c["client_p99"]), 0.5) for c in cells
                    if c["arm"] == arm and c["sf"] == sf and c.get("ok")]
            cell = [e for e in eps if e["arm"] == arm and e["sf"] == sf]
            if not vals or not cell:
                continue
            xs.append(max(vals))
            ys.append(sum(1 for e in cell if e["score_correct"]) / len(cell))
        ax.plot(xs, ys, "-", color=ARM_COLOR[arm], linewidth=1.4, alpha=0.85, zorder=2)
        for (x, y, sf, size) in zip(xs, ys, SFS, (5.2, 6.8, 8.6)):
            ax.plot([x], [y], marker=ARM_MARKER[arm], markersize=size,
                    markerfacecolor=ARM_COLOR[arm], markeredgecolor="white",
                    markeredgewidth=1.2, linestyle="", zorder=3)
        if arm == "labels":
            # the zigzag needs naming: SF10 sits RIGHT of SF100 for this arm
            ax.annotate("SF10", xy=(xs[1], ys[1]), xytext=(6, -12),
                        textcoords="offset points", fontsize=7.4, color=MUTED)
            ax.annotate("SF100", xy=(xs[2], ys[2]), xytext=(6, 8),
                        textcoords="offset points", fontsize=7.4, color=MUTED)
        # selective direct labels: only the extremes; the legend carries the rest
        # (ontology and guardrail settle on nearly identical queries, so their SF100
        # points overlap by construction)
        if arm == "labels":
            ax.annotate(ARM_LABEL[arm], xy=(xs[-1], ys[-1]),
                        xytext=(10, 20), textcoords="offset points",
                        fontsize=8.4, color=ARM_COLOR[arm], va="center", weight="bold")
        if arm == "plan":
            ax.annotate(ARM_LABEL[arm], xy=(xs[-1], ys[-1]),
                        xytext=(-14, -20), textcoords="offset points", ha="right",
                        fontsize=8.4, color=ARM_COLOR[arm], va="center", weight="bold")

    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=5.6, label=ARM_LABEL[a])
               for a in CHAIN]
    ax.legend(handles=handles, frameon=False, fontsize=8.2, loc="lower left")
    ax.set_xscale("log")
    ax.set_xlim(15, 40000)
    ax.set_ylim(0.72, 1.005)
    ax.set_xlabel("slowest question's replayed p99 (ms, log)",
                  fontsize=8.6, color=MUTED)
    ax.set_ylabel("episodes matching gold (share of 39 per cell)", fontsize=8.6, color=MUTED)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, labelsize=8)
    ax.set_yticks([0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0])
    ax.set_yticklabels(["70%", "75%", "80%", "85%", "90%", "95%", "100%"])

    fig.suptitle("Accuracy against tail latency, inside the SLO — the contract chain, "
                 "SF1 → SF100", fontsize=13, weight="bold", x=0.03, ha="left", y=0.965,
                 color=INK)
    fig.text(0.03, 0.905,
             "One trajectory per design; marker grows with scale (SF1 · SF10 · SF100).\n"
             "y: share of the cell's 39 live episodes matching gold. x: the slowest "
             "question's replayed p99 (100 model-free runs) — an SLO breaks on the\n"
             "worst path, not the average. Designs 2 and 3 overlap at SF100 (nearly "
             "identical settled queries). gpt-oss-120b only.",
             fontsize=8.0, color=MUTED, ha="left", va="top")
    Path(args.out).parent.mkdir(exist_ok=True)
    fig.savefig(args.out)
    fig.savefig("/tmp/claude-1000/-home-hadry-lab-AIsummit26/e438afef-9ffb-42c3-ae19-f7273ba469ed/scratchpad/slo-tradeoff.png", dpi=110)
    plt.close(fig)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
