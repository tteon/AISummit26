"""Benchmark Configuration Parser."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from dataclasses import dataclass, field


@dataclass
class DatasetConfig:
    engine: str = "duckdb"
    scales: List[int] = field(default_factory=lambda: [1, 10, 100])
    base_ontology: str = "ontology/finbench.ontology.yaml"
    fibo_ontology: str = "ontology/fibo_finbench.ontology.yaml"
    output_dir: str = "outputs/finbench"


@dataclass
class ModelConfig:
    provider: str = "mara"
    model_name: str = "gpt-oss-120b"
    temperature: float = 0.0
    max_tokens: int = 1000
    base_url: Optional[str] = None
    api_key: Optional[str] = None

    def __post_init__(self):
        if not self.base_url:
            self.base_url = os.getenv("MARA_BASE_URL", "https://api.cloud.mara.com/v1")
        if not self.api_key:
            self.api_key = os.getenv("MARA_API_KEY")
            if not self.api_key:
                env_f = Path(".env")
                if env_f.exists():
                    for line in env_f.read_text().splitlines():
                        if line.startswith("MARA_API_KEY="):
                            self.api_key = line.split("=", 1)[1].strip()


@dataclass
class SuiteConfig:
    name: str
    enabled: bool = True
    description: str = ""
    difficulty_tiers: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    arms: List[str] = field(default_factory=list)
    proposals: List[str] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)
    scale: int = 10


@dataclass
class OutputConfig:
    results_dir: str = "results"
    save_json: bool = True
    generate_markdown_report: bool = True
    auto_plot: bool = False


@dataclass
class BenchmarkConfig:
    name: str
    version: str = "1.0.0"
    description: str = ""
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    suites: List[SuiteConfig] = field(default_factory=list)
    output: OutputConfig = field(default_factory=OutputConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> BenchmarkConfig:
        data = yaml.safe_load(Path(path).read_text())
        dataset = DatasetConfig(**data.get("dataset", {}))
        model = ModelConfig(**data.get("model", {}))
        output = OutputConfig(**data.get("output", {}))
        suites = [SuiteConfig(**s) for s in data.get("suites", [])]
        return cls(
            name=data.get("name", "aml-benchmark"),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            dataset=dataset,
            model=model,
            suites=suites,
            output=output
        )
