import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("business_mapping", ROOT / "scripts/analysis/validate_business_request_mapping.py")
MODULE = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(MODULE)


class BusinessRequestMappingTest(unittest.TestCase):
    def test_mapping_is_valid(self):
        report = MODULE.validate(ROOT / "ontology/business_request_finbench.mapping.yaml",
                                 ROOT / "ontology/finbench.ontology.yaml",
                                 ROOT / "ontology/fibo_finbench.projection.yaml")
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["counts"]["terms"], 13)

    def test_unsupported_semantics_need_a_typed_refusal(self):
        source = (ROOT / "ontology/business_request_finbench.mapping.yaml").read_text()
        invalid = Path("/tmp/invalid-business-mapping.yaml")
        invalid.write_text(source.replace("refusal_code: UNSUPPORTED_SEMANTIC_FIELD", "# refusal intentionally absent", 1))
        report = MODULE.validate(invalid, ROOT / "ontology/finbench.ontology.yaml",
                                 ROOT / "ontology/fibo_finbench.projection.yaml")
        self.assertFalse(report["valid"])
        self.assertIn("owner_type: unsupported term needs refusal_code and reason", report["errors"])


if __name__ == "__main__":
    unittest.main()
