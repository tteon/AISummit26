#!/usr/bin/env python3
"""Figures for the in-context trio — conditions 5 (JSON), 6 (blind control), 7 (CSV).

These arms are not part of the cumulative chain the main figures draw, so they get their own
set. Three figures, written to figures/:

  in-context-outcomes.svg   what 117 episodes per arm became — correct, wrong-but-disclosed,
                            wrong off a full view, no answer, or the failure that matters:
                            wrong off a truncated view WITHOUT saying so. The blind control
                            is the point of the figure.

Colors match figures/conditions.svg for the arms; outcome segments use a validated
status-like set, silent failure deliberately in red and stacked last.

Usage:
  python scripts/plot_in_context.py --episodes results/agent_interaction.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ARMS = ["in_context", "in_context_blind", "in_context_csv"]
ARM_LABEL = {"in_context": "5 · in-context (JSON, told)",
             "in_context_blind": "6 · blind (not told)",
             "in_context_csv": "7 · in-context (CSV, told)"}
ARM_COLOR = {"in_context": "#6d28d9", "in_context_blind": "#9f7aea",
             "in_context_csv": "#be185d"}   # same trio as figures/conditions.svg, validated

# Stack order keeps pink and red non-adjacent; the chromatic chain green-yellow-pink passes
# the validator, gray is the semantic null (no answer) and carries its own label.
OUTCOMES = [("correct", "#008300", "correct"),
            ("wrong_disclosed", "#eda100", "wrong, but says the view was bounded"),
            ("wrong_other", "#e87ba4", "wrong (full view)"),
            ("no_answer", "#a6adba", "no parseable answer / out of turns"),
            ("silent_fail", "#e34948", "wrong off a truncated view, silent")]
INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "figure.facecolor": "white", "savefig.facecolor": "white",
})


def outcome(e: dict) -> str:
    if e.get("error") or e.get("parse") == "unparseable":
        return "no_answer"
    if e.get("score_correct"):
        return "correct"
    if e.get("silent_truncation_failure"):
        return "silent_fail"
    if e.get("disclosed_truncation"):
        return "wrong_disclosed"
    return "wrong_other"


def fig_outcomes(eps, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(9.2, 3.0))
    fig.subplots_adjust(left=0.215, right=0.975, top=0.66, bottom=0.20)
    for y, arm in zip((3, 2, 1), ARMS):
        sub = [e for e in eps if e["arm"] == arm]
        counts = {k: sum(1 for e in sub if outcome(e) == k) for k, _, _ in OUTCOMES}
        x = 0.0
        for key, color, _ in OUTCOMES:
            w = counts[key] / len(sub) * 100
            if w == 0:
                continue
            ax.barh(y, w, left=x, height=0.58, color=color, zorder=2,
                    edgecolor="white", linewidth=1.2)
            if w >= 5:
                ax.text(x + w / 2, y, str(counts[key]), ha="center", va="center",
                        fontsize=8.2, color="white", weight="bold")
            x += w
        ax.text(-1.5, y, ARM_LABEL[arm], ha="right", va="center", fontsize=8.8,
                color=ARM_COLOR[arm], weight="bold")
    ax.set_xlim(0, 100)
    ax.set_ylim(0.45, 3.55)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"], fontsize=8, color=MUTED)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, c, _ in OUTCOMES]
    ax.legend(handles, [l for _, _, l in OUTCOMES], frameon=False, fontsize=7.6,
              loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=3)
    fig.suptitle("What 117 episodes per arm became — the control is the point",
                 fontsize=12.5, weight="bold", x=0.028, ha="left", y=0.96, color=INK)
    fig.text(0.028, 0.855,
             "Numbers are episode counts. Withhold one field (`more_available`) and two "
             "sentences, and silent truncation failures go 11 → 71:\nthe model does not stop "
             "being wrong, it stops saying so. The disclosure the told arms produce is the "
             "field's doing, not the model's instinct.",
             fontsize=8.4, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")




def fig_by_question(eps, out: Path) -> None:
    """Per-question scalability for the trio, in the repo's by-question convention:
    SF across, median db hits per episode up (log), marker fill = correctness share."""
    qids = ["ext_easy_1", "ext_easy_2", "ext_med_1", "ext_med_2", "ext_hard_1", "ext_hard_2",
            "int_easy_1", "int_easy_2", "int_med_1", "int_med_2", "int_hard_1",
            "int_hard_1b", "int_hard_2"]
    sfs = [1, 10, 100]
    fig, axes = plt.subplots(4, 4, figsize=(11.4, 9.2), sharex=True)
    fig.subplots_adjust(left=0.065, right=0.985, top=0.865, bottom=0.05,
                        hspace=0.42, wspace=0.28)
    flat = axes.flatten()
    for ax in flat[len(qids):]:
        ax.set_visible(False)
    for ax, qid in zip(flat, qids):
        for arm in ARMS:
            xs, ys, fills = [], [], []
            for i, sf in enumerate(sfs):
                sub = [e for e in eps if e["arm"] == arm and e["sf"] == sf
                       and e["question_id"] == qid]
                if not sub:
                    continue
                hits = statistics.median([max(e.get("db_hits") or 1, 1) for e in sub])
                ok = sum(1 for e in sub if e.get("score_correct")) / len(sub)
                xs.append(i)
                ys.append(hits)
                fills.append(ok)
            ax.plot(xs, ys, "-", color=ARM_COLOR[arm], linewidth=1.4, zorder=2)
            for x, y, ok in zip(xs, ys, fills):
                face = (ARM_COLOR[arm] if ok >= 1.0
                        else "white" if ok <= 0.0 else "#c3c8d2")
                ax.plot([x], [y], "o", markersize=5.2, markerfacecolor=face,
                        markeredgecolor=ARM_COLOR[arm], markeredgewidth=1.2, zorder=3)
        ax.set_yscale("log")
        ax.set_xticks(range(len(sfs)))
        ax.set_xticklabels(["SF1", "SF10", "SF100"], fontsize=7.4)
        ax.tick_params(axis="y", labelsize=7)
        ax.set_title(qid, fontsize=8.4, color=INK, loc="left", pad=4)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0)
    handles = [plt.Line2D([], [], marker="o", linestyle="-", color=ARM_COLOR[a],
                          markersize=5.5, label=ARM_LABEL[a]) for a in ARMS]
    fig.legend(handles=handles, frameon=False, fontsize=8.4,
               loc="upper right", bbox_to_anchor=(0.985, 0.985))
    fig.suptitle("The in-context trio, question by question — db hits against scale",
                 fontsize=13.5, weight="bold", x=0.028, ha="left", y=0.975, color=INK)
    fig.text(0.028, 0.935,
             "Median database hits per episode (log). Filled marker: all three repeats "
             "matched gold; hollow: none did; grey: some. Db hits rather than\nmilliseconds "
             "because it is the one cost unit unaffected by what else runs on the box. The "
             "blind arm is often cheapest and hollow at once —\nit stops fetching before it "
             "has the rows the answer needs.",
             fontsize=8.6, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/agent_interaction.json")
    p.add_argument("--figures", default="figures")
    args = p.parse_args()
    eps = [e for e in json.loads(Path(args.episodes).read_text())["episodes"]
           if e["arm"] in ARMS]
    figs = Path(args.figures)
    figs.mkdir(exist_ok=True)
    fig_outcomes(eps, figs / "in-context-outcomes.svg")
    fig_by_question(eps, figs / "in-context-by-question.svg")


if __name__ == "__main__":
    main()
