#!/usr/bin/env python3
"""Condition 4b: what happens when a refusal hands the agent the planner's steering wheel.

Two panels, the deck's grammar (bars = correctness on the right axis, lines = latency on
the left, log): the A/B against condition 4, and then the split that explains it.

Left — plan vs plan_hints at SF100 and SF1000. Hints move latency hard (SF1000 median
episode 96 s → 26 s; SF100 median db hits 3.8M → 722k) and move accuracy the wrong way.

Right — the same episodes split by whether the settled query actually carried a `USING`
clause. This is not a controlled contrast and the chart says so: adoption concentrates on
anchored external questions, where an index seek is the obvious steer, and never reaches
the unanchored conjunctions. It shows where hints apply, not what they would do
everywhere.

Latency here is the live episode wall clock, not the replayed tail the overview uses —
the A/B has no replay stage — so the two figures' latency axes are not comparable, and
this one is labelled accordingly.

  python scripts/plot_plan_hints.py   # figures/plan-hints-ab.svg
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt

ARM_COLOR = {"plan": "#15803d", "plan_hints": "#7c3aed"}
ARM_LABEL = {"plan": "4 · plan feedback", "plan_hints": "4b · + engineer_query & hints"}
ARM_MARKER = {"plan": "D", "plan_hints": "P"}
SPLIT_COLOR = {"hinted": "#7c3aed", "plain": "#9aa3b0"}
INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"
SFS = [100, 1000]
BAR_W = 0.22


def med(rows, key):
    xs = sorted(r[key] for r in rows)
    return xs[len(xs) // 2] if xs else 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/plan_hints_ab.json")
    p.add_argument("--out", default="figures/plan-hints-ab.svg")
    args = p.parse_args()
    eps = json.loads(Path(args.episodes).read_text())["episodes"]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "figure.facecolor": "white", "savefig.facecolor": "white"})
    fig, (axA, axB) = plt.subplots(1, 2, figsize=(11.8, 5.0))
    fig.subplots_adjust(left=0.072, right=0.915, top=0.70, bottom=0.13, wspace=0.46)

    # ---- panel A: the A/B ----
    axAr = axA.twinx()
    n_cell = max(len([e for e in eps if e["arm"] == a and e["sf"] == sf])
                 for a in ARM_COLOR for sf in SFS)
    for arm, off in zip(("plan", "plan_hints"), (-0.13, 0.13)):
        xs, lat, ok = [], [], []
        for i, sf in enumerate(SFS):
            r = [e for e in eps if e["arm"] == arm and e["sf"] == sf]
            xs.append(i + off)
            lat.append(med(r, "wall_ms"))
            ok.append(sum(1 for e in r if e["score_correct"]))
        axAr.bar(xs, ok, width=BAR_W, color=ARM_COLOR[arm], alpha=0.45,
                 edgecolor="white", linewidth=0.9, zorder=1)
        for x, c in zip(xs, ok):
            axA.annotate(f"{c}", xy=(x, 0), xycoords=axAr.transData, xytext=(0, 2),
                         textcoords="offset points", ha="center", va="bottom",
                         fontsize=7.6, color=ARM_COLOR[arm], weight="bold", zorder=6,
                         path_effects=[pe.withStroke(linewidth=1.8, foreground="white")])
        axA.plot(xs, lat, "-", color=ARM_COLOR[arm], linewidth=1.8, zorder=3)
        axA.plot(xs, lat, marker=ARM_MARKER[arm], markersize=7,
                 markerfacecolor=ARM_COLOR[arm], markeredgecolor=INK,
                 markeredgewidth=0.9, linestyle="", zorder=4)
    axA.set_yscale("log")
    axA.set_ylim(1500, 900000)
    axA.set_zorder(axAr.get_zorder() + 1)
    axA.patch.set_visible(False)
    axA.set_xlim(-0.5, 1.5)
    axA.set_xticks(range(len(SFS)))
    axA.set_xticklabels([f"SF{s}" for s in SFS], fontsize=9)
    axAr.set_ylim(0, n_cell * 2.3)
    axAr.set_yticks(range(0, n_cell + 1, 10))
    axA.set_ylabel("LINES · median episode wall clock (ms, log)", fontsize=8.4,
                   color=MUTED)
    axAr.set_ylabel(f"BARS · episodes answered correctly (of {n_cell})", fontsize=8.4,
                    color=MUTED)
    axA.set_title("A · the A/B — hints buy latency, not accuracy", fontsize=10.5,
                  weight="bold", color=INK, loc="left", pad=7)

    # ---- panel B: hinted vs plain, inside 4b ----
    axBr = axB.twinx()
    hints = [e for e in eps if e["arm"] == "plan_hints"]
    groups = {"hinted": [e for e in hints if e.get("hint_in_settled")],
              "plain": [e for e in hints if not e.get("hint_in_settled")]}
    for i, (name, rows) in enumerate(groups.items()):
        share = sum(1 for e in rows if e["score_correct"]) / len(rows)
        axBr.bar([i], [share], width=0.4, color=SPLIT_COLOR[name], alpha=0.45,
                 edgecolor="white", linewidth=0.9, zorder=1)
        axB.plot([i], [med(rows, "wall_ms")], marker="o", markersize=9,
                 markerfacecolor=SPLIT_COLOR[name], markeredgecolor=INK,
                 markeredgewidth=0.9, linestyle="", zorder=4)
        axB.annotate(f"{med(rows, 'wall_ms')/1000:.1f} s", xy=(i, med(rows, "wall_ms")),
                     xytext=(10, 0), textcoords="offset points", va="center",
                     fontsize=8.4, color=SPLIT_COLOR[name], weight="bold")
        axB.annotate(f"{sum(1 for e in rows if e['score_correct'])}/{len(rows)}",
                     xy=(i, 0), xycoords=axBr.transData, xytext=(0, 3),
                     textcoords="offset points", ha="center", va="bottom",
                     fontsize=8.4, color=SPLIT_COLOR[name], weight="bold", zorder=6,
                     path_effects=[pe.withStroke(linewidth=2, foreground="white")])
    axB.set_yscale("log")
    axB.set_ylim(1500, 900000)
    axB.set_zorder(axBr.get_zorder() + 1)
    axB.patch.set_visible(False)
    axB.set_xlim(-0.6, 1.6)
    axB.set_xticks([0, 1])
    axB.set_xticklabels(["settled query\ncarries USING", "no hint in the\nsettled query"],
                        fontsize=8.4)
    axBr.set_ylim(0, 2.3)
    axBr.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    axBr.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    axB.set_ylabel("median episode wall clock (ms, log)", fontsize=8.4, color=MUTED)
    axBr.set_ylabel("BARS · share answered correctly", fontsize=8.4, color=MUTED)
    axB.set_title("B · inside 4b — where the hint lands", fontsize=10.5,
                  weight="bold", color=INK, loc="left", pad=7)

    for ax in (axA, axB, axAr, axBr):
        ax.spines["top"].set_visible(False)
        ax.tick_params(length=0, labelsize=8)
    for ax in (axA, axB):
        ax.grid(axis="y", color=GRID, linewidth=0.6)

    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=6.4, label=ARM_LABEL[a])
               for a in ("plan", "plan_hints")]
    fig.legend(handles=handles, frameon=False, fontsize=8.8, ncol=2,
               loc="upper left", bbox_to_anchor=(0.072, 0.845))
    fig.suptitle("Handing the agent the planner's steering wheel — condition 4b",
                 fontsize=13.5, weight="bold", x=0.028, ha="left", y=0.965, color=INK)
    fig.text(0.028, 0.905,
             "156 episodes, gpt-oss-120b. A refusal unlocks engineer_query (probe a "
             "candidate, hints allowed) — but the agent mostly used hints *before* being "
             "refused: 93 refusals,\n13 episodes that probed, 40 settled queries carrying "
             "a real USING clause. Panel B is descriptive, not controlled: adoption "
             "concentrates on anchored external questions.",
             fontsize=8.2, color=MUTED, ha="left", va="top")
    Path(args.out).parent.mkdir(exist_ok=True)
    fig.savefig(args.out)
    fig.savefig("/tmp/claude-1000/-home-hadry-lab-AIsummit26/e438afef-9ffb-42c3-ae19-f7273ba469ed/scratchpad/plan-hints.png", dpi=110)
    plt.close(fig)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
