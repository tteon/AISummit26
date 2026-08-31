import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "retrieval_ledger",
    Path(__file__).resolve().parents[1] / "scripts" / "analysis" /
    "analyze_retrieval_ledger.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def node(name, rows, estimated, hits=0, children=None):
    return {
        "operator_type": name,
        "arguments": {"Rows": rows, "EstimatedRows": estimated, "DbHits": hits,
                      "PageCacheHits": 0, "PageCacheMisses": 0},
        "children": children or [],
    }


class RetrievalLedgerTest(unittest.TestCase):
    def test_template_fingerprint_ignores_whitespace_but_not_query_shape(self) -> None:
        first = MODULE._cypher_template_fingerprint("MATCH (a)  RETURN a")
        second = MODULE._cypher_template_fingerprint(" MATCH (a)\nRETURN a ")
        third = MODULE._cypher_template_fingerprint("MATCH (a) RETURN count(a)")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_seek_name_with_many_rows_is_a_sweep_by_cost(self) -> None:
        tree = node("ProduceResults@db", 1, 1, children=[
            node("NodeIndexSeek@db", 200_470, 200_470, hits=6_400_000)
        ])
        metrics = MODULE._plan_metrics(tree, 1000)
        self.assertEqual(metrics["access_class"], "sweep")
        self.assertTrue(metrics["seek_operator_name_present"])
        self.assertTrue(metrics["name_cost_disagreement"])

    def test_point_seek_and_expansion_fanout_are_kept_separate(self) -> None:
        tree = node("ProduceResults@db", 20, 20, children=[
            node("Expand(All)@db", 20, 20, hits=50, children=[
                node("NodeIndexSeek@db", 1, 1, hits=2)
            ])
        ])
        metrics = MODULE._plan_metrics(tree, 1000)
        self.assertEqual(metrics["access_class"], "point")
        self.assertEqual(metrics["max_expansion_ratio"], 20.0)
        self.assertEqual(metrics["db_hits_total"], 52)


if __name__ == "__main__":
    unittest.main()
