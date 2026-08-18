#!/usr/bin/env python3
"""One page, three difficulties: latency and accuracy against scale, on twin axes.

Every dimension of the experiment on a single canvas, per the deck's requirement: three
difficulty panels, SF1/SF10/SF100 across, and both halves of the trade-off inside each
panel — bars are correctness (right axis) and lines are latency (left axis, log ms) with
the 1 s request-p99 SLO as a horizontal rule. Each design keeps one x offset in a panel,
so a design's bar and its latency marker sit in the same vertical slot: read a column,
and the pair is the trade-off.

Correctness is drawn twice, and the gap between the two is the point. The **ghost bar**
is answer accuracy — did the reply carry the gold values — which saturates near full
marks and separates almost nothing. The **solid bar** is execution accuracy: the query
the design settled on, re-run and compared against the golden query's own result
(`scripts/rescore_execution.py`). Scoring the sentence says the four designs are the
same; scoring the Cypher says they are not.

Twin axes carry a known risk — two scales invite a correlation that is not there — so
the encodings are deliberately different in kind (filled bars vs. marked lines) and each
axis is labelled in the colour-free ink of its own side. Latency is the slowest
question's replayed p99 in the difficulty: an SLO breaks on the worst path, and the
geometric mean over a difficulty's questions never crosses 1 s at all.

gpt-oss-120b, this repo's 819 episodes and 156 replay cells. A second model family would
enter as a second line style per design, same grammar.

  python scripts/plot_tradeoff.py   # figures/slo-tradeoff.svg
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt

CHAIN = ["labels", "ontology", "guardrail", "plan"]
ARM_LABEL = {"labels": "1 · labels only", "ontology": "2 · + ontology",
             "guardrail": "3 · + guardrail", "plan": "4 · + plan feedback"}
ARM_COLOR = {"labels": "#c2410c", "ontology": "#ca8a04", "guardrail": "#2a78d6",
             "plan": "#15803d"}
ARM_MARKER = {"labels": "o", "ontology": "s", "guardrail": "^", "plan": "D"}
DIFFS = ["easy", "medium", "hard"]
SFS = [1, 10, 100]
OFFSETS = [-0.27, -0.09, 0.09, 0.27]
BAR_W = 0.15   # a visible surface gap between adjacent bars
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
    p.add_argument("--execution", default="results/rescore_execution.json")
    p.add_argument("--out", default="figures/slo-tradeoff.svg")
    args = p.parse_args()

    eps = json.loads(Path(args.episodes).read_text())["episodes"]
    cells = json.loads(Path(args.replay).read_text())["cells"]
    exec_rows = json.loads(Path(args.execution).read_text())["episodes"]
    exact = defaultdict(int)
    for r in exec_rows:
        if r["query_exact"]:
            exact[(r["arm"], r["sf"], r["difficulty"])] += 1

    fig, axes = plt.subplots(1, 3, figsize=(13.2, 5.2))
    fig.subplots_adjust(left=0.055, right=0.945, top=0.695, bottom=0.105, wspace=0.34)

    for col, diff in enumerate(DIFFS):
        axL = axes[col]
        axR = axL.twinx()
        n_cell = max(len([e for e in eps if e["arm"] == "labels" and e["sf"] == sf
                          and e["difficulty"] == diff]) for sf in SFS)

        for arm, off in zip(CHAIN, OFFSETS):
            xs, lat, correct, exec_ok = [], [], [], []
            for i, sf in enumerate(SFS):
                vals = [max(float(c["client_p99"]), 0.5) for c in cells
                        if c["arm"] == arm and c["sf"] == sf
                        and c["difficulty"] == diff and c.get("ok")]
                cell = [e for e in eps if e["arm"] == arm and e["sf"] == sf
                        and e["difficulty"] == diff]
                xs.append(i + off)
                lat.append(max(vals))
                correct.append(sum(1 for e in cell if e["score_correct"]))
                exec_ok.append(exact[(arm, sf, diff)])
            # ghost bar: answer accuracy, which sits near full marks nearly everywhere
            axR.bar(xs, correct, width=BAR_W, color=ARM_COLOR[arm], alpha=0.16,
                    edgecolor="white", linewidth=0.8, zorder=1)
            # solid bar: execution accuracy against the golden query — the measure that
            # actually separates the designs
            axR.bar(xs, exec_ok, width=BAR_W, color=ARM_COLOR[arm], alpha=0.62,
                    edgecolor="white", linewidth=0.8, zorder=2)
            for x, c in zip(xs, exec_ok):
                if True:
                    # drawn on the left axis (which sits above the bar axis) but
                    # positioned in the bar axis's data space, so a line crossing the
                    # bar top cannot bury the count
                    axL.annotate(f"{c}", xy=(x, 0), xycoords=axR.transData,
                                 xytext=(0, 2), textcoords="offset points",
                                 ha="center", va="bottom", fontsize=7.4,
                                 color=ARM_COLOR[arm], weight="bold", zorder=6,
                                 path_effects=[pe.withStroke(linewidth=1.8,
                                                             foreground="white")])
            # latency: lines + markers, left axis, log ms
            axL.plot(xs, lat, "-", color=ARM_COLOR[arm], linewidth=1.7, zorder=3)
            # dark edge, not white: a marker sitting inside its own bar shares the hue,
            # and a white ring on that background reads as a hollow marker
            axL.plot(xs, lat, marker=ARM_MARKER[arm], markersize=6.2,
                     markerfacecolor=ARM_COLOR[arm], markeredgecolor=INK,
                     markeredgewidth=0.9, linestyle="", zorder=4)

        axL.axhline(SLO_MS, color="#b91c1c", linewidth=1.1, linestyle=(0, (5, 4)),
                    zorder=2, alpha=0.8)
        axL.set_yscale("log")
        axL.set_ylim(8, 200000)
        axL.set_zorder(axR.get_zorder() + 1)
        axL.patch.set_visible(False)
        axL.set_xlim(-0.55, 2.55)
        axL.set_xticks(range(3))
        axL.set_xticklabels(["SF1", "SF10", "SF100"], fontsize=9)
        axL.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
        axL.set_axisbelow(False)
        axR.set_ylim(0, n_cell * 2.4)
        axR.set_yticks(list(range(0, n_cell + 1, 3 if n_cell % 3 == 0 else 5)))
        for ax in (axL, axR):
            ax.spines["top"].set_visible(False)
            ax.tick_params(length=0, labelsize=8)
        axL.set_title(f"{diff}   ({n_cell} episodes per design & scale)", fontsize=10.5,
                      weight="bold", color=INK, loc="left", pad=7)
        if col == 0:
            axL.set_ylabel("LINES · slowest question's replayed p99 (ms, log)",
                           fontsize=8.4, color=MUTED)
            axL.annotate("1 s SLO", xy=(0.02, SLO_MS),
                         xycoords=("axes fraction", "data"), fontsize=7.4,
                         color="#b91c1c", va="bottom", ha="left")
        if col == len(DIFFS) - 1:
            axR.set_ylabel("BARS · episodes answered correctly", fontsize=8.4,
                           color=MUTED)

    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=6, label=ARM_LABEL[a])
               for a in CHAIN]
    handles += [
        plt.Rectangle((0, 0), 1, 1, color=INK, alpha=0.16,
                      label="ghost bar · answer matched gold"),
        plt.Rectangle((0, 0), 1, 1, color=INK, alpha=0.62,
                      label="solid bar · query matched the golden query"),
    ]
    fig.legend(handles=handles, frameon=False, fontsize=8.6, ncol=3,
               loc="upper left", bbox_to_anchor=(0.052, 0.875))
    fig.suptitle("How much of the text2cypher was actually right, and what it cost — "
                 "against the golden query", fontsize=13.5, weight="bold", x=0.028,
                 ha="left", y=0.972, color=INK)
    fig.text(0.028, 0.935,
             "Right axis: the ghost bar is answers that carried the gold values; "
             "the solid bar (labelled at its foot) is queries whose own execution "
             "matched the golden query's\n"
             "result — the gap is where answer-level scoring flatters a design. "
             "Left axis (log): the slowest question's replayed p99, 100 model-free "
             "runs. gpt-oss-120b.",
             fontsize=8.3, color=MUTED, ha="left", va="top")
    Path(args.out).parent.mkdir(exist_ok=True)
    fig.savefig(args.out)
    fig.savefig("/tmp/claude-1000/-home-hadry-lab-AIsummit26/e438afef-9ffb-42c3-ae19-f7273ba469ed/scratchpad/slo-tradeoff.png", dpi=105)
    plt.close(fig)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
