import importlib.util, unittest
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1];s=importlib.util.spec_from_file_location("a",ROOT/"scripts/analysis/evaluate_agent_contract_adherence.py");M=importlib.util.module_from_spec(s);assert s.loader;s.loader.exec_module(M)
class T(unittest.TestCase):
 def test_gold_is_perfect(self):
  suite=yaml.safe_load((ROOT/"configs/agent_contract_adherence_v1.yaml").read_text()); preds=[{"id":x["id"],**x["gold"]} for x in suite["cases"]]; r=M.evaluate(suite,preds);self.assertEqual(r["metrics"]["policy_conformance"],1)
 def test_unsafe_repair_is_visible(self):
  suite=yaml.safe_load((ROOT/"configs/agent_contract_adherence_v1.yaml").read_text());preds=[{"id":x["id"],**x["gold"]} for x in suite["cases"]];preds[4]["repair_allowed"]=True;r=M.evaluate(suite,preds);self.assertGreater(r["metrics"]["avoidable_repair_rate"],0)
if __name__=="__main__":unittest.main()
