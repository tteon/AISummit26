"""Multi-agent replay sweeps over the rust-harness binary, manifest attached.

Each arm of rust-harness prints one JSON object (raw per-op samples included) to stdout;
this wrapper runs the sweep grid, collects those objects, and writes one
results/bench/multiagent_<arm>_<UTC>.json per arm with the runmeta manifest — same
convention as the neo4rs_native results.

    python scripts/bench_multiagent.py            # all arms
    python scripts/bench_multiagent.py scale mix  # a subset
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from runmeta import manifest

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "rust-harness/target/release/rust-harness"

SWEEPS: dict[str, list[list]] = {
    # consume-side CPU wall: N agents, episode = 1 indexed lookup + 5 stream pages
    "scale": [["scale", db, n, e]
              for db, e in (("finbenchl10", 30), ("finbenchl100", 15))
              for n in (1, 2, 4, 8, 16)],
    # noisy neighbor: K sorted-pagers vs (8-K) lookup agents, 15 s window
    "mix": [["mix", "finbenchl100", 8, k, 15] for k in (0, 2, 4, 6)],
    # hot-node write contention on the isolated agentcontend db
    "contend": [["contend", n, mode, 200]
                for mode in ("same", "spread") for n in (1, 4, 8, 16)],
    # redundant interchange: 8 agents x 25 repeats, identical vs distinct result sets
    "dedup": [["dedup", "finbenchl100", 8, 25, mode] for mode in ("same", "distinct")],
}


def main() -> None:
    arms = sys.argv[1:] or list(SWEEPS)
    unknown = set(arms) - set(SWEEPS)
    if unknown:
        sys.exit(f"unknown arms: {sorted(unknown)} (have {sorted(SWEEPS)})")
    for arm in arms:
        runs = []
        for spec in SWEEPS[arm]:
            argv = [str(BIN), *map(str, spec)]
            print("::", *argv[1:], file=sys.stderr, flush=True)
            proc = subprocess.run(argv, capture_output=True, text=True, check=True)
            runs.append(json.loads(proc.stdout))
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = REPO / "results" / "bench" / f"multiagent_{arm}_{ts}.json"
        path.write_text(json.dumps({
            "manifest": manifest(bench="multiagent-replay", arm=arm,
                                 source="rust-harness", neo4j_rust_crate="0.2.0",
                                 harness="thread-per-agent, session+explicit tx per agent"),
            "runs": runs,
        }, indent=1))
        print(f"wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
