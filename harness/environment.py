"""Hardware and Software Environment Collector for Benchmark Reproducibility."""
from __future__ import annotations

import os
import sys
import platform
import subprocess
import datetime
from pathlib import Path
from typing import Any, Dict


def _run_cmd(*args: str) -> str | None:
    try:
        return subprocess.check_output(list(args), text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _get_pkg_version(name: str) -> str | None:
    try:
        import importlib.metadata as im
        return im.version(name)
    except Exception:
        return None


def get_hardware_info() -> Dict[str, Any]:
    """Collects detailed hardware environment specifications."""
    cpu_model = "Unknown CPU"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except Exception:
        cpu_model = platform.processor() or "Unknown CPU"

    mem_total_gb = None
    mem_avail_gb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total_gb = round(int(line.split()[1]) / (1024 * 1024), 2)
            elif line.startswith("MemAvailable:"):
                mem_avail_gb = round(int(line.split()[1]) / (1024 * 1024), 2)
    except Exception:
        pass

    gpu_info = _run_cmd("nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader")

    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "cpu_model": cpu_model,
        "cpu_logical_cores": os.cpu_count(),
        "memory_total_gb": mem_total_gb,
        "memory_available_gb": mem_avail_gb,
        "gpu": gpu_info or "None / CPU Only",
    }


def get_software_info(repo_root: Path) -> Dict[str, Any]:
    """Collects software, package, and git metadata."""
    git_commit = _run_cmd("git", "-C", str(repo_root), "rev-parse", "HEAD")
    git_branch = _run_cmd("git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD")
    git_dirty = bool(_run_cmd("git", "-C", str(repo_root), "status", "--porcelain"))

    return {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_commit,
        "git_branch": git_branch,
        "git_dirty": git_dirty,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "virtual_env": sys.prefix,
        "packages": {
            "seocho": _get_pkg_version("seocho"),
            "duckdb": _get_pkg_version("duckdb"),
            "openai": _get_pkg_version("openai"),
            "neo4j": _get_pkg_version("neo4j"),
            "pyyaml": _get_pkg_version("pyyaml"),
            "pydantic": _get_pkg_version("pydantic"),
        },
        "db_container_image": _run_cmd("docker", "inspect", "--format", "{{.Config.Image}}", "graphrag-neo4j"),
    }
