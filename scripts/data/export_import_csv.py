#!/usr/bin/env python3
"""FinBench parquet snapshot -> neo4j-admin CSV, and the import command that reads it.

`bulk_load.py` needs two things from this module: `export()`, which turns a snapshot
directory (`outputs/finbench/sf<N>/{nodes,edges}/*.parquet`) into the header-carrying CSVs
`neo4j-admin database import full` expects, and `import_command()`, which spells that
import out. Naming is derived from the spec tables below rather than from disk, so the
command can be built without the export having happened in the same process.

Two decisions the CSV shape forces, both of which cost a reload to get wrong:

* **Party is one ID space.** `OWN`, `APPLY`, `INVEST` and `GUARANTEE` all start at
  `Person|Company`, and the generator gives parties one contiguous id range (Person
  0..n-1, Company n..n+m-1 — `gen_duckdb.py:641`), so a single `Party` id space with a
  `:LABEL` column resolves those endpoints. Separate `Person`/`Company` spaces cannot:
  `own.src = 5` carries no label of its own.
* **Every parquet column is loaded**, typed from the parquet schema, not just the
  ontology-declared ones. The guardrail's `allowed_properties` comes from the ontology, so
  the model stays constrained either way, while the reference queries that score episodes
  read columns the ontology never declared (`withdraw.amount`, `sign_in.ts`). Dropping
  them would make gold answers unanswerable.

`_workspace_id` is stamped on every node and relationship because every query in the
harness is workspace-scoped (`agent_interaction.py:74`, `WS = "default"`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

WORKSPACE_DEFAULT = "default"

# parquet type -> neo4j-admin CSV type
_TYPE_MAP = {
    "BIGINT": "long", "HUGEINT": "long", "INTEGER": "int", "SMALLINT": "int",
    "TINYINT": "int", "DOUBLE": "double", "FLOAT": "double", "DECIMAL": "double",
    "BOOLEAN": "boolean", "VARCHAR": "string", "DATE": "string", "TIMESTAMP": "string",
}

# label -> (parquet stem, id space, id column in parquet, id property name in the graph,
#           extra derived columns as (csv_header, sql_expr))
NODE_SPECS: Dict[str, Tuple[str, str, str, str, Sequence[Tuple[str, str]]]] = {
    # Account keeps both identity keys the ontology declares: `id` (STRING, unique — the
    # import id) and `acct_no` (INTEGER — what every question names).
    "Account": ("Account", "Account", "id", "id", (("acct_no:long", "id"),)),
    "Person": ("Person", "Party", "id", "id", ()),
    "Company": ("Company", "Party", "id", "id", ()),
    "Loan": ("Loan", "Loan", "id", "id", ()),
    "Channel": ("Channel", "Channel", "code", "code", ()),
    "Medium": ("Medium", "Medium", "id", "id", ()),
}

# rel type -> (parquet stem, start id space, end id space)
REL_SPECS: Dict[str, Tuple[str, str, str]] = {
    "TRANSFER": ("transfer", "Account", "Account"),
    "OWN": ("own", "Party", "Account"),
    "DEPOSIT": ("deposit", "Loan", "Account"),
    "REPAY": ("repay", "Account", "Loan"),
    "USES_CHANNEL": ("uses_channel", "Account", "Channel"),
    "APPLY": ("apply", "Party", "Loan"),
    "INVEST": ("invest", "Party", "Party"),
    "GUARANTEE": ("guarantee", "Party", "Party"),
    "WITHDRAW": ("withdraw", "Account", "Account"),
    "SIGN_IN": ("sign_in", "Medium", "Account"),
}


def _columns(con: Any, path: Path) -> List[Tuple[str, str]]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')").fetchall()
    return [(r[0], r[1].upper()) for r in rows]


def _csv_type(duck_type: str) -> str:
    base = duck_type.split("(")[0].strip().upper()
    return _TYPE_MAP.get(base, "string")


def _copy(con: Any, select: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({select}) TO '{dest.as_posix()}' (FORMAT CSV, HEADER, DELIMITER ',')")
    return con.execute(f"SELECT count(*) FROM ({select})").fetchone()[0]


def _node_select(con: Any, src: Path, label: str, workspace_id: str) -> str:
    stem, space, id_col, id_prop, extras = NODE_SPECS[label]
    path = src / "nodes" / f"{stem}.parquet"
    cols = _columns(con, path)
    # The :ID column doubles as a property (a named column is stored, unlike a bare :ID),
    # and it must be a string: the id spaces mix integer party ids with Channel codes.
    parts = [f'CAST(n."{id_col}" AS VARCHAR) AS "{id_prop}:ID({space})"']
    for header, expr in extras:
        parts.append(f'n."{expr}" AS "{header}"')
    for name, duck_type in cols:
        if name == id_col:
            continue
        parts.append(f'n."{name}" AS "{name}:{_csv_type(duck_type)}"')

    joins = ""
    if label == "Account":
        # `_out_degree` is not decoration: the episode harness picks each run's anchor
        # account by it (`agent_interaction.py:1015` — p99 of out-degree, then the lowest
        # acct_no at or above it). Without the property every question is asked about
        # anchor None, which is a silently empty run rather than a failure.
        transfer = (src / "edges" / "transfer.parquet").as_posix()
        parts.append('COALESCE(d.c, 0) AS "_out_degree:long"')
        joins = (f" LEFT JOIN (SELECT src, count(*) AS c FROM read_parquet('{transfer}') "
                 f"GROUP BY src) d ON d.src = n.\"{id_col}\"")

    parts.append(f"'{workspace_id}' AS \"_workspace_id:string\"")
    parts.append(f"'{label}' AS \":LABEL\"")
    return (f"SELECT {', '.join(parts)} FROM read_parquet('{path.as_posix()}') n{joins}")


def _rel_select(con: Any, src: Path, rel_type: str, workspace_id: str) -> str:
    stem, start_space, end_space = REL_SPECS[rel_type]
    path = src / "edges" / f"{stem}.parquet"
    cols = _columns(con, path)
    parts = [
        f'CAST("src" AS VARCHAR) AS ":START_ID({start_space})"',
        f'CAST("dst" AS VARCHAR) AS ":END_ID({end_space})"',
    ]
    for name, duck_type in cols:
        if name in ("src", "dst"):
            continue
        parts.append(f'"{name}" AS "{name}:{_csv_type(duck_type)}"')
    parts.append(f"'{workspace_id}' AS \"_workspace_id:string\"")
    parts.append(f"'{rel_type}' AS \":TYPE\"")
    return f"SELECT {', '.join(parts)} FROM read_parquet('{path.as_posix()}')"


def _hub_threshold(con: Any, src: Path) -> Dict[str, Any]:
    """Out-degree tail of TRANSFER — the hub the 'hub defense' arm is defending against.

    Reported rather than acted on: the loader does not reshape the graph, but a plan that
    expands from an unindexed hub is the failure this number predicts.
    """
    path = (src / "edges" / "transfer.parquet").as_posix()
    row = con.execute(
        f"""WITH od AS (SELECT src, count(*) AS c FROM read_parquet('{path}') GROUP BY src)
            SELECT max(c), quantile_cont(c, 0.999), avg(c) FROM od"""
    ).fetchone()
    return {
        "max_out_degree": int(row[0] or 0),
        "p999_out_degree": int(row[1] or 0),
        "mean_out_degree": round(float(row[2] or 0.0), 3),
    }


def export(src: Path, out: Path, *, workspace_id: str = WORKSPACE_DEFAULT) -> Dict[str, Any]:
    """Write neo4j-admin CSVs for every label and relationship type present in `src`."""
    import duckdb

    src = Path(src)
    out = Path(out)
    con = duckdb.connect()
    files: Dict[str, int] = {}
    skipped: List[str] = []

    for label in NODE_SPECS:
        stem = NODE_SPECS[label][0]
        if not (src / "nodes" / f"{stem}.parquet").exists():
            skipped.append(f"nodes/{stem}")
            continue
        select = _node_select(con, src, label, workspace_id)
        files[f"nodes/{label}.csv"] = _copy(con, select, out / "nodes" / f"{label}.csv")

    for rel_type in REL_SPECS:
        stem = REL_SPECS[rel_type][0]
        if not (src / "edges" / f"{stem}.parquet").exists():
            skipped.append(f"edges/{stem}")
            continue
        select = _rel_select(con, src, rel_type, workspace_id)
        files[f"rels/{rel_type}.csv"] = _copy(con, select, out / "rels" / f"{rel_type}.csv")

    hub = _hub_threshold(con, src) if (src / "edges" / "transfer.parquet").exists() else {}
    con.close()

    return {
        "schema_version": "seocho.finbench.admin-csv.v1",
        "src": str(src), "dest": str(out),
        "workspace_id": workspace_id,
        "files": files,
        "skipped": skipped,
        "hub_threshold": hub.get("p999_out_degree"),
        "degree": hub,
    }


def import_command(database: str, remote_dir: str, *, extra_args: Sequence[str] = ()) -> str:
    """The `neo4j-admin database import full` invocation for an exported CSV directory.

    Built from the spec tables, so it stays in step with `export()` without reading disk —
    `bulk_load` runs the export locally and the import inside the container, where the
    local paths do not exist.
    """
    args = [
        "neo4j-admin", "database", "import", "full", database,
        "--overwrite-destination", "--id-type=string", "--verbose",
    ]
    args += [f"--nodes={remote_dir}/nodes/{label}.csv" for label in NODE_SPECS]
    args += [f"--relationships={remote_dir}/rels/{rel}.csv" for rel in REL_SPECS]
    args += list(extra_args)
    return " ".join(args)


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", type=Path, required=True, help="snapshot dir, e.g. outputs/finbench/sf1")
    p.add_argument("--out", type=Path, required=True, help="CSV destination dir")
    p.add_argument("--workspace", default=WORKSPACE_DEFAULT)
    p.add_argument("--print-command", metavar="DATABASE")
    p.add_argument("--remote-dir", default="/var/lib/neo4j/import/csv")
    args = p.parse_args()

    summary = export(args.src, args.out, workspace_id=args.workspace)
    print(json.dumps(summary, indent=2))
    if args.print_command:
        print(import_command(args.print_command, args.remote_dir))


if __name__ == "__main__":
    main()
