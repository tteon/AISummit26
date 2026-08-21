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


# Both providers speak the OpenAI chat-completions surface, so the connector is a
# base_url plus a key policy — not a new client. The difference that matters for this
# repo is what each endpoint can be *asked*: MARA has no prefix caching (verified
# 2026-08-08 — cached_tokens absent, TTFT flat across identical prefixes), so the
# ontology-as-shared-KV-tier question can only be put to a self-hosted vLLM.
PROVIDER_SPECS: Dict[str, Dict[str, Any]] = {
    "mara": {
        "base_url_env": ("MARA_BASE_URL",),
        "default_base_url": "https://api.cloud.mara.com/v1",
        "api_key_env": ("MARA_API_KEY",),
        "api_key_required": True,
        "default_model": "gpt-oss-120b",
        "model_env": ("MARA_MODEL",),
    },
    "vllm": {
        # SEOCHO's own vLLM preset reads the same two names (store/llm.py:86), so a
        # testbed instance configured for one is configured for both.
        "base_url_env": ("VLLM_BASE_URL", "SEOCHO_VLLM_BASE_URL"),
        "default_base_url": "http://localhost:8000/v1",
        "api_key_env": ("VLLM_API_KEY", "SEOCHO_VLLM_API_KEY"),
        # vLLM serves unauthenticated by default; the OpenAI client still wants a
        # non-empty string, and "EMPTY" is vLLM's documented placeholder.
        "api_key_required": False,
        "api_key_placeholder": "EMPTY",
        # No default: the model name must match the server's --served-model-name, and a
        # wrong one fails at request time with a 404 that reads like a network problem.
        "default_model": None,
        "model_env": ("VLLM_MODEL",),
    },
}


def _env_first(names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _dotenv_first(names: tuple[str, ...], path: Path = Path(".env")) -> Optional[str]:
    """Read a key out of a local .env without importing dotenv (it may not be installed)."""
    if not path.exists():
        return None
    wanted = {f"{n}=" for n in names}
    for line in path.read_text().splitlines():
        line = line.strip()
        for prefix in wanted:
            if line.startswith(prefix):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


@dataclass
class ModelConfig:
    provider: str = "mara"
    model_name: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 1000
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    # The window the server was started with, and how it got there. Position Interpolation
    # (Chen et al. 2023) extends a RoPE model's window by linearly down-scaling position
    # indices — vLLM spells it `--rope-scaling {"rope_type":"linear","factor":N}`, which is
    # PI exactly. A run at an extended window is a different condition from a run at the
    # native one, in both directions: it can hold a longer shared prefix (the capacity the
    # experiment wants) and it can answer worse (the paper reports degradation even inside
    # the original window, and reaches usable quality only after ~1000 fine-tuning steps,
    # which we do not do). Recording the factor is what keeps the two apart; the harness's
    # own correctness scoring is what measures the second.
    native_context: Optional[int] = None
    max_model_len: Optional[int] = None
    pi_factor: Optional[float] = None
    # Sampling/serving extras passed straight through to the endpoint (vLLM guided
    # decoding, cache salts). Empty for every arm measured so far.
    extra_body: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        spec = PROVIDER_SPECS.get(self.provider)
        if spec is None:
            known = ", ".join(sorted(PROVIDER_SPECS))
            raise ValueError(f"unknown model provider {self.provider!r} (known: {known})")

        if not self.base_url:
            self.base_url = _env_first(spec["base_url_env"]) or spec["default_base_url"]

        if not self.model_name:
            self.model_name = _env_first(spec["model_env"]) or spec["default_model"]
        if not self.model_name:
            raise ValueError(
                f"provider {self.provider!r} has no default model: set model.model_name in the "
                f"config or {spec['model_env'][0]} in the environment. For vLLM it must match "
                "the server's served model name (GET /v1/models lists it)."
            )

        if not self.api_key:
            self.api_key = _env_first(spec["api_key_env"]) or _dotenv_first(spec["api_key_env"])
        if not self.api_key and not spec["api_key_required"]:
            self.api_key = spec.get("api_key_placeholder", "EMPTY")
        # A missing *required* key is not raised here: `runner.py list` and every plotting
        # path build a config without ever calling the endpoint. It is raised by
        # `client_kwargs()`, at the moment a client is actually constructed.

    def client_kwargs(self) -> Dict[str, Any]:
        spec = PROVIDER_SPECS[self.provider]
        if not self.api_key and spec["api_key_required"]:
            raise ValueError(
                f"provider {self.provider!r} needs a key: set {spec['api_key_env'][0]} in the "
                "environment or .env"
            )
        return {"api_key": self.api_key, "base_url": self.base_url}

    @property
    def effective_context(self) -> Optional[int]:
        """The window the model is being asked to use, PI included."""
        if self.max_model_len:
            return self.max_model_len
        if self.native_context and self.pi_factor:
            return int(self.native_context * self.pi_factor)
        return self.native_context

    def descriptor(self) -> Dict[str, Any]:
        """Endpoint identity for the run manifest — never the key."""
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_body": self.extra_body or None,
            "api_key_present": bool(self.api_key and self.api_key != "EMPTY"),
            "context": {
                "native": self.native_context,
                "max_model_len": self.max_model_len,
                "effective": self.effective_context,
                # None means the server ran at its native window. A number means Position
                # Interpolation was in play and the answers must be read with that in mind.
                "position_interpolation_factor": self.pi_factor,
                "rope_scaling": ({"rope_type": "linear", "factor": self.pi_factor}
                                 if self.pi_factor else None),
            },
        }


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
        model_data = dict(data.get("model", {}))
        # The provider is the one field the testbed overrides without editing the config:
        # the same suite runs against MARA here and against the rented vLLM there.
        model_data["provider"] = os.getenv("MODEL_PROVIDER", model_data.get("provider", "mara"))
        if os.getenv("MODEL_PROVIDER") and not os.getenv("KEEP_MODEL_NAME"):
            # A provider swap invalidates the config's model name; let the provider spec
            # and its *_MODEL env resolve it instead of silently asking vLLM for a name
            # it does not serve.
            model_data.pop("model_name", None)
        model = ModelConfig(**model_data)
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
