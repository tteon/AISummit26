#!/usr/bin/env python3
"""The one-level-down figures: what a returned row costs at each layer of bridge 2.

Three figures from the 2026-08-08 measurements (results/interface_*_20260808.txt and the
tokenizer sweep recorded in docs/conditions.md's condition-7 note):

  figures/depth-format-tokens.svg   the model boundary — same 200 rows, seven encodings
  figures/depth-runaway.svg         the transport boundary — who is protected from a
                                    missing LIMIT, on a log scale (dots, not bars: a bar's
                                    length lies on a log axis)
  figures/depth-driver-cpu.svg      the runtime boundary — CPU per row by decoder, and the
                                    GIL ceiling under concurrent workers

Every number is measured, none are illustrative. Sources are the bench scripts in this
directory; regenerate the data before regenerating the figures if anything upstream moved.
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

INK, MUTED, GRID = "#12151a", "#6b7684", "#e5e8ec"
BLUE, ORANGE = "#2a78d6", "#eb6834"          # categorical slots 1-2, validated for CVD
BLUE_DIM = "#aecbf0"                          # de-emphasis tint of the same hue

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "figure.facecolor": "white", "savefig.facecolor": "white",
    "axes.edgecolor": GRID, "axes.linewidth": 0.8,
})


def _strip(ax, keep_x=True):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    if not keep_x:
        ax.spines["bottom"].set_visible(False)
    ax.tick_params(length=0, colors=MUTED)


def format_tokens(out: str) -> None:
    # o200k tokens for the identical 200 rows (4 columns of this graph's shape)
    data = [("JSON  (condition 5)", 9017, True), ("YAML", 9001, False),
            ("markdown table", 6221, False), ("columnar JSON", 6035, False),
            ("TOON", 5620, False), ("CSV  (condition 7)", 5211, True),
            ("TSV", 5208, False)]
    fig, ax = plt.subplots(figsize=(7.6, 3.1))
    fig.subplots_adjust(left=0.26, right=0.93, top=0.74, bottom=0.10)
    ys = range(len(data), 0, -1)
    for y, (label, v, hot) in zip(ys, data):
        ax.barh(y, v, height=0.62, color=BLUE if hot else BLUE_DIM, zorder=2)
        ax.text(v + 90, y, f"{v:,}", va="center", fontsize=8.6,
                color=INK if hot else MUTED)
        ax.text(-120, y, label, va="center", ha="right", fontsize=8.6,
                color=INK if hot else MUTED)
    ax.set_yticks([])
    ax.set_xlim(0, 10600)
    ax.set_xticks([])
    _strip(ax, keep_x=False)
    fig.suptitle("Same 200 rows, seven encodings — tokens the model must read",
                 fontsize=12, weight="bold", x=0.028, ha="left", y=0.955, color=INK)
    fig.text(0.028, 0.845,
             "o200k tokenizer, 4-column rows. The data is ~2,100 tokens in every encoding; "
             "the rest is per-row keys and\npunctuation. Highlighted: the pair conditions 5 "
             "and 7 actually compare.", fontsize=8.4, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def runaway(out: str) -> None:
    # 100,000 rows produced, 50 wanted — median ms (bench_bridge2, finbenchl1)
    data = [("LIMIT in the query — either transport", 1.44, BLUE),
            ("Bolt, client stops pulling after 50", 12.24, BLUE),
            ("HTTP, whole body arrives (2.4 MB)", 397.65, ORANGE)]
    fig, ax = plt.subplots(figsize=(7.6, 2.6))
    fig.subplots_adjust(left=0.34, right=0.95, top=0.70, bottom=0.22)
    for y, (label, v, c) in zip((3, 2, 1), data):
        ax.plot([v], [y], "o", markersize=9, color=c, zorder=3)
        ax.hlines(y, 0.8, v, color=GRID, linewidth=1.4, zorder=1)
        ax.text(v * 1.25, y, f"{v:,.1f} ms", va="center", fontsize=8.8, color=INK)
        ax.text(0.72, y, label, va="center", ha="right", fontsize=8.6, color=INK)
    ax.set_xscale("log")
    ax.set_xlim(0.8, 4000)
    ax.set_xticks([1, 10, 100, 1000])
    ax.set_xticklabels(["1 ms", "10 ms", "100 ms", "1 s"], fontsize=8.2)
    ax.set_yticks([])
    ax.set_ylim(0.5, 3.5)
    _strip(ax)
    fig.suptitle("A query without LIMIT: who pays, by transport", fontsize=12,
                 weight="bold", x=0.028, ha="left", y=0.94, color=INK)
    fig.text(0.028, 0.815,
             "100,000 rows produced, 50 wanted (median, log scale). Bolt streams and can stop; "
             "the HTTP body is already complete.\nThe 276× spread is closed by one clause in "
             "the query — the contract, not the transport.",
             fontsize=8.4, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def driver_cpu(out: str) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 3.5),
                                   gridspec_kw={"width_ratios": [1, 1.35]})
    fig.subplots_adjust(left=0.20, right=0.97, top=0.66, bottom=0.14, wspace=0.30)

    # left: CPU to move ONE row across bridge 2, 100k-row fetches
    rows = [("server produces it", 2.9, MUTED),
            ("client decodes it — rust", 15.5, ORANGE),
            ("client decodes it — pure", 20.8, BLUE)]
    for y, (label, v, c) in zip((1, 2, 3), rows):
        ax1.barh(y, v, height=0.6, color=c, zorder=2)
        ax1.text(v + 0.5, y, f"{v:.1f}", va="center", fontsize=8.8, color=INK)
        ax1.text(-0.8, y, label, va="center", ha="right", fontsize=8.6, color=INK)
    ax1.set_yticks([])
    ax1.set_xlim(0, 26)
    ax1.set_xticks([])
    _strip(ax1, keep_x=False)
    ax1.set_title("CPU per row, µs (100k-row fetch)", fontsize=9.4, color=INK,
                  loc="left", pad=8)

    # right: the GIL ceiling — client process utilization vs workers
    workers = [1, 2, 4, 8]
    pure = [0.94, 1.13, 1.34, 1.31]
    rust = [0.92, 1.16, 1.41, 1.36]
    ax2.axhline(1.0, color=GRID, linewidth=1.2, linestyle=(0, (4, 3)))
    ax2.text(1.05, 0.82, "1 core (the GIL)", fontsize=7.8, color=MUTED, va="top")
    ax2.plot(workers, pure, "-o", color=BLUE, linewidth=2, markersize=6, label="pure-python")
    ax2.plot(workers, rust, "-o", color=ORANGE, linewidth=2, markersize=6, label="rust")
    ax2.set_xticks(workers)
    ax2.set_xlabel("concurrent workers (threads, one process)", fontsize=8.4, color=MUTED)
    ax2.set_ylim(0, 8.3)
    ax2.set_yticks([0, 1, 2, 4, 8])
    ax2.set_yticklabels(["0", "1", "2", "4", "8 cores"], fontsize=8.2)
    ax2.grid(axis="y", color=GRID, linewidth=0.7)
    ax2.set_axisbelow(True)
    _strip(ax2)
    ax2.legend(frameon=False, fontsize=8.2, loc="upper left")
    ax2.set_title("Client CPU utilization vs workers", fontsize=9.4, color=INK,
                  loc="left", pad=8)

    fig.suptitle("The runtime boundary: consuming a row costs ~7× producing it",
                 fontsize=12, weight="bold", x=0.028, ha="left", y=0.965, color=INK)
    fig.text(0.028, 0.875,
             "Left: client process CPU (user+sys) per row vs the DB container's cgroup CPU — "
             "the rust codec cuts decode 26%, server cost identical.\nRight: 2,000-row calls, "
             "25/worker, utilization against one core. Both builds plateau near 1.3 of 8 "
             "cores —\nthe ceiling is the row representation, not the codec.",
             fontsize=8.4, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def scalability(out: str) -> None:
    """The talk's closing figure for the driver axis: the same 8-worker load, four ways."""
    workers = [1, 2, 4, 8]
    series = [
        ("Python threads · pure", [43.8, 91.4, 265.9, 769.3], BLUE),
        ("Python threads · rust-ext", [33.1, 68.3, 214.8, 678.7], ORANGE),
        ("Python, process per worker", [45.3, 48.2, 61.4, 81.2], "#1baf7a"),
        ("neo4rs, one process (tokio)", [4.6, 5.6, 6.1, 7.7], "#4a3aa7"),
    ]
    fig, ax = plt.subplots(figsize=(8.2, 3.9))
    fig.subplots_adjust(left=0.09, right=0.90, top=0.76, bottom=0.13)
    for label, ys, c in series:
        ax.plot(workers, ys, "-o", color=c, linewidth=2, markersize=6, label=label)
        ax.text(8.25, ys[-1], f"{ys[-1]:,.0f} ms", fontsize=8.2, color=c, va="center")
    ax.set_yscale("log")
    ax.set_xticks(workers)
    ax.set_xlabel("concurrent workers, 2,000-row calls", fontsize=8.4, color=MUTED)
    ax.set_ylabel("tool-call p50, ms (log)", fontsize=8.4, color=MUTED)
    ax.grid(axis="y", color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    _strip(ax)
    ax.legend(frameon=False, fontsize=8.0, loc="upper left")
    fig.suptitle("Same load, four data planes — the ceiling was the runtime",
                 fontsize=12, weight="bold", x=0.028, ha="left", y=0.96, color=INK)
    fig.text(0.028, 0.865,
             "Median tool-call latency against concurrency, one box, same DozerDB. Threads "
             "share one interpreter and queue on the GIL; processes buy\ncores with N "
             "interpreters; a single native process does the whole load in 7.7 ms because "
             "rows never become Python objects.",
             fontsize=8.4, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def engineering_summary(out: str) -> None:
    """The whole engineering ledger on one slide: encoding, transport, runtime, concurrency.

    Four measured panels, each carrying its own numbers, so the talk needs exactly one
    detail chart next to the levers overview.
    """
    fig, axes = plt.subplots(2, 2, figsize=(11.2, 6.4))
    fig.subplots_adjust(left=0.13, right=0.965, top=0.82, bottom=0.075,
                        hspace=0.62, wspace=0.42)
    (ax_tok, ax_run), (ax_cpu, ax_sc) = axes

    # (a) encoding — tokens the model reads for the same 200 rows
    for y, (label, v, hot) in enumerate(
            [("CSV", 5211, True), ("markdown", 6221, False), ("JSON", 9017, True)], start=1):
        ax_tok.barh(y, v, height=0.6, color=BLUE if hot else BLUE_DIM, zorder=2)
        ax_tok.text(v + 150, y, f"{v:,}", va="center", fontsize=8.6, color=INK)
        ax_tok.text(-250, y, label, va="center", ha="right", fontsize=8.6, color=INK)
    ax_tok.set_xlim(0, 11500)
    ax_tok.set_xticks([])
    ax_tok.set_yticks([])
    _strip(ax_tok, keep_x=False)
    ax_tok.set_title("Encoding — o200k tokens, same 200 rows\n(the data itself is ~2,100 in "
                     "every encoding)", fontsize=9.2, color=INK, loc="left", pad=6)

    # (b) transport — the runaway query, who is protected from a missing LIMIT
    for y, (label, v, c) in enumerate(
            [("HTTP, full 2.4 MB body", 397.65, ORANGE),
             ("Bolt, client stops at 50", 12.24, BLUE),
             ("LIMIT in the query", 1.44, BLUE)], start=1):
        ax_run.plot([v], [y], "o", markersize=8.5, color=c, zorder=3)
        ax_run.hlines(y, 0.9, v, color=GRID, linewidth=1.3, zorder=1)
        ax_run.text(v * 1.3, y, f"{v:,.1f} ms", va="center", fontsize=8.6, color=INK)
        ax_run.text(0.8, y, label, va="center", ha="right", fontsize=8.4, color=INK)
    ax_run.set_xscale("log")
    ax_run.set_xlim(0.9, 3000)
    ax_run.set_xticks([1, 10, 100, 1000])
    ax_run.set_xticklabels(["1", "10", "100", "1000 ms"], fontsize=7.8)
    ax_run.set_yticks([])
    ax_run.set_ylim(0.4, 3.6)
    _strip(ax_run)
    ax_run.set_title("Transport — 100k rows produced, 50 wanted\n(the fix is the contract, "
                     "not the transport)", fontsize=9.2, color=INK, loc="left", pad=6)

    # (c) runtime — CPU to move one row, client vs server
    for y, (label, v, c) in enumerate(
            [("server produces", 2.9, MUTED),
             ("decode — rust codec", 15.5, ORANGE),
             ("decode — pure python", 20.8, BLUE)], start=1):
        ax_cpu.barh(y, v, height=0.6, color=c, zorder=2)
        ax_cpu.text(v + 0.4, y, f"{v:.1f} µs", va="center", fontsize=8.6, color=INK)
        ax_cpu.text(-0.6, y, label, va="center", ha="right", fontsize=8.4, color=INK)
    ax_cpu.set_xlim(0, 25)
    ax_cpu.set_xticks([])
    ax_cpu.set_yticks([])
    _strip(ax_cpu, keep_x=False)
    ax_cpu.set_title("Runtime — CPU per row, 100k-row fetch\n(346 B/row as Python dicts in "
                     "BOTH builds: the cost is the representation)",
                     fontsize=9.2, color=INK, loc="left", pad=6)

    # (d) concurrency — the same 8-worker load, four data planes
    workers = [1, 2, 4, 8]
    for label, ys, c in [
            ("threads · pure", [43.8, 91.4, 265.9, 769.3], BLUE),
            ("threads · rust-ext", [33.1, 68.3, 214.8, 678.7], ORANGE),
            ("process/worker", [45.3, 48.2, 61.4, 81.2], "#1baf7a"),
            ("neo4rs, one process", [4.6, 5.6, 6.1, 7.7], "#4a3aa7")]:
        ax_sc.plot(workers, ys, "-o", color=c, linewidth=1.8, markersize=5, label=label)
        ax_sc.text(8.25, ys[-1], f"{ys[-1]:,.0f}", fontsize=8.2, color=c, va="center")
    ax_sc.set_yscale("log")
    ax_sc.set_xticks(workers)
    ax_sc.tick_params(labelsize=7.8)
    ax_sc.grid(axis="y", color=GRID, linewidth=0.6)
    ax_sc.set_axisbelow(True)
    _strip(ax_sc)
    ax_sc.legend(frameon=False, fontsize=7.4, loc="lower right")
    ax_sc.set_title("Concurrency — p50 ms vs workers, 2,000-row calls\n(the ceiling was the GIL)",
                    fontsize=9.2, color=INK, loc="left", pad=6)

    fig.suptitle("The exchange, measured — encoding, transport, runtime, concurrency",
                 fontsize=13.5, weight="bold", x=0.028, ha="left", y=0.975, color=INK)
    fig.text(0.028, 0.925,
             "One box, same DozerDB, per-iteration samples and machine manifests in "
             "results/interface/.", fontsize=8.6, color=MUTED, ha="left", va="top")
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    format_tokens("figures/depth-format-tokens.svg")
    runaway("figures/depth-runaway.svg")
    driver_cpu("figures/depth-driver-cpu.svg")
    scalability("figures/depth-scalability.svg")
    engineering_summary("figures/engineering-detail.svg")
