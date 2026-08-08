"""One manifest per measurement run, so a number can always be traced to its machine.

Every benchmark writes its results as JSON with this manifest attached. The rule it serves:
a metric that cannot name the commit, the decoder, the container image and the box it ran on
is an anecdote, not a measurement. Import and call `manifest()`; everything is best-effort —
a field that cannot be collected records its absence rather than failing the run.
"""
from __future__ import annotations

import datetime
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


def _cmd(*args: str) -> str | None:
    try:
        return subprocess.check_output(list(args), text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _pkg(name: str) -> str | None:
    try:
        import importlib.metadata as im
        return im.version(name)
    except Exception:
        return None


def _decoder() -> str:
    try:
        import neo4j._codec.packstream.v1 as v1
        return "rust" if getattr(v1, "_rust_unpack", None) is not None else "pure-python"
    except Exception:
        return "unknown"


def _cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def manifest(db_container: str = "graphrag-neo4j", **extra: Any) -> Dict[str, Any]:
    repo = Path(__file__).resolve().parent.parent
    return {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc)
                                          .isoformat(timespec="seconds"),
        "git_commit": _cmd("git", "-C", str(repo), "rev-parse", "HEAD"),
        "git_dirty": bool(_cmd("git", "-C", str(repo), "status", "--porcelain")),
        "argv": sys.argv,
        "python": sys.version.split()[0],
        "venv": sys.prefix,
        "kernel": platform.release(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "neo4j_driver": _pkg("neo4j"),
        "neo4j_rust_ext": _pkg("neo4j-rust-ext"),
        "packstream_decoder": _decoder(),
        "db_container_image": _cmd("docker", "inspect", "--format",
                                   "{{.Config.Image}}", db_container),
        "db_container_id": (_cmd("docker", "inspect", "--format", "{{.Id}}",
                                 db_container) or "")[:12] or None,
        **extra,
    }
