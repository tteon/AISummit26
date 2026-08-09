#!/usr/bin/env python3
"""The trade-off page: latency and accuracy side by side, against scale, inside the SLO.

One grammar, two panels, shared x (SF1 / SF10 / SF100). Left: the slowest question's
replayed p99 per design (log y) with the 1 s request-p99 SLO as a horizontal line —
worst-question rather than an average, because an SLO breaks on the worst path, and the
geometric mean over 13 questions never crosses 1 s at all. Right: the share of the
cell's 39 live episodes matching gold. Reading the trade-off is comparing the two
panels at the same x: labels-only breaches the SLO at SF10 *and* drops to 77% there;
the informed designs hold both lines until SF100, where every design breaches — the
chain delays the breach by an order of scale (and narrows it, 7.6 s vs 3.3 s), it does
not repeal it.

gpt-oss-120b only; the model facet lives in AIEngineerNY26 figures/tradeoff-onepage.svg.

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

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.6, 4.5))
    fig.subplots_adjust(left=0.075, right=0.985, top=0.72, bottom=0.12, wspace=0.24)

    for arm in CHAIN:
        lat, acc = [], []
        for sf in SFS:
            vals = [max(float(c["client_p99"]), 0.5) for c in cells
                    if c["arm"] == arm and c["sf"] == sf and c.get("ok")]
            cell = [e for e in eps if e["arm"] == arm and e["sf"] == sf]
            lat.append(max(vals))
            acc.append(sum(1 for e in cell if e["score_correct"]) / len(cell))
        for ax, ys in ((axL, lat), (axR, acc)):
            ax.plot(range(3), ys, "-", color=ARM_COLOR[arm], linewidth=1.6, zorder=2)
            ax.plot(range(3), ys, marker=ARM_MARKER[arm], markersize=6,
                    markerfacecolor=ARM_COLOR[arm], markeredgecolor="white",
                    markeredgewidth=1.1, linestyle="", zorder=3)

    axL.axhline(SLO_MS, color="#b91c1c", linewidth=1.1, linestyle=(0, (5, 4)),
                zorder=1, alpha=0.75)
    axL.annotate("1 s — request p99 SLO", xy=(0.02, SLO_MS),
                 xycoords=("axes fraction", "data"), fontsize=7.8, color="#b91c1c",
                 va="bottom", ha="left")
    axL.set_yscale("log")
    axL.set_title("Slowest question's replayed p99", fontsize=10, weight="bold",
                  color=INK, loc="left", pad=6)
    axL.set_ylabel("ms (log) — 100 model-free runs each", fontsize=8.4, color=MUTED)
    axR.set_title("Accuracy — episodes matching gold", fontsize=10, weight="bold",
                  color=INK, loc="left", pad=6)
    axR.set_ylim(0.70, 1.01)
    axR.set_yticks([0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0])
    axR.set_yticklabels(["70%", "75%", "80%", "85%", "90%", "95%", "100%"])
    axR.set_ylabel("share of the cell's 39 episodes", fontsize=8.4, color=MUTED)

    for ax in (axL, axR):
        ax.set_xticks(range(3))
        ax.set_xticklabels(["SF1", "SF10", "SF100"], fontsize=8.8)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0, labelsize=8)

    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=5.6, label=ARM_LABEL[a])
               for a in CHAIN]
    fig.legend(handles=handles, frameon=False, fontsize=8.4, ncol=4,
               loc="upper left", bbox_to_anchor=(0.06, 0.845))
    fig.suptitle("Latency and accuracy against scale, inside the SLO — the contract "
                 "chain", fontsize=13, weight="bold", x=0.03, ha="left", y=0.96,
                 color=INK)
    fig.text(0.03, 0.885,
             "Same designs, both panels: read the trade-off at a fixed scale. Left is "
             "the tail an SLO actually breaks on (the slowest question); right is live "
             "accuracy. gpt-oss-120b only.",
             fontsize=8.2, color=MUTED, ha="left", va="top")
    Path(args.out).parent.mkdir(exist_ok=True)
    fig.savefig(args.out)
    fig.savefig("/tmp/claude-1000/-home-hadry-lab-AIsummit26/e438afef-9ffb-42c3-ae19-f7273ba469ed/scratchpad/slo-tradeoff.png", dpi=110)
    plt.close(fig)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
