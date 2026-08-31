import importlib.util
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("traversal",ROOT/"scripts/analysis/validate_finbench_traversal_contract.py")
M=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(M)
class TestTraversalContract(unittest.TestCase):
 def test_contract(self): self.assertTrue(M.validate(ROOT/"configs/finbench_agent_traversal_contract_v1.yaml")["valid"])
 def test_bounded_policy_requires_order(self):
  p=Path("/tmp/no-order.yaml"); p.write_text((ROOT/"configs/finbench_agent_traversal_contract_v1.yaml").read_text().replace("truncation_order: TIMESTAMP_DESCENDING", "truncation_order: null")); self.assertFalse(M.validate(p)["valid"])
if __name__=="__main__": unittest.main()
