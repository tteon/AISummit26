"""Put seocho in the middle of the exchange, where it belongs, and make it say so.

The thesis this repo measures is a three-tier one: a graph database on the CPU, a model server
on the GPU, and an orchestrator between them deciding what crosses. Two of those tiers already
report for themselves — the database through the Bolt summary and our own stage timers, the
model server through vLLM's metrics and spans. The orchestrator did not, because the harness
called the driver directly and used seocho only as a library of pure functions (ontology,
policy, guardrail). So the middle tier was the one tier with no telemetry, which is exactly
backwards for an experiment about interface engineering.

This bridge routes the Cypher call through `seocho.query.query_proxy.QueryProxy`, which is
seocho's instrumented execution path. What that buys, per call:

  * `seocho.retrieval.duration` / `seocho.retrieval.inflight` and admission rejections
  * a `db.query` span carrying db.system, db.statement, the workspace hash, the ontology
    profile, the query *template* hash, and whether workspace filtering was enforced
  * a DomainEvent per rejection, so refusals are countable rather than inferable

Nothing about the arms changes. The same Cypher, the same row cap, the same PROFILE, the same
guardrail decisions — the adapter below does the driver work exactly as the harness did, and
QueryProxy wraps it. What changes is that the middle tier now emits, and that its spans nest
inside the episode's trace alongside vLLM's own, so one trace shows the whole exchange:
episode -> seocho db.query -> Bolt, and episode -> llm_request -> prefill/decode.

Kept behind a flag (`--via-seocho`) and recorded in the manifest, because the published arms
ran without it and a silent change of execution path would make old and new runs
incomparable while looking identical.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_OTLP = "http://127.0.0.1:4317"


def _ensure_seocho_on_path(source: Optional[str]) -> Optional[str]:
    """Prefer an explicit checkout over whatever pip resolved.

    The pip-installed seocho on a given box may be an old build (0.2.0 here, with no
    `seocho.query` at all), while a working tree has the instrumented paths. Which one answered
    is recorded, because 'seocho' is not a version.
    """
    import sys
    candidates = [source] if source else []
    candidates += [os.getenv("SEOCHO_SRC"), "/home/hadry/lab/seocho-room/seocho/src"]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if p.name != "src" and (p / "src" / "seocho").is_dir():
            p = p / "src"
        if (p / "seocho").is_dir():
            sys.path.insert(0, str(p))
            return str(p)
    return None


def enable_observability(*, backend: str = "otlp", endpoint: Optional[str] = None,
                         service_name: str = "seocho-aisummit26",
                         source: Optional[str] = None) -> Dict[str, Any]:
    """Turn on seocho's own metrics and tracing, and report what actually came up."""
    used_path = _ensure_seocho_on_path(source)
    endpoint = endpoint or os.getenv("SEOCHO_TRACE_OTLP_ENDPOINT") or DEFAULT_OTLP

    import seocho
    from seocho import metrics as seocho_metrics
    from seocho import tracing as seocho_tracing

    metrics_on = tracing_on = False
    errors: List[str] = []
    try:
        seocho_metrics.enable_metrics(backend=backend, endpoint=endpoint)
        metrics_on = True
    except Exception as exc:  # a missing exporter must not take the run down
        errors.append(f"metrics: {type(exc).__name__}: {exc}")
    try:
        tracing_on = bool(seocho_tracing.enable_tracing(
            backend=backend, endpoint=endpoint, service_name=service_name))
    except Exception as exc:
        errors.append(f"tracing: {type(exc).__name__}: {exc}")

    return {
        "seocho_version": getattr(seocho, "__version__", None),
        "seocho_path": getattr(seocho, "__file__", None),
        "source_prepended": used_path,
        "backend": backend, "endpoint": endpoint, "service_name": service_name,
        "metrics_enabled": metrics_on, "tracing_enabled": tracing_on,
        "tracing_backends": (seocho_tracing.current_backend_names()
                             if hasattr(seocho_tracing, "current_backend_names") else None),
        "degraded_reasons": (seocho_tracing.tracing_degraded_reasons()
                             if hasattr(seocho_tracing, "tracing_degraded_reasons") else None),
        "errors": errors or None,
    }


