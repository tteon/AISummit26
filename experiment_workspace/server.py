#!/usr/bin/env python3
"""Local, read-only evidence browser with versioned research contracts. No run launcher."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlparse
import uuid

import yaml

APP = Path(__file__).resolve().parent
ROOT = APP.parent
TEXT_FIELDS = (
    "title", "decision", "hypothesis", "input_spec", "expected_output", "intervention",
    "baseline", "treatment", "fixed_conditions", "acceptance", "cost_budget", "stop_rule",
    "observation", "interpretation", "limitation", "conclusion",
)
REQUIRED = TEXT_FIELDS[:12]
LABELS = dict(zip(REQUIRED, (
    "제목", "내릴 결정", "가설", "Input", "기대 Output", "변경점", "대조 조건",
    "실험 조건", "고정 조건", "판정 기준", "비용 상한", "종료 조건",
)))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=24)
def cached_json(path, mtime, size):
    return read_json(Path(path))


def document(path):
    stat = path.stat()
    return cached_json(str(path), stat.st_mtime_ns, stat.st_size)


def readiness(contract):
    missing = [LABELS[k] for k in REQUIRED if not contract.get(k, "").strip()]
    return {"missing": missing, "complete": not missing,
            "meaning": "계약 문서의 필수 항목 확인입니다. 실행·Gold·예산 검증이나 실행 승인을 뜻하지 않습니다."}


def observed_output(conversation):
    """Use only an explicitly recorded return payload, never the scorer's Gold."""
    recorded = conversation.get("observed_output")
    if isinstance(recorded, dict) and isinstance(recorded.get("rows"), list):
        return {"record": recorded, "source_role": "database", "source_field": "observed_output",
                "meaning": "conversations.jsonl에 기록된 PROFILE/Bolt 실제 결과 행입니다."}
    for stage in reversed(conversation.get("stages", [])):
        if stage.get("role") != "verifier":
            continue
        try:
            payload = json.loads(stage.get("user", ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        envelope = payload.get("ResultEnvelope", payload.get("EvidencePacket", payload))
        if isinstance(envelope, dict) and isinstance(envelope.get("rows"), list):
            return {"record": envelope, "source_role": "verifier", "source_field": "user",
                    "meaning": "conversations.jsonl에 기록된 검증기 입력의 결과 행입니다."}
    return None


class Conflict(Exception):
    pass


class Workspace:
    def __init__(self, root=ROOT, data_dir=None):
        self.root = Path(root).resolve()
        self.seed = read_json(APP / "seed.json")
        self.data_dir = Path(data_dir or APP / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.data_dir / "workspace.sqlite"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS revisions (id TEXT NOT NULL, version INTEGER NOT NULL, saved_at TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id, version))")

    def connect(self):
        return sqlite3.connect(self.db, timeout=10)

    def contracts(self):
        items = {c["id"]: {**c, "version": 0, "saved_at": None} for c in self.seed["contracts"]}
        with self.connect() as db:
            rows = db.execute("SELECT body FROM revisions ORDER BY version").fetchall()
        for (body,) in rows:
            c = json.loads(body)
            items[c["id"]] = c
        return [{**c, "readiness": readiness(c)} for c in items.values()]

    def history(self, cid):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM revisions WHERE id=? ORDER BY version DESC", (cid,))]

    def save(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("계약은 JSON 객체여야 합니다.")
        cid = payload.get("id")
        if cid == "new":
            cid = "experiment-" + uuid.uuid4().hex[:12]
        elif cid not in {c["id"] for c in self.contracts()}:
            raise ValueError("알 수 없는 실험 계약입니다.")
        if type(payload.get("version")) is not int or payload["version"] < 0:
            raise ValueError("계약 버전이 필요합니다.")
        for field in TEXT_FIELDS:
            if not isinstance(payload.get(field), str) or len(payload[field]) > 20000:
                raise ValueError(f"{field}: 문자열이어야 하며 20,000자 이하여야 합니다.")
        if payload["conclusion"] not in {"unreviewed", "adopt", "reject", "inconclusive"}:
            raise ValueError("허용되지 않은 판정입니다.")
        evidence = payload.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 100:
            raise ValueError("근거 목록은 최대 100개입니다.")
        runs = self.run_paths()
        for ref in evidence:
            if not isinstance(ref, dict) or set(ref) != {"run", "episode"}:
                raise ValueError("근거에는 run과 episode가 필요합니다.")
            if not isinstance(ref["run"], str) or not isinstance(ref["episode"], str):
                raise ValueError("근거 식별자는 문자열이어야 합니다.")
            if ref["run"] not in runs or ref["episode"] not in {
                s.get("episode_id") for s in document(runs[ref["run"]]).get("samples", [])
            }:
                raise ValueError("원본에서 찾을 수 없는 실행 근거입니다.")
        if payload["conclusion"] != "unreviewed" and (
            not evidence or not all(payload[k].strip() for k in ("observation", "interpretation", "limitation"))
        ):
            raise ValueError("판정을 저장하려면 실제 근거, 관측, 해석, 한계를 작성하세요.")
        body = {k: payload[k] for k in TEXT_FIELDS}
        body.update(id=cid, evidence=evidence, version=payload["version"] + 1,
                    saved_at=datetime.now(timezone.utc).isoformat())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT MAX(version) FROM revisions WHERE id=?", (cid,)).fetchone()[0] or 0
            if current != payload["version"]:
                raise Conflict("다른 창에서 계약이 변경되었습니다. 변경 내용을 복사한 뒤 새로고침하세요.")
            db.execute("INSERT INTO revisions VALUES (?, ?, ?, ?)",
                       (cid, body["version"], body["saved_at"], json.dumps(body, ensure_ascii=False)))
        return {**body, "readiness": readiness(body)}

    def run_paths(self):
        paths = set(self.root.glob("results/episodes/fibo_schema_context/*/report.json"))
        paths.update(self.root.glob("results/episodes/ontology_mapping/*/report.json"))
        paths.update(self.root.glob("results/episodes/agent_model_matrix/*/models/*/report.json"))
        paths.update(self.root.glob("results/episodes/agent_topology/*/report.json"))
        # Whitelisted families exclude explicitly invalid runs and matrix parent summaries.
        return {hashlib.sha256(str(p.relative_to(self.root)).encode()).hexdigest()[:16]: p
                for p in sorted(paths) if p.resolve().is_relative_to(self.root / "results")}

    def cases(self):
        catalog = []
        for relative in (
            "configs/fibo_text2cypher_suite.yaml", "configs/agentic_request_schema_contracts_v2.yaml",
            "configs/mara_stage_b_suite_v1.yaml",
        ):
            path = self.root / relative
            if not path.exists():
                continue
            data = yaml.safe_load(path.read_text())
            for q in data.get("questions", data.get("requests", [])):
                catalog.append({**q, "suite": data["suite"], "source": relative,
                                "catalog_key": data["suite"] + ":" + q["id"],
                                "question": q.get("question", q.get("user_request")),
                                "gold_query": q.get("gold", q.get("ref")),
                                "planned": "requests" in data})
        return catalog

    def run_meta(self, key, path):
        d = document(path)
        samples = d.get("samples", [])
        manifest = d.get("manifest") or {}
        endpoint = d.get("endpoint") or {}
        config = d.get("config") or {}
        graph = d.get("graph") or {}
        flags = []
        for name in ("manifest.json", "samples.jsonl"):
            p = path.parent / name
            if not p.exists() or not p.stat().st_size:
                flags.append(f"{name} 없음")
        if not endpoint.get("model_name") or not endpoint.get("base_url"):
            flags.append("endpoint 불완전")
        if manifest.get("git_dirty"):
            flags.append("dirty commit · 당시 scope 확인 필요")
        if (d.get("trace_receipt") or {}).get("tempo_complete") is False:
            flags.append("trace receipt 불완전")
        if (d.get("system_monitor_receipt") or {}).get("valid") is False:
            flags.append("system monitor 검증 실패")
        if d.get("run_status") and d["run_status"] != "completed":
            flags.append("실행 상태: " + str(d["run_status"]))
        return {
            "key": key, "name": path.parent.name, "source": str(path.relative_to(self.root)),
            "family": "ontology" if {"fibo_schema_context", "ontology_mapping"}.intersection(path.parts) else "agent",
            "endpoint": {k: endpoint.get(k) for k in (
                "provider", "model_name", "base_url", "reasoning_effort", "temperature", "max_tokens")},
            "graph": graph, "sample_count": len(samples), "flags": flags,
            "arms": sorted({s["arm"] for s in samples}),
            "commit": manifest.get("git_commit"), "date": manifest.get("timestamp_utc"),
            "suite": config.get("suite"), "row_cap": config.get("row_cap"),
            "semantic_source": d.get("semantic_source"),
            "run_status": d.get("run_status", "recorded"),
            "protocol": config if config.get("schema_version") == "finance.ontology-mapping-pilot.v1" else None,
            "receipt_note": "기존 측정 기록입니다. 플랫폼에서 재실행하거나 인과관계를 재검증하지 않았습니다.",
        }

    def catalog(self):
        runs, errors = [], []
        for key, path in self.run_paths().items():
            try:
                runs.append(self.run_meta(key, path))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append({"source": str(path.relative_to(self.root)), "error": str(exc)})
        mapping = self.root / "ontology/business_request_finbench.mapping.yaml"
        terms = yaml.safe_load(mapping.read_text()).get("terms", {}) if mapping.exists() else {}
        return {"project": self.seed["project"], "contracts": self.contracts(),
                "cases": self.cases(), "terms": terms, "runs": runs,
                "import_errors": errors}

    def run(self, key):
        path = self.run_paths().get(key)
        if path is None:
            raise KeyError("실행 기록이 없습니다.")
        d = document(path)
        samples = []
        graph = d.get("graph") or {}
        fields = ("episode_id", "arm", "question_id", "repeat", "correct", "error",
                  "prompt_tokens", "completion_tokens", "reasoning_tokens", "db_hits", "db_ms",
                  "wall_ms", "generate_ms", "server_total_latency_ms", "model_calls", "verifier_pass", "valid")
        for s in d.get("samples", []):
            row = {k: s.get(k) for k in fields}
            row.update(sf=s.get("sf"), database=s.get("database", graph.get("database")),
                       anchor=s.get("anchor", graph.get("anchor")))
            samples.append(row)
        return {"meta": self.run_meta(key, path), "samples": samples}

    def episode(self, key, episode_id):
        path = self.run_paths().get(key)
        if path is None:
            raise KeyError("실행 기록이 없습니다.")
        d = document(path)
        sample = next((s for s in d.get("samples", []) if s.get("episode_id") == episode_id), None)
        if sample is None:
            raise KeyError("episode가 없습니다.")
        conv = {}
        conv_path = path.parent / "conversations.jsonl"
        if conv_path.exists():
            with conv_path.open() as f:
                for line in f:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get("episode_id") == episode_id:
                        conv = record
                        break
        sources = [str(p.relative_to(self.root)) for p in (
            path, conv_path, path.parent / "manifest.json", path.parent / "samples.jsonl",
            path.parent / "trace_receipt.json", path.parent / "PREEXISTING_DIRTY_SCOPE.json",
            path.parent / "protocol.yaml", path.parent / "preflight.json", path.parent / "endpoint.json",
        ) if p.exists()]
        return {"sample": sample, "conversation": conv, "observed_output": observed_output(conv), "meta": self.run_meta(key, path),
                "sources": sources}

    def source(self, relative):
        # A repository file server must never expose .env, .git, or arbitrary symlink targets.
        permitted = {c["source"] for c in self.cases()}
        permitted.update({"ontology/business_request_finbench.mapping.yaml",
                          "ontology/fibo_finbench.projection.yaml", "docs/request_schema_contract.md",
                          "configs/ontology_mapping_pilot_v1.yaml"})
        for path in self.run_paths().values():
            for name in ("report.json", "samples.jsonl", "manifest.json", "conversations.jsonl",
                         "trace_receipt.json", "PREEXISTING_DIRTY_SCOPE.json", "protocol.yaml", "preflight.json", "endpoint.json"):
                permitted.add(str((path.parent / name).relative_to(self.root)))
        path = (self.root / relative).resolve()
        if relative not in permitted or not path.is_relative_to(self.root) or path != self.root / relative:
            raise KeyError("허용된 원본 경로가 아닙니다.")
        return path.read_bytes()


def handler_for(workspace):
    class Handler(BaseHTTPRequestHandler):
        def send(self, value, status=200, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def host_allowed(self):
            port = self.server.server_port
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.host_allowed():
                return self.send({"error": "로컬 주소로 접속하세요."}, 403)
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/catalog":
                    return self.send(workspace.catalog())
                if url.path == "/api/run":
                    return self.send(workspace.run(query["key"]))
                if url.path == "/api/episode":
                    return self.send(workspace.episode(query["key"], query["episode"]))
                if url.path == "/api/history":
                    return self.send(workspace.history(query["id"]))
                if url.path == "/api/export":
                    return self.send({"schema": "finance-ontology-workspace.v1", "project": workspace.seed["project"],
                                      "contracts": workspace.contracts(),
                                      "history": {c["id"]: workspace.history(c["id"]) for c in workspace.contracts()}})
                if url.path == "/api/source":
                    return self.send(workspace.source(query["path"]), content_type="text/plain; charset=utf-8")
                assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css"), "/icon.svg": ("icon.svg", "image/svg+xml")}
                name, kind = assets[url.path]
                return self.send((APP / "static" / name).read_bytes(), content_type=kind + "; charset=utf-8")
            except (KeyError, FileNotFoundError):
                self.send({"error": "요청한 자료가 없습니다."}, 404)
            except (ValueError, OSError) as exc:
                self.send({"error": str(exc)}, 422)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (not self.host_allowed() or self.headers.get("X-Workspace-Request") != "1"
                    or (origin and origin != "http://" + self.headers.get("Host", ""))):
                return self.send({"error": "동일한 로컬 화면에서 저장하세요."}, 403)
            if self.path != "/api/contracts":
                return self.send({"error": "지원하지 않는 작업입니다."}, 404)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 400000:
                    return self.send({"error": "저장 내용의 크기를 확인하세요."}, 413)
                payload = json.loads(self.rfile.read(size))
                self.send(workspace.save(payload))
            except Conflict as exc:
                self.send({"error": str(exc)}, 409)
            except (ValueError, TypeError) as exc:
                self.send({"error": str(exc)}, 422)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=APP / "data")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(Workspace(data_dir=args.data_dir)))
    print(f"Finance Ontology Lab → http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
