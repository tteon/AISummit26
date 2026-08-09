#!/usr/bin/env python3
"""The trade-off page: latency and accuracy against scale, by difficulty — the chain.

The overview's grammar, completed: three difficulty columns, SF on x, and now two rows
so both halves of the trade-off are on one page. Top: the slowest question's replayed
p99 in the difficulty (log y) with the 1 s request-p99 SLO as a horizontal line —
worst-question, because an SLO breaks on the worst path, not the average. Bottom: the
share of the difficulty's live episodes matching gold (easy 4 · medium 4 · hard 5
questions, ×3 repeats). Reading it is one motion: pick a column, look down.

All data from this repo's episodes and stage-two replays; gpt-oss-120b (the chain arms
were measured on one model family — a DeepSeek chain run would add a second line style
per arm, same grammar).

  python scripts/plot_tradeoff.py   # figures/slo-tradeoff.svg
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

CHAIN = ["labels", "ontology", "guardrail", "plan"]
ARM_LABEL = {"labels": "1 · labels only", "ontology": "2 · + ontology",
             "guardrail": "3 · + guardrail", "plan": "4 · + plan feedback"}
ARM_COLOR = {"labels": "#c2410c", "ontology": "#ca8a04", "guardrail": "#2a78d6",
             "plan": "#15803d"}
ARM_MARKER = {"labels": "o", "ontology": "s", "guardrail": "^", "plan": "D"}
DIFFS = ["easy", "medium", "hard"]
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

    fig, axes = plt.subplots(2, 3, figsize=(11.8, 7.0), sharex=True)
    fig.subplots_adjust(left=0.075, right=0.985, top=0.815, bottom=0.065,
                        hspace=0.16, wspace=0.22)

    for col, diff in enumerate(DIFFS):
        axT, axB = axes[0][col], axes[1][col]
        for arm in CHAIN:
            lat, acc = [], []
            for sf in SFS:
                vals = [max(float(c["client_p99"]), 0.5) for c in cells
                        if c["arm"] == arm and c["sf"] == sf
                        and c["difficulty"] == diff and c.get("ok")]
                cell = [e for e in eps if e["arm"] == arm and e["sf"] == sf
                        and e["difficulty"] == diff]
                lat.append(max(vals))
                acc.append(sum(1 for e in cell if e["score_correct"]) / len(cell))
            for ax, ys in ((axT, lat), (axB, acc)):
                ax.plot(range(3), ys, "-", color=ARM_COLOR[arm], linewidth=1.6,
                        zorder=2)
                ax.plot(range(3), ys, marker=ARM_MARKER[arm], markersize=5.8,
                        markerfacecolor=ARM_COLOR[arm], markeredgecolor="white",
                        markeredgewidth=1.0, linestyle="", zorder=3)
        axT.axhline(SLO_MS, color="#b91c1c", linewidth=1.0, linestyle=(0, (5, 4)),
                    zorder=1, alpha=0.75)
        axT.set_yscale("log")
        axT.set_ylim(0.4, 80000)
        axT.set_title(diff, fontsize=11.5, weight="bold", color=INK, loc="left",
                      pad=6)
        axB.set_ylim(0, 1.05)
        axB.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
        axB.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
        for ax in (axT, axB):
            ax.set_xticks(range(3))
            ax.set_xticklabels(["SF1", "SF10", "SF100"], fontsize=8.6)
            ax.grid(axis="y", color=GRID, linewidth=0.6)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            ax.tick_params(length=0, labelsize=7.8)

    axes[0][0].set_ylabel("slowest question's replayed p99 (ms, log)",
                          fontsize=8.4, color=MUTED)
    axes[1][0].set_ylabel("episodes matching gold", fontsize=8.4, color=MUTED)
    axes[0][0].annotate("1 s — request p99 SLO", xy=(0.03, SLO_MS),
                        xycoords=("axes fraction", "data"), fontsize=7.4,
                        color="#b91c1c", va="bottom", ha="left")

    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=5.6, label=ARM_LABEL[a])
               for a in CHAIN]
    fig.legend(handles=handles, frameon=False, fontsize=8.6, ncol=4,
               loc="upper left", bbox_to_anchor=(0.06, 0.89))
    fig.suptitle("Latency and accuracy against scale, by difficulty — the contract "
                 "chain, inside the SLO", fontsize=13.5, weight="bold", x=0.03,
                 ha="left", y=0.97, color=INK)
    fig.text(0.03, 0.915,
             "Top: the slowest question's replayed p99 in the difficulty (100 "
             "model-free runs; an SLO breaks on the worst path).\n"
             "Bottom: share of the difficulty's live episodes matching gold "
             "(easy/medium 12, hard 15 per cell). gpt-oss-120b.",
             fontsize=8.4, color=MUTED, ha="left", va="top")
    Path(args.out).parent.mkdir(exist_ok=True)
    fig.savefig(args.out)
    fig.savefig("/tmp/claude-1000/-home-hadry-lab-AIsummit26/e438afef-9ffb-42c3-ae19-f7273ba469ed/scratchpad/slo-tradeoff.png", dpi=100)
    plt.close(fig)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
