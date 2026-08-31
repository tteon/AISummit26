import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("contracts", ROOT / "scripts/analysis/validate_request_schema_contracts.py")
CONTRACTS = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(CONTRACTS)


class RequestSchemaContractsTest(unittest.TestCase):
    def test_versioned_catalog_matches_finbench(self):
        report = CONTRACTS.validate(ROOT / "configs/agentic_request_schema_contracts_v2.yaml",
                                    ROOT / "ontology/finbench.ontology.yaml",
                                    ROOT / "ontology/fibo_finbench.projection.yaml")
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["counts"]["requests"], 12)

    def test_business_alias_is_not_accepted_as_physical_label(self):
        catalog = ROOT / "configs/agentic_request_schema_contracts_v2.yaml"
        text = catalog.read_text().replace("nodes: [Account]", "nodes: [Transaction]", 1)
        bad = Path("/tmp/invalid-request-contract.yaml")
        bad.write_text(text)
        report = CONTRACTS.validate(bad, ROOT / "ontology/finbench.ontology.yaml",
                                    ROOT / "ontology/fibo_finbench.projection.yaml")
        self.assertFalse(report["valid"])
        self.assertIn("inbound_amount_band: unknown physical node Transaction", report["errors"])


if __name__ == "__main__":
    unittest.main()
