"""Durable local host and database-container sampling for benchmark correlation.

Samples use both wall and process-local monotonic clocks. Raw cumulative counters are kept
instead of precomputed rates so analysis can choose intervals without mixing denominators.
Hosted model-server telemetry is deliberately out of scope: the sampler records only the
client/driver host and the explicitly named local database container.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


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
        return {"available": True, "name": container, "stats": payload}
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

    def start(self) -> None:
        if self._thread is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="system-metrics", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        with self.path.open("a", encoding="utf-8") as out:
            while not self._stop.is_set():
                container = _container_sample(self.db_container)
                record = {
                    "schema_version": "seocho.system-metrics.v1",
                    "t_wall": time.time(), "t_mono": time.monotonic(),
                    "host": _host_sample(), "db_container": container,
                }
                out.write(json.dumps(record, default=str) + "\n")
                out.flush()
                os.fsync(out.fileno())
                self.samples += 1
                self.container_unavailable += int(not container.get("available", False))
                self._stop.wait(self.interval_s)

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 6)
        return {
            "schema_version": "seocho.system-monitor-receipt.v1",
            "path": str(self.path), "interval_s": self.interval_s,
            "db_container": self.db_container, "samples": self.samples,
            "container_unavailable_samples": self.container_unavailable,
            "complete": bool(self.samples) and self._thread is not None and
                        not self._thread.is_alive(),
        }
