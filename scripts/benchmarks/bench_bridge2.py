#!/usr/bin/env python3
"""Break bridge 2 — harness <-> DozerDB — into its stages and time each one per transport.

Two transports, same server, same queries:

  bolt   the official neo4j driver on 7687: persistent pool, PackStream, lazy record pull
  http   the Query API v2 on 7474: one JSON request, one JSON body back

Three scenarios, because the stages they stress are different:

  connect    cost of stage 1 alone (cold session vs pooled; fresh HTTP request vs keep-alive)
  rows-N     UNWIND range(1,N) — stages 5+6, serialization and transfer, row count swept
  runaway    a 100k-row result the client only wants 50 rows of. Bolt stops pulling after 50;
             HTTP has already been sent the whole body. This is the asymmetry that makes a
             model-composed query without LIMIT expensive on HTTP and merely wasteful on bolt.

Also reported per scenario: the payload as the *model* would receive it (json.dumps of the
rows), because past the driver everything becomes prompt tokens and that cost is
transport-independent.

  python scripts/bench_bridge2.py --password "$PW" [--database neo4j] [--repeat 20]
"""
from __future__ import annotations

import argparse
import base64
import datetime
import http.client
import json
import statistics
import time
import urllib.parse
from pathlib import Path

from neo4j import GraphDatabase

from runmeta import manifest

ROW_SWEEP = [50, 200, 1000]
RUNAWAY_ROWS = 100_000
CLIENT_CAP = 50

RESULTS: list = []


def pct(xs, p):
    return statistics.quantiles(xs, n=100)[p - 1] if len(xs) >= 10 else max(xs)


def timed(fn, repeat):
    xs = []
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        xs.append((time.perf_counter() - t0) * 1000)
    return xs, out


def report(name, xs, extra=""):
    print(f"  {name:<28} median {statistics.median(xs):8.2f} ms   "
          f"p95 {pct(xs, 95):8.2f} ms   {extra}")
    RESULTS.append({"name": name, "median_ms": round(statistics.median(xs), 2),
                    "p95_ms": round(pct(xs, 95), 2),
                    "samples_ms": [round(x, 2) for x in xs], "extra": extra.strip()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--http-url", default="http://localhost:7474")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--repeat", type=int, default=20)
    ap.add_argument("--json", default=None,
                    help="machine-readable report path "
                         "(default results/bench/bridge2_<utc>.json)")
    args = ap.parse_args()

    auth = base64.b64encode(f"{args.user}:{args.password}".encode()).decode()
    parsed = urllib.parse.urlparse(args.http_url)
    path = f"/db/{args.database}/query/v2"
    endpoint = f"{args.http_url}{path}"
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {auth}"}

    # One persistent connection, so the per-call numbers measure the exchange and not a fresh
    # TCP handshake each time — the [connect] stage measures setup cost separately, on its own
    # throwaway connections.
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=60)

    def http_query(statement, parameters, *, connection=None):
        c = connection or conn
        body = json.dumps({"statement": statement, "parameters": parameters}).encode()
        try:
            c.request("POST", path, body=body, headers=headers)
            resp = c.getresponse()
            raw = resp.read()
        except (http.client.RemoteDisconnected, ConnectionError, http.client.CannotSendRequest):
            c.close()
            c.request("POST", path, body=body, headers=headers)
            resp = c.getresponse()
            raw = resp.read()
        doc = json.loads(raw)
        if doc.get("errors"):
            raise RuntimeError(doc["errors"])
        fields = doc["data"]["fields"]
        rows = [dict(zip(fields, v)) for v in doc["data"]["values"]]
        return rows, len(raw)

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()

    def bolt_query(statement, parameters, cap=None):
        with driver.session(database=args.database) as s:
            result = s.run(statement, **parameters)
            if cap is None:
                rows = [dict(r) for r in result]
            else:
                rows = [dict(r) for _, r in zip(range(cap), result)]
            result.consume()
        return rows

    print(f"bridge 2 benchmark — {args.database} on {args.uri} / {endpoint}, "
          f"repeat={args.repeat}\n")

    # stage 1: connection. A fresh driver per call vs the pooled path vs one HTTP round trip.
    print("[connect]")
    xs, _ = timed(lambda: GraphDatabase.driver(
        args.uri, auth=(args.user, args.password)).verify_connectivity(), args.repeat)
    report("bolt fresh driver+handshake", xs)
    xs, _ = timed(lambda: bolt_query("RETURN 1 AS ok", {}), args.repeat)
    report("bolt pooled RETURN 1", xs)
    xs, _ = timed(lambda: http_query("RETURN 1 AS ok", {}), args.repeat)
    report("http keep-alive RETURN 1", xs)

    def http_fresh():
        c = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=60)
        try:
            return http_query("RETURN 1 AS ok", {}, connection=c)
        finally:
            c.close()
    xs, _ = timed(http_fresh, args.repeat)
    report("http fresh TCP RETURN 1", xs)

    # stages 5+6: result streaming + deserialization, row count swept.
    for n in ROW_SWEEP:
        q = "UNWIND range(1,$n) AS i RETURN i, toString(i) AS s, i*1.5 AS f"
        print(f"[rows-{n}]")
        xs, rows = timed(lambda: bolt_query(q, {"n": n}), args.repeat)
        to_model = len(json.dumps(rows, default=str))
        report("bolt", xs, f"to-model {to_model:>9,} chars")
        xs, (rows, raw_len) = timed(lambda: http_query(q, {"n": n}), args.repeat)
        report("http", xs, f"wire {raw_len:>9,} B")

    # the asymmetry: server produces 100k rows, client wants 50.
    q = "UNWIND range(1,$n) AS i RETURN i, toString(i) AS s, i*1.5 AS f"
    print(f"[runaway] {RUNAWAY_ROWS:,} rows produced, {CLIENT_CAP} wanted")
    xs, rows = timed(lambda: bolt_query(q, {"n": RUNAWAY_ROWS}, cap=CLIENT_CAP),
                     args.repeat)
    report(f"bolt stop after {CLIENT_CAP}", xs, f"rows kept {len(rows)}")
    xs, (rows, raw_len) = timed(lambda: http_query(q, {"n": RUNAWAY_ROWS}), args.repeat)
    report("http full body", xs, f"wire {raw_len:>11,} B, rows kept {len(rows)}")
    xs, rows = timed(lambda: bolt_query(q + " LIMIT $cap",
                                        {"n": RUNAWAY_ROWS, "cap": CLIENT_CAP}),
                     args.repeat)
    report("either, LIMIT in query", xs, "the fix is stage 3, not the transport")

    driver.close()
    conn.close()

    out = Path(args.json) if args.json else Path("results/bench") / (
        "bridge2_{}.json".format(
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "manifest": manifest(database=args.database, repeat=args.repeat,
                             row_sweep=ROW_SWEEP, runaway_rows=RUNAWAY_ROWS,
                             client_cap=CLIENT_CAP),
        "results": RESULTS,
    }, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