class DriverGraphStore:
    """`GraphStore.query` over the driver the harness already owns.

    Deliberately not `Neo4jGraphStore`: that one opens its own driver from a URI, which would
    mean a second connection pool with different settings, and the pool is part of what this
    repo measures. The adapter keeps our session, our PROFILE, our row cap and our stage
    timers, and hands the rows back in the shape QueryProxy expects.
    """

    def __init__(self, driver, *, row_cap: int, profile: bool = True,
                 tx_timeout_s: Optional[float] = None):
        self._driver = driver
        self._row_cap = row_cap
        self._profile = profile
        self._tx_timeout_s = tx_timeout_s
        self._local = threading.local()

    @property
    def last(self) -> Dict[str, Any]:
        """Stage timings and the summary from this thread's most recent call."""
        return getattr(self._local, "last", {})

    def query(self, cypher: str, *, params: Optional[Dict[str, Any]] = None,
              database: str = "neo4j", workspace_id: Optional[str] = None,
              enforce_workspace_filter: bool = False) -> List[Dict[str, Any]]:
        params = dict(params or {})
        if workspace_id is not None:
            params.setdefault("workspace_id", workspace_id)
        text = ("PROFILE " + cypher) if self._profile else cypher
        t0 = time.perf_counter()
        with self._driver.session(database=database) as session:
            tx = (session.begin_transaction(timeout=self._tx_timeout_s)
                  if self._tx_timeout_s else session.begin_transaction())
            try:
                t_run = time.perf_counter()
                result = tx.run(text, **params)
                t_hydrate = time.perf_counter()
                rows = [dict(r) for _, r in zip(range(self._row_cap), result)]
                t_hydrated = time.perf_counter()
                summary = result.consume()
                tx.commit()
            except Exception:
                tx.close()
                raise
        total_ms = (time.perf_counter() - t0) * 1000
        server_ms = (float(summary.result_available_after or 0)
                     + float(summary.result_consumed_after or 0))
        self._local.last = {
            "summary": summary,
            "ms": round(total_ms, 3),
            "submit_ms": round((t_hydrate - t_run) * 1000, 3),
            "hydrate_ms": round((t_hydrated - t_hydrate) * 1000, 3),
            "server_available_ms": float(summary.result_available_after or 0),
            "server_consumed_ms": float(summary.result_consumed_after or 0),
            "client_cpu_ms": round(max(total_ms - server_ms, 0.0), 3),
        }
        return rows

    # GraphStore's ABC has more surface than a read path needs; QueryProxy only calls query().
    def ensure_constraints(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        raise NotImplementedError("read-only adapter")


class SeochoOrchestrator:
    """One Cypher call, orchestrated and instrumented by seocho."""

    def __init__(self, driver, *, row_cap: int, workspace_id: str,
                 ontology_profile: str = "default", tx_timeout_s: Optional[float] = None,
                 enforce_workspace_filter: Optional[bool] = None):
        from seocho.query.query_proxy import QueryProxy, QueryRequest  # noqa: F401
        self._QueryRequest = QueryRequest
        self.store = DriverGraphStore(driver, row_cap=row_cap, tx_timeout_s=tx_timeout_s)
        self.proxy = QueryProxy(self.store, enforce_workspace_filter=enforce_workspace_filter)
        self.workspace_id = workspace_id
        self.ontology_profile = ontology_profile

    def run(self, cypher: str, params: Dict[str, Any], *, database: str) -> Dict[str, Any]:
        """Returns {"rows", "stages", "rejected"} — never raises for a refusal."""
        from seocho.query.query_proxy import QueryAdmissionRejected
        req = self._QueryRequest(cypher=cypher, workspace_id=self.workspace_id,
                                 database=database, ontology_profile=self.ontology_profile,
                                 params=params)
        try:
            rows = self.proxy.query(req)
        except QueryAdmissionRejected as exc:
            # Countable, not inferable: seocho already emitted the rejection metric and event.
            return {"rows": [], "stages": {}, "rejected": str(exc)}
        return {"rows": rows, "stages": dict(self.store.last), "rejected": None}


# --------------------------------------------------------------------------------------
# The orchestrator's own text2cypher agent
# --------------------------------------------------------------------------------------
def make_llm_backend(cfg) -> Any:
    """A seocho LLM backend pointed at the endpoint this run already resolved.

    seocho 0.6.0 ships presets for both providers this repo uses (`mara` and `vllm`), so the
    orchestrator talks to the same server the episode's own agent does — same model, same
    base_url, same key policy. Two different endpoints would make the two LLM calls in a round
    incomparable, which is the whole point of measuring them together.
    """
    from seocho.store.llm import create_llm_backend
    return create_llm_backend(provider=cfg.provider, model=cfg.model_name,
                              api_key=cfg.api_key, base_url=cfg.base_url)


def schema_map_from_ontology(ontology: Any) -> Dict[str, Any]:
    """`{label: (property, ...)}` — the shape text2cypher puts in its prompt."""
    out: Dict[str, Any] = {}
    for label, node in (getattr(ontology, "nodes", None) or {}).items():
        props = getattr(node, "properties", None) or {}
        out[str(label)] = tuple(str(k) for k in props)
    rels = getattr(ontology, "relationships", None) or {}
    if rels:
        out["_relationships"] = tuple(str(k) for k in rels)
    return out


class SeochoGraphAgent:
    """Question in, rows out — with seocho doing the generation *and* the execution.

    This is the arm the three-tier thesis actually describes. In every other arm our own agent
    writes the Cypher and the orchestrator is a library; here seocho's text2cypher agent
    writes it (one LLM call on the GPU), its validator accepts or repairs it, and its
    QueryProxy runs it (the CPU/graph tier) — so a single round contains two model calls with
    the orchestrator between them, and both tiers report:

        seocho.text2cypher.duration / .validation_failure.count / .execution_failure.count
        seocho.retrieval.duration / .inflight

    The row cap comes from the policy (`max_result_rows`), not from this harness, because the
    interface must not disagree with itself about how many rows exist.
    """

    def __init__(self, driver, *, cfg, ontology, policy, workspace_id: str,
                 database: str, row_cap: Optional[int] = None,
                 tx_timeout_s: Optional[float] = None):
        self.backend = make_llm_backend(cfg)
        self.model = cfg.model_name
        self.policy = policy
        self.schema = schema_map_from_ontology(ontology)
        self.database = database
        self.workspace_id = workspace_id
        self._driver = driver
        cap = row_cap or int(getattr(policy, "max_result_rows", 50) or 50)
        self.row_cap = cap
        self.orchestrator = SeochoOrchestrator(driver, row_cap=cap, workspace_id=workspace_id,
                                               tx_timeout_s=tx_timeout_s)

    async def _explain(self, cypher: str, params: Dict[str, Any]) -> None:
        """seocho's `Explain` contract: raise if the query does not compile.

        Run on a thread so the event loop is not blocked by the driver's sync API.
        """
        import asyncio

        def _run() -> None:
            with self._driver.session(database=self.database) as session:
                session.run("EXPLAIN " + cypher, **params).consume()

        await asyncio.to_thread(_run)

    async def ask(self, question: str, params: Dict[str, Any]) -> Dict[str, Any]:
        from seocho.query.text2cypher import generate_validated_cypher
        t0 = time.perf_counter()
        try:
            gen = await generate_validated_cypher(
                question=question, schema=self.schema, params=params, policy=self.policy,
                backend=self.backend, model=self.model, explain=self._explain)
        except Exception as exc:
            # A failed generation still spent its wall time in the LLM, and losing that
            # number corrupts the episode's attribution: the harness computes db_ms as
            # (tool time - generation time), so a generation that raises without reporting
            # its time gets booked as database time. One run of 26 episodes booked 101.7 s
            # of failed-generation LLM time as db_ms that way — reported as "seocho
            # orchestration overhead" until the per-call residuals said otherwise.
            exc.generate_ms = round((time.perf_counter() - t0) * 1000, 3)  # type: ignore[attr-defined]
            raise
        gen_ms = (time.perf_counter() - t0) * 1000
        import asyncio
        try:
            res = await asyncio.to_thread(self.orchestrator.run, gen.cypher, dict(gen.params),
                                          database=self.database)
        except Exception as exc:
            # Same stamp on the execution path: generation succeeded and its time is known,
            # so a query that then fails at the database must not re-book that time as db_ms.
            exc.generate_ms = round(gen_ms, 3)  # type: ignore[attr-defined]
            raise
        return {
            "cypher": gen.cypher, "params": dict(gen.params),
            "attempts": gen.attempts, "explained": gen.explained,
            "prompt_version": gen.prompt_version,
            "generate_ms": round(gen_ms, 3),
            "rows": res["rows"], "stages": res["stages"], "rejected": res["rejected"],
        }


def guide_backend_with_grammar(backend: Any, grammar: str) -> Any:
    """Make an existing seocho backend decode under an EBNF, in place.

    seocho's `provider_options` has an allowlist (`prompt_cache_key`, `cache_salt`,
    `thinking`), so a grammar cannot be threaded through the public argument — and patching
    seocho is not on the table for an experiment that has to stay comparable to it. Instead the
    instance's class gains one override: the request kwargs get
    `extra_body={"structured_outputs": {"grammar": ...}}`, which is vLLM 0.27's shape
    (`guided_grammar` was removed; see StructuredOutputsParams in sampling_params.py).

    `response_format` is dropped when a grammar is set: the two are mutually exclusive ways of
    constraining the same output, and vLLM refuses both at once. The grammar itself produces the
    JSON envelope, so seocho's contract still holds.
    """
    base = type(backend)

    class _GrammarGuided(base):  # type: ignore[misc, valid-type]
        def _completion_request_kwargs(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            kw = super()._completion_request_kwargs(*args, **kwargs)
            extra = dict(kw.get("extra_body") or {})
            extra["structured_outputs"] = {"grammar": grammar}
            kw["extra_body"] = extra
            kw.pop("response_format", None)
            return kw

    backend.__class__ = _GrammarGuided
    return backend
