#!/usr/bin/env python3
"""Checkpoint a testbed run to S3 — datasets in, results out, resume log continuously.

A rented instance is not storage. It can be reclaimed mid-run, and everything on it goes
with it, so the rule this script enforces is: nothing that cost GPU minutes exists only on
the instance. Three kinds of object, with three different lifetimes:

  datasets/finbench/sf<N>/<manifest_sha>/   immutable, content-addressed, re-used across
                                            rentals — 40MB of parquet is cheaper to keep
                                            than to regenerate on a metered GPU box
  runs/<run_id>/...                          the run's manifest, metrics and figures
  runs/<run_id>/episodes.jsonl               the append-only resume log, synced *while the
                                            run is still going* (see `watch`)

The dataset key is the sha256 of the snapshot's own `manifest.json`, which already carries
the generator seed and a sha256 per parquet file. So the key changes if and only if the
data changes, and two runs that name the same key provably read the same graph.

    export S3_BUCKET=my-bucket S3_PREFIX=aisummit26 AWS_REGION=ap-northeast-2
    python3 scripts/testbed/s3_ckpt.py push-datasets --src outputs/finbench/sf1
    python3 scripts/testbed/s3_ckpt.py pull-datasets --sf 1 --dest outputs/finbench
    python3 scripts/testbed/s3_ckpt.py watch --path results/episodes/run.json.jsonl \\
        --run-id 20260821_h200_prefix_on --interval 60 &
    python3 scripts/testbed/s3_ckpt.py push --run-id 20260821_h200_prefix_on \\
        --path results/runs/20260821_h200_prefix_on
    python3 scripts/testbed/s3_ckpt.py verify --run-id 20260821_h200_prefix_on \\
        --path results/runs/20260821_h200_prefix_on    # run this BEFORE destroying
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_PREFIX = "aisummit26"


def _client():
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.exit("boto3 is required: pip install -r testbed/requirements.txt "
                 "(or pip install boto3)")
    import boto3
    session_kwargs: Dict[str, Any] = {}
    if os.getenv("AWS_PROFILE"):
        session_kwargs["profile_name"] = os.getenv("AWS_PROFILE")
    if os.getenv("AWS_REGION"):
        session_kwargs["region_name"] = os.getenv("AWS_REGION")
    return boto3.Session(**session_kwargs).client("s3")


def _bucket(args) -> str:
    bucket = args.bucket or os.getenv("S3_BUCKET")
    if not bucket:
        sys.exit("no bucket: pass --bucket or set S3_BUCKET")
    return bucket


def _prefix(args) -> str:
    return (args.prefix or os.getenv("S3_PREFIX") or DEFAULT_PREFIX).strip("/")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dataset_key(src: Path) -> str:
    """Content address of a snapshot: sha256 of its manifest (seed + per-file checksums)."""
    manifest = src / "manifest.json"
    if not manifest.exists():
        sys.exit(f"{src} has no manifest.json — not a generated snapshot")
    doc = json.loads(manifest.read_text())
    digest = _sha256_file(manifest)[:16]
    return f"sf{doc.get('scale_factor', '?')}/{digest}"


def _walk(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    for p in sorted(path.rglob("*")):
        if p.is_file():
            yield p


def _upload(s3, bucket: str, local: Path, key: str, *, base: Optional[Path] = None) -> List[Tuple[str, int]]:
    sent = []
    for f in _walk(local):
        rel = f.relative_to(base or local) if (base or local) != f else Path(f.name)
        remote = f"{key}/{rel.as_posix()}" if (base or local) != f else key
        s3.upload_file(str(f), bucket, remote)
        sent.append((remote, f.stat().st_size))
    return sent


def cmd_push_datasets(args) -> None:
    s3, bucket, prefix = _client(), _bucket(args), _prefix(args)
    for src in args.src:
        src = Path(src)
        key = f"{prefix}/datasets/finbench/{dataset_key(src)}"
        sent = _upload(s3, bucket, src, key, base=src)
        total = sum(size for _, size in sent)
        print(f"[push-datasets] {src} -> s3://{bucket}/{key}/ "
              f"({len(sent)} files, {total/1e6:.1f} MB)")


def cmd_pull_datasets(args) -> None:
    s3, bucket, prefix = _client(), _bucket(args), _prefix(args)
    dest_root = Path(args.dest)
    paginator = s3.get_paginator("list_objects_v2")
    for sf in args.sf:
        base = f"{prefix}/datasets/finbench/sf{sf}/"
        digests = set()
        for page in paginator.paginate(Bucket=bucket, Prefix=base, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                digests.add(cp["Prefix"])
        if not digests:
            sys.exit(f"nothing under s3://{bucket}/{base} — push-datasets first")
        if len(digests) > 1 and not args.digest:
            listed = ", ".join(sorted(d.rstrip("/").rsplit("/", 1)[-1] for d in digests))
            sys.exit(f"sf{sf} has several snapshots ({listed}); pass --digest to choose")
        chosen = (f"{base}{args.digest}/" if args.digest else sorted(digests)[0])
        dest = dest_root / f"sf{sf}"
        count = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=chosen):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(chosen):]
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, obj["Key"], str(out))
                count += 1
        print(f"[pull-datasets] s3://{bucket}/{chosen} -> {dest} ({count} files)")


def cmd_push(args) -> None:
    s3, bucket, prefix = _client(), _bucket(args), _prefix(args)
    key_root = f"{prefix}/runs/{args.run_id}"
    receipt = {"bucket": bucket, "prefix": key_root, "uploaded": [], "ts": int(time.time())}
    for path in args.path:
        local = Path(path)
        if not local.exists():
            sys.exit(f"{local} does not exist")
        key = f"{key_root}/{local.name}" if local.is_dir() else f"{key_root}/{local.name}"
        sent = _upload(s3, bucket, local, key, base=local if local.is_dir() else None)
        receipt["uploaded"].extend(k for k, _ in sent)
        print(f"[push] {local} -> s3://{bucket}/{key} ({len(sent)} files)")
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(receipt, indent=2) + "\n")


def cmd_watch(args) -> None:
    """Sync one growing file on an interval — the resume log, while episodes are running."""
    s3, bucket, prefix = _client(), _bucket(args), _prefix(args)
    local = Path(args.path)
    key = f"{prefix}/runs/{args.run_id}/{local.name}"
    last_size = -1
    print(f"[watch] {local} -> s3://{bucket}/{key} every {args.interval}s", flush=True)
    while True:
        if local.exists():
            size = local.stat().st_size
            if size != last_size:
                s3.upload_file(str(local), bucket, key)
                last_size = size
                print(f"[watch] synced {size/1024:.1f} KiB", flush=True)
        if args.once:
            return
        time.sleep(args.interval)


def cmd_verify(args) -> None:
    """The destroy gate: every local file present in S3 at the same size, or exit 1."""
    s3, bucket, prefix = _client(), _bucket(args), _prefix(args)
    key_root = f"{prefix}/runs/{args.run_id}"
    remote: Dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=key_root + "/"):
        for obj in page.get("Contents", []):
            remote[obj["Key"]] = obj["Size"]

    missing, mismatched, checked = [], [], 0
    for path in args.path:
        local = Path(path)
        base = local if local.is_dir() else None
        for f in _walk(local):
            rel = f.relative_to(base) if base else Path(f.name)
            key = f"{key_root}/{local.name}/{rel.as_posix()}" if base else f"{key_root}/{f.name}"
            checked += 1
            if key not in remote:
                missing.append(key)
            elif remote[key] != f.stat().st_size:
                mismatched.append((key, f.stat().st_size, remote[key]))

    print(f"[verify] {checked} local files, {len(remote)} objects under s3://{bucket}/{key_root}/")
    for key in missing:
        print(f"  MISSING   {key}")
    for key, local_size, remote_size in mismatched:
        print(f"  SIZE      {key} local={local_size} remote={remote_size}")
    if missing or mismatched:
        sys.exit(f"[verify] FAIL — {len(missing)} missing, {len(mismatched)} mismatched. "
                 "Do not destroy the instance.")
    print("[verify] OK — every local file is in S3. Safe to destroy.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=None, help="default from S3_BUCKET")
    ap.add_argument("--prefix", default=None, help=f"default from S3_PREFIX, else {DEFAULT_PREFIX}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("push-datasets", help="upload snapshot dirs, content-addressed")
    p.add_argument("--src", nargs="+", required=True)
    p.set_defaults(func=cmd_push_datasets)

    p = sub.add_parser("pull-datasets", help="download snapshots for the given scale factors")
    p.add_argument("--sf", nargs="+", type=int, required=True)
    p.add_argument("--dest", default="outputs/finbench")
    p.add_argument("--digest", default=None, help="pick one content address explicitly")
    p.set_defaults(func=cmd_pull_datasets)

    p = sub.add_parser("push", help="upload run artifacts")
    p.add_argument("--run-id", required=True)
    p.add_argument("--path", nargs="+", required=True)
    p.add_argument("--receipt", default=None, help="write the uploaded key list here")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("watch", help="periodically sync one growing file (the resume log)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("verify", help="fail unless every local file is in S3 (destroy gate)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--path", nargs="+", required=True)
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
