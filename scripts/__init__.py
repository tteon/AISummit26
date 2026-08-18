"""Scripts package root - auto-bootstrapping subdirectories into sys.path."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in ["scripts", "scripts/benchmarks", "scripts/agents", "scripts/analysis", "scripts/data", "scripts/plotting", "scripts/smoke"]:
    p = str(REPO_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
