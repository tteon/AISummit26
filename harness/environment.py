"""Hardware and Software Environment Collector for Benchmark Reproducibility.

Extended for the rented-GPU testbed: when the model is served from a vLLM process this
repo starts, the endpoint's *serving* configuration becomes part of the measurement.
"Warm TTFT improved" is not a reproducible claim unless the manifest also says which GPU,
which vLLM build, whether prefix caching was on, and how much KV cache the server had —
that last one is the actual independent variable in the ontology-as-shared-prefix
experiment. A hosted API cannot report any of it; a local server reports all of it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import platform
import subprocess
import datetime
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        "accelerator": get_accelerator_info(),
        "host_context": get_host_context(),
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


# --------------------------------------------------------------------------------------
# Rented-GPU testbed context
# --------------------------------------------------------------------------------------
_GPU_FIELDS = ("index", "name", "memory.total", "memory.used", "driver_version",
               "compute_cap", "pci.bus_id")


def get_accelerator_info() -> Dict[str, Any]:
    """Per-GPU identity and free memory, or an explicit absence.

    Free memory matters as much as the model: the serve script gates on it, and how much
    is left after weights load is the KV-cache budget the prefix-cache experiment spends.
    """
    query = ",".join(_GPU_FIELDS)
    out = _run_cmd("nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits")
    if not out:
        return {"present": False, "reason": "nvidia-smi unavailable or no driver"}
    gpus: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_GPU_FIELDS):
            continue
        gpus.append(dict(zip(_GPU_FIELDS, parts)))
    return {
        "present": bool(gpus),
        "count": len(gpus),
        "gpus": gpus,
        "cuda_visible_devices": os.getenv("CUDA_VISIBLE_DEVICES"),
    }


# vast.ai injects these into the instance; keeping them means a number can be traced back
# to the machine that was rented, which is gone by the time anyone reads the chart.
_RENTAL_ENV = ("VAST_CONTAINERLABEL", "CONTAINER_ID", "CONTAINER_API_KEY_ID", "VAST_TCP_PORT_22",
               "PUBLIC_IPADDR", "DATA_DIRECTORY", "RUNPOD_POD_ID", "TESTBED_LABEL")


def get_host_context() -> Dict[str, Any]:
    ctx = {k: os.getenv(k) for k in _RENTAL_ENV if os.getenv(k)}
    ctx["in_container"] = Path("/.dockerenv").exists()
    ctx["provider_hint"] = ("vast.ai" if os.getenv("VAST_CONTAINERLABEL")
                            else "runpod" if os.getenv("RUNPOD_POD_ID")
                            else "local")
    return ctx


def _http_get(url: str, timeout: float = 3.0) -> Optional[str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


# Metric names have moved between vLLM releases (gpu_prefix_cache_hit_rate became
# queries/hits counters in V1), so the filter is by keyword and whatever exists is kept
# verbatim rather than mapped onto names this repo guesses at.
_METRIC_KEYWORDS = ("cache_config_info", "prefix_cache", "gpu_cache_usage", "kv_cache",
                    "kv_block", "num_requests", "time_to_first_token", "prefill",
                    "queue_time", "prompt_tokens", "generation_tokens")


def scrape_vllm_metrics(base_url: str, timeout: float = 3.0) -> Dict[str, Any]:
    """Prometheus lines a vLLM server exposes that describe caching and prefill."""
    root = base_url.rstrip("/")
    root = root[: -len("/v1")] if root.endswith("/v1") else root
    text = _http_get(f"{root}/metrics", timeout)
    if text is None:
        return {"reachable": False}
    lines = [ln for ln in text.splitlines()
             if ln and not ln.startswith("#") and "_created" not in ln
             and any(k in ln for k in _METRIC_KEYWORDS)]
    return {"reachable": True, "endpoint": f"{root}/metrics", "lines": lines}


def get_inference_info(base_url: Optional[str] = None, *, model_descriptor: Optional[Dict[str, Any]] = None,
                       timeout: float = 3.0) -> Dict[str, Any]:
    """Endpoint identity plus, when it is a local vLLM, its serving configuration."""
    info: Dict[str, Any] = {"model": model_descriptor}
    base_url = base_url or (model_descriptor or {}).get("base_url")
    if not base_url:
        return info

    served = _http_get(f"{base_url.rstrip('/')}/models", timeout)
    if served:
        try:
            info["served_models"] = [m.get("id") for m in json.loads(served).get("data", [])]
        except (ValueError, AttributeError):
            info["served_models"] = None

    info["vllm_version"] = _get_pkg_version("vllm")
    # The serve flags are the experiment's independent variables (--enable-prefix-caching
    # above all), and they exist only in the command line that started the server.
    cmdline = _run_cmd("bash", "-lc",
                       "ps -eo args= | grep -E '(vllm serve|vllm.entrypoints)' | grep -v grep | head -1")
    if cmdline:
        info["server_cmdline"] = cmdline
        flags = dict(re.findall(r"--([a-z0-9-]+)(?:[= ]([^\s-][^\s]*))?", cmdline))
        info["server_flags"] = flags
        info["prefix_caching"] = (
            False if "no-enable-prefix-caching" in cmdline
            else True if "enable-prefix-caching" in cmdline
            else "default")
    info["metrics"] = scrape_vllm_metrics(base_url, timeout)
    return info
