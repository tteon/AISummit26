import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.system_monitor import (
    SystemMetricsSampler,
    _normalized_container_stats,
    _size_bytes,
)


class SystemMonitorTest(unittest.TestCase):
    def test_docker_units_are_normalized_without_dropping_raw_denominators(self) -> None:
        raw = {
            "CPUPerc": "1.00%", "MemPerc": "13.79%",
            "MemUsage": "8.627GiB / 62.55GiB",
            "NetIO": "9.94kB / 5.48kB", "BlockIO": "99.6MB / 2.01MB",
            "PIDs": "67",
        }
        normalized = _normalized_container_stats(raw)
        self.assertEqual(_size_bytes("1KiB"), 1024)
        self.assertEqual(normalized["cpu_percent"], 1.0)
        self.assertEqual(normalized["memory_used_bytes"], round(8.627 * 1024 ** 3))
        self.assertEqual(normalized["network_rx_bytes"], 9940)
        self.assertEqual(normalized["block_write_bytes"], 2_010_000)
        self.assertEqual(normalized["pids"], 67)

    def test_receipt_requires_database_container_coverage(self) -> None:
        available = {
            "available": True, "name": "db", "identity": {}, "counters": {}, "raw": {}
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system_metrics.jsonl"
            with patch("harness.system_monitor._container_sample", return_value=available):
                sampler = SystemMetricsSampler(path, interval_s=0.25, db_container="db")
                self.assertTrue(sampler.probe()["available"])
                sampler.start()
                deadline = time.monotonic() + 2
                while sampler.samples == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                receipt = sampler.stop()
            self.assertTrue(receipt["complete"])
            self.assertTrue(receipt["db_container_complete"])
            self.assertTrue(receipt["valid"])
            record = json.loads(path.read_text().splitlines()[0])
            self.assertEqual(record["schema_version"], "seocho.system-metrics.v2")
            self.assertIn("sample_duration_ms", record)


if __name__ == "__main__":
    unittest.main()
