import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "local_db_analysis",
    Path(__file__).resolve().parents[1] / "scripts" / "analysis" /
    "analyze_local_db_monitor.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalDbMonitorAnalysisTest(unittest.TestCase):
    def test_overlap_uses_sample_interval_not_cross_process_clock_math(self) -> None:
        sample = {"sample_started_mono": 9.0, "t_mono": 11.0}
        self.assertTrue(MODULE._overlaps(
            sample, {"started_mono": 10.0, "ended_mono": 12.0}))
        self.assertFalse(MODULE._overlaps(
            sample, {"started_mono": 12.1, "ended_mono": 13.0}))

    def test_cumulative_counter_reset_is_not_reported_as_negative_io(self) -> None:
        def sample(value):
            return {"db_container": {"counters": {"block_read_bytes": value}}}

        self.assertEqual(MODULE._counter_delta(
            [sample(100), sample(160)], "block_read_bytes"), 60)
        self.assertIsNone(MODULE._counter_delta(
            [sample(100), sample(20)], "block_read_bytes"))


if __name__ == "__main__":
    unittest.main()
