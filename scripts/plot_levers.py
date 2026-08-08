#!/usr/bin/env python3
"""The closing figure: every lever measured in this repo, grouped by what kind of fix it is.

Two blocks, not one sorted list — the grouping IS the argument. Within a block, rows are
comparable (same kind of intervention); across blocks they are not, and the layout says so
without a disclaimer. Each row names the metric it moved.

Sources: results/agent_interaction.json (conditions 1-7), results/bench_*_20260808.txt and
results/bench/*.json. Usage:  python scripts/plot_levers.py
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"
ONTOLOGY, ENGINEERING = "#6d28d9", "#eb6834"   # validated pair

GROUPS = [
    ("ONTOLOGY / CONTRACT — change what the model is told or must write", ONTOLOGY, [
        ("LIMIT required in every query", "runaway fetch  398 → 1.4 ms", 276.0),
        ("plan feedback on a slow query", "ext-hard p99, ≈ order of magnitude", 10.0),
        ("ontology in the prompt", "ext_med_1 correct  1/9 → 9/9", 9.0),
        ("`more_available` in the tool reply", "silent truncation failures  71 → 11", 6.5),
    ]),
    ("ENGINEERING / RUNTIME — change what executes underneath", ENGINEERING, [
        ("native driver (neo4rs, tokio)", "8-worker tool-call p50  769 → 7.7 ms", 100.0),
        ("process per worker", "8-worker tool-call p50  769 → 81 ms", 9.5),
        ("CSV rows instead of JSON", "context at SF100  11.0k → 3.7k tokens", 3.0),
        ("rust-ext codec", "client CPU  20.8 → 15.5 µs/row", 1.3),
    ]),
]


def main(out: str = "figures/levers.svg") -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9,
        "figure.facecolor": "white", "savefig.facecolor": "white",
    })
    fig, ax = plt.subplots(figsize=(9.4, 4.9))
    fig.subplots_adjust(left=0.295, right=0.90, top=0.87, bottom=0.09)

    y = 0.0
    yticks = []
    for title, color, rows in GROUPS:
        ax.text(0.9, y + 0.72, title, fontsize=9.2, weight="bold", color=color,
                ha="left", va="center", clip_on=False)
        for label, metric, ratio in rows:
            ax.hlines(y, 0.9, ratio, color=GRID, linewidth=1.4, zorder=1)
            ax.plot([ratio], [y], "o", markersize=9.5, color=color, zorder=3)
            ax.text(ratio * 1.18, y, f"×{ratio:g}", va="center", fontsize=9.2,
                    color=INK, weight="bold")
            ax.text(0.82, y + 0.13, label, va="center", ha="right", fontsize=8.8, color=INK)
            ax.text(0.82, y - 0.24, metric, va="center", ha="right", fontsize=7.3,
                    color=MUTED)
            yticks.append(y)
            y -= 1.0
        y -= 1.05   # gap between the two blocks

    ax.set_xscale("log")
    ax.set_xlim(0.9, 900)
    ax.set_xticks([1, 10, 100])
    ax.set_xticklabels(["×1", "×10", "×100 better"], fontsize=8.4, color=MUTED)
    ax.set_yticks([])
    ax.set_ylim(y + 0.4, 1.35)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    fig.suptitle("Eight levers, two kinds of fix — each against its own metric",
                 fontsize=13, weight="bold", x=0.028, ha="left", y=0.965, color=INK)
    fig.text(0.028, 0.90, "Compare within a block, not across blocks.",
             fontsize=8.6, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
