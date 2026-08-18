#!/usr/bin/env python3
"""The overview pair: per-call DB latency p50 and p99, by difficulty, scale and design.

Two charts, one per percentile. Three panels each (easy / medium / hard — the questions in
a category pooled), SF across, per-tool-call database latency up (log), one labelled line
per design across all seven conditions. Marker fill carries the cell's correctness share:
filled = every episode matched gold, hollow = none, grey = some.

The latency is the measured `ms` of every executed tool call in the cell's episodes —
per-call, so an agent that answers in one round trip and one that pages eight times are
compared on what each trip cost, while the paging itself is visible in db-hits and
round-trip charts (`--by-question` regenerates the 13-panel db-hits detail).

  python scripts/plot_overview.py                 # figures/overview-p50.svg + overview-p99.svg
  python scripts/plot_overview.py --by-question   # the 13-panel db-hits backup
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ARMS = ["labels", "ontology", "guardrail", "plan", "in_context", "in_context_blind",
        "in_context_csv"]
# The overview draws only the cumulative contract chain. Conditions 5-7 are a different
# regime (the database stops aggregating) plus its two ablations — not designs anyone
# ships — and they have their own figure set (plot_in_context.py). Overlaying them here
# compared one expensive call against five cheap pages and called it one axis.
CHAIN = ARMS[:4]
ARM_LABEL = {"labels": "1 · labels only", "ontology": "2 · + ontology",
             "guardrail": "3 · + guardrail", "plan": "4 · + plan feedback",
             "in_context": "5 · in-context (JSON)", "in_context_blind": "6 · blind control",
             "in_context_csv": "7 · in-context (CSV)"}
ARM_COLOR = {"labels": "#c2410c", "ontology": "#ca8a04", "guardrail": "#2a78d6",
             "plan": "#15803d", "in_context": "#6d28d9", "in_context_blind": "#9f7aea",
             "in_context_csv": "#be185d"}   # validated as a set; markers are the backup
ARM_MARKER = {"labels": "o", "ontology": "s", "guardrail": "^", "plan": "D",
              "in_context": "v", "in_context_blind": "P", "in_context_csv": "X"}
DIFFICULTIES = ["easy", "medium", "hard"]
SFS = [1, 10, 100]
INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "figure.facecolor": "white", "savefig.facecolor": "white",
})


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


# The dashed reference each chart carries. Generic industry conventions, not measured
# here: ~200 ms is the usual per-call budget inside an interactive request, and 1 s is
# the common p99 SLO for the request itself — a DB call at 1 s has spent the whole budget.
SLO = {"p50": (200, "200 ms — interactive per-call budget"),
       "p99": (1000, "1 s — a common request p99 SLO")}


def overview(eps, *, q, tag, out: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(11.6, 4.0), sharey=True)
    fig.subplots_adjust(left=0.07, right=0.985, top=0.665, bottom=0.115, wspace=0.14)
    slo_ms, slo_label = SLO[tag]
    for ax, diff in zip(axes, DIFFICULTIES):
        ax.axhline(slo_ms, color="#b91c1c", linewidth=1.1, linestyle=(0, (5, 4)),
                   zorder=1, alpha=0.75)
        for arm in CHAIN:
            xs, ys, fills = [], [], []
            for i, sf in enumerate(SFS):
                cell = [e for e in eps if e["arm"] == arm and e["sf"] == sf
                        and e["difficulty"] == diff]
                calls = [c["ms"] for e in cell for c in e.get("calls", [])
                         if c.get("outcome") == "ok" and c.get("ms") is not None]
                v = _pct(calls, q)
                if v is None:
                    continue
                xs.append(i)
                ys.append(max(v, 0.5))
                fills.append(sum(1 for e in cell if e.get("score_correct")) / len(cell))
            ax.plot(xs, ys, "-", color=ARM_COLOR[arm], linewidth=1.5, zorder=2)
            for x, y, ok in zip(xs, ys, fills):
                face = (ARM_COLOR[arm] if ok >= 1.0
                        else "white" if ok <= 0.0 else "#c3c8d2")
                ax.plot([x], [y], marker=ARM_MARKER[arm], markersize=6,
                        markerfacecolor=face, markeredgecolor=ARM_COLOR[arm],
                        markeredgewidth=1.2, linestyle="", zorder=3)
        ax.set_yscale("log")
        ax.set_xticks(range(len(SFS)))
        ax.set_xticklabels(["SF1", "SF10", "SF100"], fontsize=8.6)
        ax.set_title(f"{diff}", fontsize=10.5, weight="bold", color=INK, loc="left", pad=6)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0, labelsize=7.6)
    axes[0].set_ylabel(f"per-call DB latency, {tag} (ms, log)", fontsize=8.4, color=MUTED)
    axes[0].annotate(slo_label, xy=(0.03, slo_ms), xycoords=("axes fraction", "data"),
                     fontsize=7.6, color="#b91c1c", va="bottom", ha="left")
    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=5.6, label=ARM_LABEL[a])
               for a in CHAIN]
    fig.legend(handles=handles, frameon=False, fontsize=8.2, ncol=4,
               loc="upper left", bbox_to_anchor=(0.045, 0.87))
    fig.suptitle(f"Query latency against scale, by difficulty — {tag} of every executed "
                 f"call, the contract chain (1–4)", fontsize=13, weight="bold",
                 x=0.028, ha="left", y=0.965, color=INK)
    fig.text(0.028, 0.90,
             "Questions within a difficulty pooled (easy 4 · medium 4 · hard 5, ×3 repeats). "
             "Marker fill: filled = every episode in the cell matched gold, hollow = none, "
             "grey = some. The in-context regime (5–7) has its own figure set.",
             fontsize=8.4, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def overview_p99_replay(cells, eps, out: Path) -> None:
    """The p99 chart, from the stage-two replays instead of live episode calls.

    A cell's live calls number as few as a dozen, and the 99th percentile of twelve
    samples is the maximum wearing a costume. The replay runs the query each design
    settled on 100 times without a model (first execution discarded), so every condition
    gets the same n and the tail is an estimate rather than an anecdote. Client-observed
    ms, same measurement plane as the p50 chart. The panel value is the geometric mean
    over the difficulty's questions — on a log axis an arithmetic mean would report the
    expensive question's cost and call it the cell's. Marker fill still carries
    live-episode correctness.
    """
    import math
    fig, axes = plt.subplots(1, 3, figsize=(11.6, 4.0), sharey=True)
    fig.subplots_adjust(left=0.07, right=0.985, top=0.665, bottom=0.115, wspace=0.14)
    slo_ms, slo_label = SLO["p99"]
    for ax, diff in zip(axes, DIFFICULTIES):
        ax.axhline(slo_ms, color="#b91c1c", linewidth=1.1, linestyle=(0, (5, 4)),
                   zorder=1, alpha=0.75)
        for arm in CHAIN:
            xs, ys, fills = [], [], []
            for i, sf in enumerate(SFS):
                vals = [max(float(c["client_p99"]), 0.5) for c in cells
                        if c["arm"] == arm and c["sf"] == sf
                        and c["difficulty"] == diff and c.get("ok")]
                if not vals:
                    continue
                xs.append(i)
                ys.append(math.exp(sum(math.log(v) for v in vals) / len(vals)))
                cell = [e for e in eps if e["arm"] == arm and e["sf"] == sf
                        and e["difficulty"] == diff]
                fills.append(sum(1 for e in cell if e.get("score_correct")) / len(cell)
                             if cell else 0.0)
            ax.plot(xs, ys, "-", color=ARM_COLOR[arm], linewidth=1.5, zorder=2)
            for x, y, ok in zip(xs, ys, fills):
                face = (ARM_COLOR[arm] if ok >= 1.0
                        else "white" if ok <= 0.0 else "#c3c8d2")
                ax.plot([x], [y], marker=ARM_MARKER[arm], markersize=6,
                        markerfacecolor=face, markeredgecolor=ARM_COLOR[arm],
                        markeredgewidth=1.2, linestyle="", zorder=3)
        ax.set_yscale("log")
        ax.set_xticks(range(len(SFS)))
        ax.set_xticklabels(["SF1", "SF10", "SF100"], fontsize=8.6)
        ax.set_title(f"{diff}", fontsize=10.5, weight="bold", color=INK, loc="left", pad=6)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0, labelsize=7.6)
    axes[0].set_ylabel("settled-query p99, replayed (ms, log)", fontsize=8.4, color=MUTED)
    axes[0].annotate(slo_label, xy=(0.03, slo_ms), xycoords=("axes fraction", "data"),
                     fontsize=7.6, color="#b91c1c", va="bottom", ha="left")
    handles = [plt.Line2D([], [], marker=ARM_MARKER[a], linestyle="-",
                          color=ARM_COLOR[a], markersize=5.6, label=ARM_LABEL[a])
               for a in CHAIN]
    fig.legend(handles=handles, frameon=False, fontsize=8.2, ncol=4,
               loc="upper left", bbox_to_anchor=(0.045, 0.87))
    fig.suptitle("Query latency against scale, by difficulty — p99 of each design's "
                 "settled query, 100 replays", fontsize=13, weight="bold",
                 x=0.028, ha="left", y=0.965, color=INK)
    fig.text(0.028, 0.90,
             "Stage two: the query each design settled on, replayed 100× without a model "
             "(first run discarded). Geometric mean over the difficulty's questions; "
             "marker fill = live-episode correctness, as on the p50 chart.",
             fontsize=8.4, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def by_question(eps, out: Path) -> None:
    """The 13-panel db-hits detail, kept as a regenerable backup."""
    qids = ["ext_easy_1", "ext_easy_2", "ext_med_1", "ext_med_2", "ext_hard_1",
            "ext_hard_2", "int_easy_1", "int_easy_2", "int_med_1", "int_med_2",
            "int_hard_1", "int_hard_1b", "int_hard_2"]
    fig, axes = plt.subplots(4, 4, figsize=(12.6, 10.4), sharex=True)
    fig.subplots_adjust(left=0.06, right=0.985, top=0.845, bottom=0.045,
                        hspace=0.44, wspace=0.30)
    flat = axes.flatten()
    for ax in flat[len(qids):]:
        ax.set_visible(False)
    for ax, qid in zip(flat, qids):
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
             "database aggregate; 5–7 pull the rows into context.",
             fontsize=8.8, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episodes", default="results/agent_interaction.json")
    p.add_argument("--replay", default="results/replay_p99.json")
    p.add_argument("--figures", default="figures")
    p.add_argument("--by-question", action="store_true",
                   help="also write the 13-panel db-hits backup chart")
    args = p.parse_args()
    eps = json.loads(Path(args.episodes).read_text())["episodes"]
    figs = Path(args.figures)
    figs.mkdir(exist_ok=True)
    overview(eps, q=0.50, tag="p50", out=figs / "overview-p50.svg")
    cells = json.loads(Path(args.replay).read_text())["cells"]
    overview_p99_replay(cells, eps, figs / "overview-p99.svg")
    if args.by_question:
        by_question(eps, figs / "overview-by-question.svg")


if __name__ == "__main__":
    main()
