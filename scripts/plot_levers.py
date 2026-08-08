#!/usr/bin/env python3
"""The closing figure: every lever measured in this repo, on one scale, wearing its label.

One row per intervention, position = how far it moved its own primary metric (log scale,
expressed as ×improvement), color = which kind of fix it is:

  ontology / contract   what the model is told, promised, or required to write
  engineering / runtime  what executes underneath, model untouched

Each row names its metric — the ratios are NOT comparable as a ranking across rows (they
move different quantities), which the subtitle says out loud. What IS comparable is the
label: the fixes that changed what the agent *does* are contract-side; the fixes that
changed what the exchange *costs* are runtime-side, and neither substitutes for the other.

Sources: results/agent_interaction.json (conditions 1-7),
results/bench_*_20260808.txt and results/bench/*.json (transport, decoder, processes,
neo4rs). Usage:  python scripts/plot_levers.py
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"
ONTOLOGY, ENGINEERING = "#6d28d9", "#eb6834"   # validated pair

# (label, metric it moved, ×improvement, kind)
LEVERS = [
    ("LIMIT as a query contract", "runaway fetch 397.6 → 1.4 ms", 276.0, ONTOLOGY),
    ("ontology in the prompt", "ext_med_1 correct 1/9 → 9/9", 9.0, ONTOLOGY),
    ("plan feedback to the model", "ext-hard p99, ≈ order of magnitude", 10.0, ONTOLOGY),
    ("`more_available` + two sentences", "silent truncation failures 71 → 11", 6.5, ONTOLOGY),
    ("CSV rows instead of JSON", "context at SF100, 11.0k → 3.7k tokens", 3.0, ENGINEERING),
    ("native driver (neo4rs, tokio)", "8-worker tool-call p50 769 → 7.7 ms", 100.0, ENGINEERING),
    ("process per worker", "8-worker tool-call p50 769 → 81 ms", 9.5, ENGINEERING),
    ("rust-ext codec", "client CPU 20.8 → 15.5 µs/row", 1.3, ENGINEERING),
]


def main(out: str = "figures/levers.svg") -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9,
        "figure.facecolor": "white", "savefig.facecolor": "white",
    })
    rows = sorted(LEVERS, key=lambda r: r[2])
    fig, ax = plt.subplots(figsize=(9.4, 4.4))
    fig.subplots_adjust(left=0.30, right=0.87, top=0.77, bottom=0.11)
    for y, (label, metric, ratio, color) in enumerate(rows, start=1):
        ax.hlines(y, 0.9, ratio, color=GRID, linewidth=1.4, zorder=1)
        ax.plot([ratio], [y], "o", markersize=9.5, color=color, zorder=3)
        ax.text(ratio * 1.18, y, f"×{ratio:g}", va="center", fontsize=9,
                color=INK, weight="bold")
        ax.text(0.82, y, label, va="center", ha="right", fontsize=8.8, color=INK)
        ax.text(0.82, y - 0.34, metric, va="center", ha="right", fontsize=7.2, color=MUTED)
    ax.set_xscale("log")
    ax.set_xlim(0.9, 900)
    ax.set_xticks([1, 10, 100])
    ax.set_xticklabels(["×1", "×10", "×100"], fontsize=8.4, color=MUTED)
    ax.set_yticks([])
    ax.set_ylim(0.4, len(rows) + 0.6)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    handles = [plt.Line2D([], [], marker="o", linestyle="", markersize=8, color=c)
               for c in (ONTOLOGY, ENGINEERING)]
    ax.legend(handles, ["ontology / contract — what the model is told or must write",
                        "engineering / runtime — what executes underneath"],
              frameon=False, fontsize=8, loc="lower right")
    fig.suptitle("Every lever we measured, on one scale — labelled by what kind of fix it is",
                 fontsize=12.5, weight="bold", x=0.028, ha="left", y=0.955, color=INK)
    fig.text(0.028, 0.865,
             "Each row moves its OWN metric (named under the label) — the positions are not "
             "a ranking across rows. What the labels say: the fixes that\nchanged what the "
             "agent does are contract-side; the fixes that changed what the exchange costs "
             "are runtime-side. Neither substitutes for the other:\nno driver makes a model "
             "disclose truncation, and no prompt makes a row cost less than 346 bytes in a "
             "Python dict.",
             fontsize=8.3, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
