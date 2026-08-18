#!/usr/bin/env python3
"""The token measurement behind condition 7 and the encoding panel, as a recorded artifact.

The 9,017-vs-5,211 numbers were first measured interactively; a chart may not rest on a
number that exists only in a conversation, so this script regenerates the measurement from
its exact specification (seed 7, 200 rows of the graph's 4-column shape, o200k) and writes
it to results/bench/ with a machine manifest.

  python scripts/measure_format_tokens.py
"""
from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path

import tiktoken

from runmeta import manifest

SEED, N_ROWS = 7, 200


def rows_spec():
    rng = random.Random(SEED)
    return [{"acct_no": rng.randint(1, 10**9), "amount": round(rng.random() * 1e6, 2),
             "channel_risk": round(rng.random(), 3), "ts": "2026-08-08T09:00:00"}
            for _ in range(N_ROWS)]


def main() -> None:
    rows = rows_spec()
    cols = list(rows[0].keys())
    enc = tiktoken.get_encoding("o200k_base")
    tok = lambda s: len(enc.encode(s))

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)

    measured = {
        "json": tok(json.dumps({"rows": rows, "row_count": N_ROWS,
                                "more_available": True})),
        "json_compact": tok(json.dumps({"rows": rows}, separators=(",", ":"))),
        "csv": tok(buf.getvalue()),
        "tsv": tok("\t".join(cols) + "\n"
                   + "\n".join("\t".join(str(r[c]) for c in cols) for r in rows)),
        "markdown_table": tok("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
                              + "\n".join("| " + " | ".join(str(v) for v in r.values())
                                          + " |" for r in rows)),
        "columnar_json": tok(json.dumps({"columns": cols,
                                         "data": [[r[c] for c in cols] for r in rows],
                                         "row_count": N_ROWS, "more_available": True})),
        "toon": tok(f"rows[{N_ROWS}]{{{','.join(cols)}}}:\n"
                    + "\n".join("  " + ",".join(str(r[c]) for c in cols) for r in rows)
                    + "\nmore_available: true"),
    }
    out = Path("results/bench/format_tokens.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "manifest": manifest(tokenizer="o200k_base", seed=SEED, n_rows=N_ROWS,
                             tiktoken=getattr(tiktoken, "__version__", None)),
        "tokens": measured,
    }, indent=1))
    for k, v in sorted(measured.items(), key=lambda kv: -kv[1]):
        print(f"{k:15} {v:6,}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
