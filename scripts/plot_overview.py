#!/usr/bin/env python3
"""THE chart: every question, every scale, every design, labelled — one figure.

Thirteen panels (one per question), SF across, median db hits per episode up (log), one
line per agent design across all seven conditions. Marker fill carries correctness: filled
= all three repeats matched gold, hollow = none did, grey = some. Db hits rather than
milliseconds because it is the one cost unit unaffected by what else runs on the box.

This is the deck's overview chart — the whole 819-episode experiment in one image — next
to figures/engineering-detail.svg, which is the layer underneath it.

Usage:
  python scripts/plot_overview.py --episodes results/agent_interaction.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Seven designs, colours validated as a set (guardrail moved off teal so plan's green
# stays separable); marker shape is the secondary encoding the 6-8 CVD band asks for.
ARMS = ["labels", "ontology", "guardrail", "plan", "in_context", "in_context_blind",
        "in_context_csv"]
ARM_LABEL = {"labels": "1 · labels only", "ontology": "2 · + ontology",
             "guardrail": "3 · + guardrail", "plan": "4 · + plan feedback",
             "in_context": "5 · in-context (JSON)", "in_context_blind": "6 · blind control",
             "in_context_csv": "7 · in-context (CSV)"}
ARM_COLOR = {"labels": "#c2410c", "ontology": "#ca8a04", "guardrail": "#2a78d6",
             "plan": "#15803d", "in_context": "#6d28d9", "in_context_blind": "#9f7aea",
             "in_context_csv": "#be185d"}
ARM_MARKER = {"labels": "o", "ontology": "s", "guardrail": "^", "plan": "D",
              "in_context": "v", "in_context_blind": "P", "in_context_csv": "X"}
QIDS = ["ext_easy_1", "ext_easy_2", "ext_med_1", "ext_med_2", "ext_hard_1", "ext_hard_2",
        "int_easy_1", "int_easy_2", "int_med_1", "int_med_2", "int_hard_1", "int_hard_1b",
        "int_hard_2"]
SFS = [1, 10, 100]
INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "figure.facecolor": "white", "savefig.facecolor": "white",
})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/agent_interaction.json")
    p.add_argument("--out", default="figures/overview-by-question.svg")
    args = p.parse_args()
    eps = json.loads(Path(args.episodes).read_text())["episodes"]

    fig, axes = plt.subplots(4, 4, figsize=(12.6, 10.4), sharex=True)
    fig.subplots_adjust(left=0.06, right=0.985, top=0.845, bottom=0.045,
                        hspace=0.44, wspace=0.30)
    flat = axes.flatten()
    for ax in flat[len(QIDS):]:
        ax.set_visible(False)

    for ax, qid in zip(flat, QIDS):
        for arm in ARMS:
            xs, ys, fills = [], [], []
            for i, sf in enumerate(SFS):
                sub = [e for e in eps if e["arm"] == arm and e["sf"] == sf
                       and e["question_id"] == qid]
                if not sub:
                    continue
                xs.append(i)
                ys.append(statistics.median([max(e.get("db_hits") or 1, 1) for e in sub]))
                fills.append(sum(1 for e in sub if e.get("score_correct")) / len(sub))
            ax.plot(xs, ys, "-", color=ARM_COLOR[arm], linewidth=1.2, alpha=0.9, zorder=2)
            for x, y, ok in zip(xs, ys, fills):
                face = (ARM_COLOR[arm] if ok >= 1.0
                        else "white" if ok <= 0.0 else "#c3c8d2")
                ax.plot([x], [y], marker=ARM_MARKER[arm], markersize=4.6,
                        markerfacecolor=face, markeredgecolor=ARM_COLOR[arm],
                        markeredgewidth=1.0, linestyle="", zorder=3)
        ax.set_yscale("log")
        ax.set_xticks(range(len(SFS)))
        ax.set_xticklabels(["SF1", "SF10", "SF100"], fontsize=7.6)
        ax.tick_params(axis="y", labelsize=7)
        ax.set_title(qid, fontsize=8.6, color=INK, loc="left", pad=4)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0)

    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=5.6, label=ARM_LABEL[a])
               for a in ARMS]
    fig.legend(handles=handles, frameon=False, fontsize=8.6, ncol=4,
               loc="upper left", bbox_to_anchor=(0.025, 0.917))
    fig.suptitle("Every question, every scale, every design — median db hits per episode",
                 fontsize=14.5, weight="bold", x=0.028, ha="left", y=0.978, color=INK)
    fig.text(0.028, 0.955,
             "819 episodes, three repeats per point (log scale). Marker fill: filled = all "
             "repeats matched gold, hollow = none, grey = some.\nDesigns 1–4 let the "
             "database aggregate; 5–7 pull the rows into context. Db hits, not milliseconds "
             "— the one cost unit\nunaffected by what else runs on the box.",
             fontsize=8.8, color=MUTED, ha="left", va="top")
    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
