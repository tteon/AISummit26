"""Charts for the agent<->database interaction experiment.

Three figures, written to figures/:

  ``agent-p99-by-difficulty.svg`` the summary. Six panels — audience x difficulty — with the
      questions in each cell averaged geometrically. This is the figure that answers "as the
      graph grows, do the agent designs separate, and where".
  ``agent-p99-by-question.svg``  the same measurement one panel per question, for checking a
      specific claim rather than seeing the shape.
  ``agent-cost-by-arm.svg``      db hits and round trips per answered question, by arm and
      scale. db hits rather than milliseconds because it is the one cost unit unaffected by
      what else is running on the box.
  ``agent-accuracy-by-cell.svg`` correctness by audience, difficulty and arm at each scale.
      A latency chart without this beside it would reward an agent design that is fast because
      it answers the wrong question cheaply.

Latency here is ``server_p99``: the database's own timing, over 100 replays of the query each
agent design settled on, first execution discarded. It excludes the model, which is deliberate
— the model's contribution is round trips, and that is charted separately, because the two
scale for entirely different reasons.

Usage:
  python scripts/plot_interaction.py \
      --replay results/replay_p99.json \
      --episodes results/agent_interaction.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager  # noqa: F401  (populates fontManager.ttflist)
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ARM_ORDER = ["labels", "ontology", "guardrail", "plan"]
ARM_LABEL = {
    "labels": "labels only",
    "ontology": "+ ontology",
    "guardrail": "+ guardrail",
    "plan": "+ plan feedback",
}
# Sequential rather than categorical: the arms are cumulative, so the eye should read them as
# a progression and not as four unrelated options.
ARM_COLOR = {"labels": "#c2410c", "ontology": "#ca8a04",
             "guardrail": "#0e7490", "plan": "#15803d"}
ARM_MARKER = {"labels": "o", "ontology": "s", "guardrail": "^", "plan": "D"}

AUDIENCES = ["external", "internal"]
DIFFICULTIES = ["easy", "medium", "hard"]
AUD_TITLE = {"external": "External · public-facing service",
             "internal": "Internal · AML investigator"}

# The Korean gloss under each panel is the question as it would actually be asked, so the
# font has to render it. DejaVu carries no Hangul and drops the glyphs silently, leaving
# boxes where the question should be.
_KO_FONTS = [f for f in ("Noto Sans CJK KR", "NanumGothic", "Malgun Gothic")
             if f in {fp.name for fp in matplotlib.font_manager.fontManager.ttflist}]

plt.rcParams.update({
    "font.family": (_KO_FONTS[:1] or ["DejaVu Sans"]) + ["DejaVu Sans"],
    "font.size": 8.5,
    "axes.unicode_minus": False,
    "axes.edgecolor": "#c9ced6",
    "axes.linewidth": 0.8,
    "axes.grid": True,
    "grid.color": "#eceff3",
    "grid.linewidth": 0.7,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
})



# Panel captions. Hand-written rather than clipped from the question, because clipping puts
# the same words under `int_hard_1` and `int_hard_1b` — they open identically and differ only
# in how they word the guarantee, which is the entire reason both are in the set. A caption
# that hides the one difference the pair exists to show is worse than no caption.
CAPTIONS = {
    "ext_easy_1":  "incoming transfers: count and total",
    "ext_easy_2":  "outgoing transfers: count and largest",
    "ext_med_1":   "senders that used a high-risk channel",
    "ext_med_2":   "owners of the accounts I paid",
    "ext_hard_1":  "reach within 2 hops downstream + worst risk tier",
    "ext_hard_2":  "count of accounts within 2 hops upstream",
    "int_easy_1":  "accounts in total, and how many at tier 5",
    "int_easy_2":  "top 5 channels by transaction count",
    "int_med_1":   "same-owner account pairs with a transfer between them",
    "int_med_2":   "accounts with fan-in above 100 senders",
    "int_hard_1":  "3 layers · owners \u201cguarantee one another\u201d (ambiguous)",
    "int_hard_1b": "3 layers · guarantee \u201cin either direction\u201d (explicit)",
    "int_hard_2":  "owner funnelling sub-threshold legs into one account",
}


def _caption(q, width: int = 52) -> str:
    text = CAPTIONS.get(q["id"])
    if text is None:  # a question added since; fall back to its opening clause
        text = q["question"].replace("{a}", "N").split("?")[0].strip()
    return text if len(text) <= width else text[: width - 1].rstrip(" ,") + "\u2026"


def _panel_key(q: Dict[str, Any]) -> str:
    return f"{q['audience']}/{q['difficulty']}"


def plot_p99(cells: List[Dict[str, Any]], questions: List[Dict[str, Any]], out: Path) -> None:
    qmeta = {q["id"]: q for q in questions}
    ordered = sorted(
        qmeta.values(),
        key=lambda q: (AUDIENCES.index(q["audience"]), DIFFICULTIES.index(q["difficulty"]),
                       q["id"]))
    sfs = sorted({c["sf"] for c in cells})

    by: Dict[Any, Dict[str, Any]] = {(c["question_id"], c["arm"], c["sf"]): c for c in cells}

    ncols = 3
    nrows = -(-len(ordered) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(11.5, 3.0 * nrows + 1.6), sharex=True)
    fig.subplots_adjust(hspace=0.62, wspace=0.26, top=1 - 1.35 / (3.0 * nrows + 1.6),
                        bottom=0.75 / (3.0 * nrows + 1.6), left=0.085, right=0.975)

    for idx, q in enumerate(ordered):
        ax = axes[idx // ncols][idx % ncols]
        for arm in ARM_ORDER:
            xs, ys, right, miss = [], [], [], []
            for sf in sfs:
                c = by.get((q["id"], arm, sf))
                if c and c.get("ok") and c.get("server_p99") is not None:
                    xs.append(sf)
                    # A p99 of 0 ms cannot be drawn on a log axis; the database reports whole
                    # milliseconds, so sub-millisecond queries land there legitimately.
                    ys.append(max(float(c["server_p99"]), 0.5))
                    right.append(c.get("correct_rate", 0.0))
                else:
                    miss.append(sf)
            if xs:
                ax.plot(xs, ys, linewidth=1.5, color=ARM_COLOR[arm], label=ARM_LABEL[arm],
                        zorder=3)
                # Marker fill carries correctness, because a latency chart alone rewards the
                # design that is fast by answering a cheaper question than the one asked —
                # which is exactly what happens on int_hard_1.
                for x, y, r in zip(xs, ys, right):
                    ax.plot([x], [y], marker=ARM_MARKER[arm], markersize=5.2,
                            markerfacecolor=(ARM_COLOR[arm] if r >= 1.0
                                             else ("white" if r <= 0 else "#d9dde3")),
                            markeredgecolor=ARM_COLOR[arm], markeredgewidth=1.3,
                            linestyle="none", zorder=4)
            for sf in miss:
                # An x marks a cell where the agent never got a query to run — a real outcome,
                # and one a gap in the line would hide.
                ax.plot([sf], [ax.get_ylim()[1]], marker="x", markersize=5,
                        color=ARM_COLOR[arm], zorder=5, clip_on=False)

        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(sfs)
        ax.set_xticklabels([f"SF{s}" for s in sfs])
        # Every panel labels its own x axis: with a ragged final row, sharex would strip the
        # labels from whichever columns do not reach the bottom.
        ax.tick_params(axis="both", labelsize=7.5, length=2.5, labelbottom=True)
        ax.set_title(f"{q['id'].replace('_', ' ')}  ·  {q['difficulty']}",
                     fontsize=8.5, pad=4, loc="left", color="#12151a")
        ax.text(0.0, 1.005, "", transform=ax.transAxes)
        ax.set_xlabel(_caption(q), fontsize=7.0, color="#6b7684", labelpad=3)
        if idx % ncols == 0:
            ax.set_ylabel("p99 latency (ms, log)", fontsize=8)

    for ax in axes.flat[len(ordered):]:
        ax.set_visible(False)

    # Audience bands, so the split the questions were written around is visible in the layout
    # rather than only in the ids. Placed from the first panel of each audience, since the
    # question count per audience is not fixed.
    first_row = {}
    for idx, q in enumerate(ordered):
        first_row.setdefault(q["audience"], idx // ncols)
    for aud, row in first_row.items():
        pos = axes[row][0].get_position()
        fig.text(0.085, pos.y1 + 0.030 / nrows + 0.008, AUD_TITLE[aud], fontsize=10.5,
                 weight="bold", color="#12151a", va="bottom")

    h = 3.0 * nrows + 1.6
    fig.suptitle("p99 query latency by question, scale and agent design",
                 fontsize=13, weight="bold", x=0.085, ha="left", y=1 - 0.30 / h,
                 color="#12151a")
    fig.text(0.085, 1 - 0.62 / h,
             "Up to 100 replays of the query each design settled on, first execution "
             "discarded; server-side timing, model excluded. Filled marker = every repeat "
             "matched gold, hollow = none did, grey = some.",
             fontsize=8, color="#6b7684", ha="left")
    handles = [Line2D([], [], color=ARM_COLOR[a], marker=ARM_MARKER[a], markersize=5,
                      linewidth=1.5, label=ARM_LABEL[a]) for a in ARM_ORDER]
    handles += [
        Line2D([], [], color="#47515f", marker="o", markerfacecolor="#47515f",
               linestyle="none", markersize=5, label="answer correct"),
        Line2D([], [], color="#47515f", marker="o", markerfacecolor="white",
               markeredgewidth=1.3, linestyle="none", markersize=5, label="answer wrong"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=6, frameon=False,
               fontsize=8.5, bbox_to_anchor=(0.53, 0.004))
    fig.savefig(out, format="svg")
    plt.close(fig)
    print(f"wrote {out}")



def plot_p99_by_difficulty(cells, episodes, questions, out: Path) -> None:
    """The same measurement, one panel per audience-and-difficulty instead of per question.

    Six panels rather than thirteen. Per-question panels are the right view for checking a
    specific claim; this one is the right view for seeing whether the designs separate at all,
    and where.

    The average is geometric. Latency here spans four orders of magnitude and the y axis is a
    log scale, so an arithmetic mean of two questions is decided almost entirely by the more
    expensive one — which would report the hard question's cost and call it the cell's.

    Correctness is the fraction of episodes in the cell whose answer matched gold, and it is
    carried on the marker fill for the same reason as in the per-question figure: without it,
    the design that is cheapest because it answered an easier question than the one asked looks
    like the winner.
    """
    qmeta = {q["id"]: q for q in questions}
    sfs = sorted({c["sf"] for c in cells})
    fig, axes = plt.subplots(2, 3, figsize=(11.5, 6.6), sharex=True)
    fig.subplots_adjust(hspace=0.44, wspace=0.24, top=0.845, bottom=0.135,
                        left=0.075, right=0.978)

    for r, aud in enumerate(AUDIENCES):
        for c, diff in enumerate(DIFFICULTIES):
            ax = axes[r][c]
            members = [q["id"] for q in qmeta.values()
                       if q["audience"] == aud and q["difficulty"] == diff]
            for arm in ARM_ORDER:
                xs, ys, right = [], [], []
                for sf in sfs:
                    vals = [max(float(x["server_p99"]), 0.5) for x in cells
                            if x["question_id"] in members and x["arm"] == arm
                            and x["sf"] == sf and x.get("ok")]
                    if not vals:
                        continue
                    xs.append(sf)
                    ys.append(math.exp(statistics.fmean(math.log(v) for v in vals)))
                    sel = [e for e in episodes if e["question_id"] in members
                           and e["arm"] == arm and e["sf"] == sf]
                    right.append(sum(e["score_correct"] for e in sel) / len(sel) if sel else 0.0)
                if not xs:
                    continue
                ax.plot(xs, ys, linewidth=1.8, color=ARM_COLOR[arm], zorder=3)
                for x, y, rate in zip(xs, ys, right):
                    ax.plot([x], [y], marker=ARM_MARKER[arm], markersize=6,
                            markerfacecolor=(ARM_COLOR[arm] if rate >= 1.0
                                             else ("white" if rate <= 0 else "#d9dde3")),
                            markeredgecolor=ARM_COLOR[arm], markeredgewidth=1.4,
                            linestyle="none", zorder=4)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_xticks(sfs)
            ax.set_xticklabels([f"SF{s}" for s in sfs])
            ax.tick_params(labelsize=8, length=2.5, labelbottom=True)
            ax.set_title(f"{diff}   ({len(members)} question"
                         f"{'s' if len(members) != 1 else ''})",
                         fontsize=9.5, loc="left", pad=4, color="#12151a")
            if c == 0:
                ax.set_ylabel("p99 latency (ms, log)", fontsize=8.5)

    for row, aud in enumerate(AUDIENCES):
        pos = axes[row][0].get_position()
        fig.text(0.075, pos.y1 + 0.024, AUD_TITLE[aud], fontsize=10.5, weight="bold",
                 color="#12151a", va="bottom")

    fig.suptitle("p99 query latency by difficulty, scale and agent design",
                 fontsize=13, weight="bold", x=0.075, ha="left", y=0.982, color="#12151a")
    fig.text(0.075, 0.940,
             "Geometric mean over the questions in each cell. Filled marker = every episode "
             "in the cell matched gold, hollow = none did, grey = some.",
             fontsize=8, color="#6b7684", ha="left")
    handles = [Line2D([], [], color=ARM_COLOR[a], marker=ARM_MARKER[a], markersize=5.5,
                      linewidth=1.8, label=ARM_LABEL[a]) for a in ARM_ORDER]
    handles += [
        Line2D([], [], color="#47515f", marker="o", markerfacecolor="#47515f",
               linestyle="none", markersize=5.5, label="all correct"),
        Line2D([], [], color="#47515f", marker="o", markerfacecolor="white",
               markeredgewidth=1.4, linestyle="none", markersize=5.5, label="none correct"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=6, frameon=False, fontsize=8.5,
               bbox_to_anchor=(0.53, 0.012))
    fig.savefig(out, format="svg")
    plt.close(fig)
    print(f"wrote {out}")


def plot_cost(episodes: List[Dict[str, Any]], out: Path) -> None:
    sfs = sorted({e["sf"] for e in episodes})
    fig, axes = plt.subplots(1, 3, figsize=(11.5, 3.9))
    fig.subplots_adjust(top=0.76, bottom=0.20, left=0.075, right=0.985, wspace=0.30)

    panels = [
        ("db_hits", "db hits per question (log)", True),
        ("round_trips", "Cypher round trips per question", False),
        ("chars_into_context", "characters returned into context (log)", True),
    ]
    for ax, (field, ylabel, logy) in zip(axes, panels):
        for arm in ARM_ORDER:
            xs, ys = [], []
            for sf in sfs:
                vals = [e[field] for e in episodes if e["arm"] == arm and e["sf"] == sf]
                if vals:
                    xs.append(sf)
                    ys.append(max(statistics.median(vals), 0.5 if logy else 0))
            ax.plot(xs, ys, marker=ARM_MARKER[arm], markersize=4.5, linewidth=1.6,
                    color=ARM_COLOR[arm], label=ARM_LABEL[arm])
        ax.set_xscale("log")
        if logy:
            ax.set_yscale("log")
        ax.set_xticks(sfs)
        ax.set_xticklabels([f"SF{s}" for s in sfs])
        ax.set_ylabel(ylabel, fontsize=8.5)
        ax.tick_params(labelsize=8, length=2.5)

    fig.suptitle("What the exchange costs, by agent design and scale",
                 fontsize=12.5, weight="bold", x=0.075, ha="left", y=0.955, color="#12151a")
    fig.text(0.075, 0.885,
             "Median across all twelve questions. db hits is the primary unit: it is the only "
             "one unaffected by concurrent load.",
             fontsize=8, color="#6b7684", ha="left")
    axes[0].legend(frameon=False, fontsize=8, loc="upper left")
    fig.savefig(out, format="svg")
    plt.close(fig)
    print(f"wrote {out}")


def plot_accuracy(episodes: List[Dict[str, Any]], out: Path) -> None:
    sfs = sorted({e["sf"] for e in episodes})
    fig, axes = plt.subplots(2, 3, figsize=(11.5, 6.2), sharey=True)
    fig.subplots_adjust(top=0.80, bottom=0.13, left=0.075, right=0.985,
                        hspace=0.42, wspace=0.14)

    width = 0.20
    for r, aud in enumerate(AUDIENCES):
        for c, diff in enumerate(DIFFICULTIES):
            ax = axes[r][c]
            for i, arm in enumerate(ARM_ORDER):
                ys = []
                for sf in sfs:
                    sel = [e for e in episodes if e["arm"] == arm and e["sf"] == sf
                           and e["audience"] == aud and e["difficulty"] == diff]
                    ys.append(sum(e["score_correct"] for e in sel) / len(sel) if sel else 0.0)
                xs = [j + (i - 1.5) * width for j in range(len(sfs))]
                ax.bar(xs, ys, width=width, color=ARM_COLOR[arm], label=ARM_LABEL[arm],
                       edgecolor="white", linewidth=0.5)
            ax.set_xticks(range(len(sfs)))
            ax.set_xticklabels([f"SF{s}" for s in sfs], fontsize=8)
            ax.set_ylim(0, 1.05)
            ax.set_title(f"{AUD_TITLE[aud].split(' · ')[0].lower()} · {diff}",
                         fontsize=9, loc="left", pad=4)
            ax.tick_params(labelsize=8, length=2.5)
            ax.grid(axis="x", visible=False)
            if c == 0:
                ax.set_ylabel("answers matching gold", fontsize=8.5)

    fig.suptitle("Correctness by audience, difficulty, scale and agent design",
                 fontsize=12.5, weight="bold", x=0.075, ha="left", y=0.965, color="#12151a")
    fig.text(0.075, 0.905,
             "Two questions per cell, three repeats each. A scalar answer counts as correct "
             "only if every value matches; a list only at full recall.",
             fontsize=8, color="#6b7684", ha="left")
    handles = [Line2D([], [], color=ARM_COLOR[a], marker="s", linestyle="none", markersize=7,
                      label=ARM_LABEL[a]) for a in ARM_ORDER]
    fig.legend(handles=handles, loc="lower center", ncol=4, frameon=False, fontsize=9,
               bbox_to_anchor=(0.53, 0.005))
    fig.savefig(out, format="svg")
    plt.close(fig)
    print(f"wrote {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--replay", default="results/replay_p99.json")
    p.add_argument("--episodes", default="results/agent_interaction.json")
    p.add_argument("--figures", default="figures")
    args = p.parse_args()

    run = json.loads(Path(args.episodes).read_text())
    figures = Path(args.figures)
    figures.mkdir(parents=True, exist_ok=True)

    replay_path = Path(args.replay)
    if replay_path.exists():
        cells = json.loads(replay_path.read_text())["cells"]
        plot_p99_by_difficulty(cells, run["episodes"], run["questions"],
                               figures / "agent-p99-by-difficulty.svg")
        plot_p99(cells, run["questions"], figures / "agent-p99-by-question.svg")
    else:
        print(f"skipping the p99 figure: {replay_path} does not exist yet")

    plot_cost(run["episodes"], figures / "agent-cost-by-arm.svg")
    plot_accuracy(run["episodes"], figures / "agent-accuracy-by-cell.svg")


if __name__ == "__main__":
    main()
