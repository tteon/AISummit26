"""Durable local host and database-container sampling for benchmark correlation.

Samples use both wall and process-local monotonic clocks. Raw cumulative counters are kept
instead of precomputed rates so analysis can choose intervals without mixing denominators.
Hosted model-server telemetry is deliberately out of scope: the sampler records only the
client/driver host and the explicitly named local database container.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


_SIZE_MULTIPLIERS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000 ** 2,
    "gb": 1000 ** 3,
    "tb": 1000 ** 4,
    "kib": 1024,
    "mib": 1024 ** 2,
    "gib": 1024 ** 3,
    "tib": 1024 ** 4,
}


def _size_bytes(value: str) -> Optional[int]:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)\s*", value,
                         flags=re.IGNORECASE)
    if not match:
        return None
    return round(float(match.group(1)) * _SIZE_MULTIPLIERS[match.group(2).lower()])


def _io_pair(value: str) -> tuple[Optional[int], Optional[int]]:
    left, separator, right = value.partition("/")
    if not separator:
        return None, None
    return _size_bytes(left), _size_bytes(right)


def _normalized_container_stats(payload: Dict[str, Any]) -> Dict[str, Any]:
    memory_used, memory_limit = _io_pair(str(payload.get("MemUsage", "")))
    network_rx, network_tx = _io_pair(str(payload.get("NetIO", "")))
    block_read, block_write = _io_pair(str(payload.get("BlockIO", "")))

    def percent(key: str) -> Optional[float]:
        raw = str(payload.get(key, "")).strip().removesuffix("%")
        try:
            return float(raw)
        except ValueError:
            return None

    try:
        pids = int(payload.get("PIDs"))
    except (TypeError, ValueError):
        pids = None
    return {
        "cpu_percent": percent("CPUPerc"),
        "memory_percent": percent("MemPerc"),
        "memory_used_bytes": memory_used,
        "memory_limit_bytes": memory_limit,
        "network_rx_bytes": network_rx,
        "network_tx_bytes": network_tx,
        "block_read_bytes": block_read,
        "block_write_bytes": block_write,
        "pids": pids,
    }


def _read_key_values(path: str) -> Dict[str, int]:
    values: Dict[str, int] = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, _, raw = line.partition(":")
            token = raw.strip().split()[0] if raw.strip() else ""
            if token.isdigit():
                values[key] = int(token)
    except OSError:
        pass
    return values


def _host_sample() -> Dict[str, Any]:
    sample: Dict[str, Any] = {}
    try:
        first = Path("/proc/stat").read_text().splitlines()[0].split()
        sample["cpu_ticks"] = {name: int(value) for name, value in zip(
            ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"),
            first[1:9])}
    except (OSError, ValueError, IndexError):
        sample["cpu_ticks"] = None
    try:
        one, five, fifteen = os.getloadavg()
        sample["loadavg"] = {"one": one, "five": five, "fifteen": fifteen}
    except OSError:
        sample["loadavg"] = None
    memory = _read_key_values("/proc/meminfo")
    sample["memory_kib"] = {key: memory.get(key) for key in (
        "MemTotal", "MemAvailable", "Buffers", "Cached", "SwapTotal", "SwapFree")}
    process = _read_key_values("/proc/self/status")
    sample["client_process_kib"] = {key: process.get(key) for key in (
        "VmRSS", "VmHWM", "VmSize")}
    return sample


def _container_sample(container: Optional[str]) -> Dict[str, Any]:
    if not container:
        return {"available": False, "reason": "no container configured"}
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
            text=True, capture_output=True, timeout=5, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"available": False,
                    "reason": (result.stderr.strip() or f"exit {result.returncode}")[:300]}
        payload = json.loads(result.stdout.splitlines()[0])
        return {"available": True, "name": container,
                "identity": {key: payload.get(key) for key in ("Container", "ID", "Name")},
                "counters": _normalized_container_stats(payload),
                "raw": payload}
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:250]}"}


class SystemMetricsSampler:
    """Background JSONL sampler with an fsync durability boundary per observation."""

    def __init__(self, path: Path, *, interval_s: float = 5.0,
                 db_container: Optional[str] = None) -> None:
        self.path = path
        self.interval_s = max(0.25, float(interval_s))
        self.db_container = db_container
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.samples = 0
        self.container_unavailable = 0

    def probe(self) -> Dict[str, Any]:
        """Check the database container before any paid model request is sent."""
        return _container_sample(self.db_container)

    def start(self) -> None:
        if self._thread is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="system-metrics", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        with self.path.open("a", encoding="utf-8") as out:
            while not self._stop.is_set():
                sample_started_wall = time.time()
                sample_started_mono = time.monotonic()
                container = _container_sample(self.db_container)
                record = {
                    "schema_version": "seocho.system-metrics.v2",
                    "t_wall": time.time(), "t_mono": time.monotonic(),
                    "sample_started_wall": sample_started_wall,
                    "sample_started_mono": sample_started_mono,
                    "sample_duration_ms": round(
                        (time.monotonic() - sample_started_mono) * 1000, 3),
                    "host": _host_sample(), "db_container": container,
                }
                out.write(json.dumps(record, default=str) + "\n")
                out.flush()
                os.fsync(out.fileno())
                self.samples += 1
                self.container_unavailable += int(not container.get("available", False))
                elapsed = time.monotonic() - sample_started_mono
                self._stop.wait(max(0.0, self.interval_s - elapsed))

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 6)
        thread_complete = (self._thread is not None and not self._thread.is_alive())
        durable_complete = bool(self.samples) and thread_complete
        db_container_complete = durable_complete and self.container_unavailable == 0
        return {
            "schema_version": "seocho.system-monitor-receipt.v2",
            "path": str(self.path), "interval_s": self.interval_s,
            "db_container": self.db_container, "samples": self.samples,
            "db_container_available_samples": self.samples - self.container_unavailable,
            "container_unavailable_samples": self.container_unavailable,
            "coverage_rate": round(
                (self.samples - self.container_unavailable) / self.samples, 6)
                if self.samples else 0.0,
            "complete": durable_complete,
            "db_container_complete": db_container_complete,
            "valid": db_container_complete,
        }
