import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOPOLOGY = load("topology_repair_test", "scripts/benchmarks/bench_agent_topology.py")
ANALYSIS = load("repair_ledger_test", "scripts/analysis/analyze_repair_loop_ledger.py")


class RepairLoopLedgerTest(unittest.TestCase):
    def test_harness_owns_scope_and_user_parameters_remain_bound(self):
        bound = TOPOLOGY._request_params(
            {"params": {"risk_weight": 2, "acct_no": "anchor", "limit": 999}},
            anchor=108, row_cap=50)
        self.assertEqual(bound["risk_weight"], 2)
        self.assertEqual(bound["acct_no"], 108)
        self.assertEqual(bound["limit"], 50)
        self.assertEqual(bound["workspace_id"], "default")
        self.assertEqual("risk 2 for 108".format(**bound), "risk 2 for 108")

    def test_repair_ledger_charges_only_incremental_work(self):
        ledger = TOPOLOGY._repair_ledger(
            stages=[
                {"stage": "planner", "elapsed_ms": 10, "prompt_tokens": 1},
                {"stage": "executor", "elapsed_ms": 20, "prompt_tokens": 2},
                {"stage": "executor", "elapsed_ms": 30, "prompt_tokens": 3,
                 "completion_tokens": 4},
                {"stage": "repair_executor", "elapsed_ms": 40, "prompt_tokens": 5,
                 "completion_tokens": 6},
            ],
            executions=[{"db_hits": 7, "elapsed_ms": 8}, {"db_hits": 9, "elapsed_ms": 10}],
            initial_correct=False, final_correct=True, verifier_requested=True,
            verifier_mode="auto", repair_applied=True, repair_elapsed_ms=55.5)
        self.assertEqual(ledger["validator_retry_generations"], 1)
        self.assertEqual(ledger["verifier_repair_generations"], 1)
        self.assertEqual(ledger["repair_model_calls"], 2)
        self.assertEqual(ledger["repair_api_elapsed_ms"], 70.0)
        self.assertEqual(ledger["repair_graph_trips"], 1)
        self.assertEqual(ledger["repair_db_hits"], 9)
        self.assertTrue(ledger["converged_to_correct"])

    def test_analysis_keeps_gpu_cost_out_of_hosted_ledger(self):
        output = ANALYSIS.analyze({
            "run_id": "unit", "endpoint": {"provider": "mara"},
            "samples": [{
                "episode_id": "e", "question_id": "q", "repeat": 0, "sf": 1,
                "arm": "multi_typed", "correct": True, "wall_ms": 90,
                "prompt_tokens": 10, "completion_tokens": 5, "graph_trips": 2,
                "db_hits": 12, "db_ms": 3, "stages": [{"elapsed_ms": 70}],
                "request": {"request_type": "aml_alert_triage", "schema_facets": ["TRANSFER"]},
                "repair_loop": {"repair_model_calls": 1, "repair_api_elapsed_ms": 20,
                                "repair_prompt_tokens": 2, "repair_graph_trips": 1,
                                "repair_db_hits": 4, "repair_db_ms": 1},
            }],
        })
        self.assertEqual(output["overall"]["repair_model_work_n"], 1)
        self.assertEqual(output["overall"]["repair_db_hits_total"], 4)
        self.assertEqual(output["method"]["gpu_cost"], "not observed or inferred for hosted MARA")

    def test_analysis_backfills_legacy_error_request_metadata_and_wall_time(self):
        output = ANALYSIS.analyze({
            "samples": [{
                "episode_id": "e", "question_id": "q", "repeat": 0, "sf": 1,
                "arm": "multi_typed", "correct": False, "error": "validator",
                "stages": [{"stage": "executor", "elapsed_ms": 4}],
                "monitor_window": {"started_mono": 10.0, "ended_mono": 10.025},
            }],
        }, {"q": {"request_type": "aml_alert_triage", "schema_facets": ["TRANSFER"]}})
        row = output["episodes"][0]
        self.assertEqual(row["request_type"], "aml_alert_triage")
        self.assertEqual(row["request_wall_ms"], 25.0)


if __name__ == "__main__":
    unittest.main()
