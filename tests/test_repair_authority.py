import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "repair_authority", Path(__file__).resolve().parents[1] / "scripts" / "analysis" /
    "analyze_repair_authority.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RepairAuthorityTest(unittest.TestCase):
    def test_pairs_only_identical_cells_and_reports_incremental_cost(self):
        endpoint = {"provider": "mara", "model_name": "test"}
        base = {"sf": 1, "question_id": "q", "repeat": 0, "correct": True,
                "model_calls": 3, "prompt_tokens": 10, "completion_tokens": 2,
                "graph_trips": 1, "db_hits": 5, "db_ms": 1, "wall_ms": 9,
                "decisions": {"initial_cypher": "MATCH"}}
        auto = dict(base, model_calls=4, prompt_tokens=14, graph_trips=2, db_hits=8,
                    repair_loop={"verifier_requested": True, "repair_applied": True,
                                 "repair_model_calls": 1, "repair_api_elapsed_ms": 4})
        output = MODULE.analyze({"endpoint": endpoint, "samples": [auto]},
                                {"endpoint": endpoint, "samples": [base]})
        self.assertEqual(output["overall"]["model_calls_delta"], 1.0)
        self.assertEqual(output["overall"]["db_hits_delta"], 3.0)
        self.assertEqual(output["overall"]["repair_applied_n"], 1)


if __name__ == "__main__":
    unittest.main()
