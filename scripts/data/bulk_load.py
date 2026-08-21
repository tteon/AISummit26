#!/usr/bin/env python3
"""End-to-end bulk load of a FinBench snapshot into DozerDB via neo4j-admin.

Replaces the transactional loader for large scale factors. Measured on SF10
(33k nodes / 196k relationships):

    transactional (GraphStore.write over bolt)   874 s   ~142 rel/s
    neo4j-admin database import full             1.6 s   ~120,674 rel/s   (~849x)

That difference is what makes SF1000+ feasible at all — the transactional path
was bolt round-trip bound, so it was a software limit, not a database limit.

Flow: export CSV (DuckDB, streaming) -> place it in the server's import dir ->
drop the database -> offline import -> fix file ownership -> create/start ->
create the supporting indexes.

The ownership step is required: the import runs as root, so the store files are
root-owned and the neo4j process (uid 7474) cannot read them, which surfaces as
"Unable to start" with an AccessDeniedException.

Two execution modes, because the server is not always in a sibling container:

* ``--exec-mode docker`` (default) — the server runs in a container next to us;
  CSVs go in with ``docker cp`` and ``neo4j-admin`` runs via ``docker exec``.
* ``--exec-mode local`` — the server runs in *this* container/host, which is the
  rented-GPU case: a vast.ai instance is itself a container, so nesting docker
  just to reach a local neo4j is a needless dependency. CSVs are copied with the
  filesystem and ``neo4j-admin`` is invoked directly.

Usage:
    python scripts/data/bulk_load.py --src outputs/finbench/sf100 \
        --database finbenchsf100 --password "$NEO4J_PASSWORD"

    # on a rented instance running DozerDB as a local process
    python scripts/data/bulk_load.py --src outputs/finbench/sf10 \
        --database finbenchl10 --password "$NEO4J_PASSWORD" --exec-mode local
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("finbench_export", _HERE / "export_import_csv.py")
exporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(exporter)  # type: ignore[union-attr]

_spec2 = importlib.util.spec_from_file_location("finbench_loader", _HERE / "load_to_graph.py")
loader = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(loader)  # type: ignore[union-attr]


def _run(cmd: list[str], timeout: int = 7200) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class _Executor:
    """Where the server's filesystem and neo4j-admin live, relative to this process."""

    def __init__(self, mode: str, container: str):
        if mode not in ("docker", "local"):
            raise ValueError(f"unknown exec mode: {mode}")
        self.mode = mode
        self.container = container

    def sh(self, script: str, timeout: int = 7200) -> tuple[int, str]:
        if self.mode == "docker":
            return _run(["docker", "exec", "-u", "0", self.container, "sh", "-c", script], timeout)
        return _run(["sh", "-c", script], timeout)

    def place(self, csv_dir: Path, remote_dir: str) -> None:
        self.sh(f"rm -rf {remote_dir} && mkdir -p {remote_dir}")
        if self.mode == "docker":
            code, out = _run(["docker", "cp", f"{csv_dir}/.", f"{self.container}:{remote_dir}/"])
            if code != 0:
                raise RuntimeError(f"docker cp failed: {out[:400]}")
            return
        shutil.copytree(csv_dir, remote_dir, dirs_exist_ok=True)

    def describe(self) -> dict:
        if self.mode == "docker":
            image = _run(["docker", "inspect", "--format", "{{.Config.Image}}", self.container])[1].strip()
            return {"exec_mode": "docker", "container": self.container, "image": image or None}
        version = self.sh("neo4j-admin --version 2>/dev/null || true")[1].strip()
        return {"exec_mode": "local", "container": None, "neo4j_admin_version": version or None}


