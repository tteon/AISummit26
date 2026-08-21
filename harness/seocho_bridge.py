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
