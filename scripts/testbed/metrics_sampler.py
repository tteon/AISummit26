#!/usr/bin/env python3
"""Sample the serving endpoint's /metrics into an append-only JSONL for the whole run.

The Grafana stack in `testbed/observability/` is a live view, and on a rented instance it is
often not available at all: a vast.ai instance is itself a container, so bringing up a
four-service compose there needs docker-in-docker. The durable record must not depend on it.

So this walks the same endpoint the collector would scrape, on the same interval, and writes
one JSON line per sample next to the run's other artifacts. Afterwards the run is fully
analysable offline — including replaying the samples into a local Prometheus — and the
question "what was the cache doing during episode 412" has an answer that does not require
a dashboard to have been running.

    python3 scripts/testbed/metrics_sampler.py --out results/runs/<id>/metrics.jsonl &

Sampling only what matches the manifest's keyword filter keeps the file small: a 3-hour run
at 5s is ~2,000 samples.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from harness.environment import scrape_vllm_metrics  # noqa: E402

_STOP = False


def _stop(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between samples (5 matches the collector's scrape)")
    ap.add_argument("--max-samples", type=int, default=0, help="0 = until stopped")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n, unreachable = 0, 0
    with out.open("a") as fh:
        while not _STOP and (args.max_samples == 0 or n < args.max_samples):
            snap = scrape_vllm_metrics(args.base_url, timeout=3.0)
            # Monotonic time alongside the wall clock: intervals derived from wall clock are
            # wrong across an NTP step, and this file exists to have intervals derived from.
            fh.write(json.dumps({"t_wall": time.time(), "t_mono": time.monotonic(),
                                 "reachable": snap.get("reachable", False),
                                 "lines": snap.get("lines", [])}) + "\n")
            fh.flush()
            n += 1
            unreachable += 0 if snap.get("reachable") else 1
            time.sleep(args.interval)
    print(f"[metrics-sampler] {n} samples -> {out} ({unreachable} unreachable)")


if __name__ == "__main__":
    main()