def bulk_load(*, src: Path, database: str, container: str, uri: str, user: str,
              password: str, staging: Path, keep_csv: bool = False,
              exec_mode: str = "docker", import_dir: str = "/var/lib/neo4j/import",
              workspace_id: str = exporter.WORKSPACE_DEFAULT) -> dict:
    from neo4j import GraphDatabase

    ex = _Executor(exec_mode, container)
    timings: dict[str, float] = {}
    csv_dir = staging / f"csv_{database}"
    if csv_dir.exists():
        shutil.rmtree(csv_dir)

    t0 = time.perf_counter()
    summary = exporter.export(src, csv_dir, workspace_id=workspace_id)
    timings["export_csv_s"] = time.perf_counter() - t0

    remote_dir = f"{import_dir.rstrip('/')}/csv_{database}"
    t0 = time.perf_counter()
    ex.place(csv_dir, remote_dir)
    timings["copy_s"] = time.perf_counter() - t0

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database="system") as session:
            session.run(f"DROP DATABASE {database} IF EXISTS").consume()

        t0 = time.perf_counter()
        code, out = ex.sh(exporter.import_command(database, remote_dir))
        timings["import_s"] = time.perf_counter() - t0
        if code != 0:
            raise RuntimeError(f"neo4j-admin import failed:\n{out[-1500:]}")
        imported = {
            key: int(line.strip().split()[0])
            for key in ("nodes", "relationships", "properties")
            for line in out.splitlines()
            if line.strip().endswith(key) and line.strip().split()[0].isdigit()
        }

        # The import runs as root; the server (uid 7474) must own the store.
        ex.sh(f"chown -R neo4j:neo4j /data/databases/{database} /data/transactions/{database} "
              f"2>/dev/null || true")

        t0 = time.perf_counter()
        with driver.session(database="system") as session:
            session.run(f"CREATE DATABASE {database}").consume()
        status = ""
        for _ in range(120):
            with driver.session(database="system") as session:
                rows = {r["name"]: r["currentStatus"] for r in session.run("SHOW DATABASES")}
            status = rows.get(database, "")
            if status == "online":
                break
            if status == "offline":
                # A root-owned store shows up as offline; retry the start once.
                with driver.session(database="system") as session:
                    session.run(f"START DATABASE {database}").consume()
            time.sleep(1)
        timings["online_s"] = time.perf_counter() - t0
        if status != "online":
            raise RuntimeError(f"database {database} did not come online (status={status})")

        t0 = time.perf_counter()
        with driver.session(database=database) as session:
            indexes = loader.create_indexes(session)
            counts = {
                "nodes": session.run("MATCH (n) RETURN count(n) AS c").single()["c"],
                "relationships": session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"],
            }
        timings["index_s"] = time.perf_counter() - t0
    finally:
        driver.close()
        if not keep_csv and csv_dir.exists():
            shutil.rmtree(csv_dir)

    rels = counts["relationships"]
    total = sum(timings.values())
    return {
        "schema_version": "seocho.finbench.bulk-load.v1",
        "src": str(src), "database": database,
        "workspace_id": workspace_id,
        "executor": ex.describe(),
        "csv_rows": summary["files"], "hub_threshold": summary["hub_threshold"],
        "degree": summary.get("degree"),
        "indexes": indexes,
        "imported": imported, "counts": counts,
        "timings_s": timings, "total_s": total,
        "relationships_per_second": rels / timings["import_s"] if timings.get("import_s") else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--container", default=os.getenv("DB_CONTAINER", "graphrag-neo4j"))
    parser.add_argument("--exec-mode", choices=("docker", "local"),
                        default=os.getenv("DB_EXEC_MODE", "docker"),
                        help="where neo4j-admin runs: a sibling container, or this machine")
    parser.add_argument("--import-dir", default=os.getenv("NEO4J_IMPORT_DIR", "/var/lib/neo4j/import"),
                        help="the server's import directory (as the server sees it)")
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    parser.add_argument("--workspace", default=exporter.WORKSPACE_DEFAULT,
                        help="value stamped into _workspace_id on every node and relationship")
    parser.add_argument("--staging", type=Path, default=Path("outputs/finbench/_staging"))
    parser.add_argument("--keep-csv", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    args.staging.mkdir(parents=True, exist_ok=True)
    report = bulk_load(src=args.src, database=args.database, container=args.container,
                       uri=args.uri, user=args.user, password=args.password,
                       staging=args.staging, keep_csv=args.keep_csv,
                       exec_mode=args.exec_mode, import_dir=args.import_dir,
                       workspace_id=args.workspace)
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
