"""Contract durability, evidence identity, and source isolation for the local workspace."""
import importlib.util
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

spec = importlib.util.spec_from_file_location(
    "experiment_workspace_server", Path(__file__).resolve().parents[1] / "experiment_workspace/server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    run = root / "results/episodes/fibo_schema_context/fixture"
    run.mkdir(parents=True)
    samples = [
        {"episode_id": "same-id:a", "question_id": "in_transfer_total", "arm": "physical_only",
         "correct": True, "repeat": 0, "prompt_tokens": 9, "db_hits": 4},
        {"episode_id": "same-id:b", "question_id": "in_transfer_total", "arm": "compiled_fibo",
         "correct": False, "repeat": 0, "prompt_tokens": 15},
    ]
    report = {"samples": samples, "graph": {"database": "fixture-db", "anchor": 108},
              "endpoint": {"model_name": "fixture-model", "provider": "fixture", "base_url": "http://fixture"},
              "manifest": {"git_commit": "fixture", "git_dirty": True}, "config": {}}
    (run / "report.json").write_text(json.dumps(report))
    (run / "samples.jsonl").write_text("\n".join(json.dumps(s) for s in samples))
    (run / "manifest.json").write_text(json.dumps(report["manifest"]))
    (run / "conversations.jsonl").write_text(json.dumps({"episode_id": "same-id:a", "question": "fixture request"})+"\n")
    (root / ".env").write_text("SECRET=fixture")
    return module.Workspace(root, tmp_path / "state")


def test_versioned_contract_survives_restart_and_preserves_results(workspace):
    key, path = next(iter(workspace.run_paths().items()))
    before = path.read_bytes()
    draft = workspace.contracts()[0]
    draft["title"] = "금융 의미 비교"
    draft["evidence"] = [{"run": key, "episode": "same-id:a"}]
    first = workspace.save(draft)
    second = workspace.save({**first, "hypothesis": "추가 문맥이 회귀를 만들 수 있다."})
    assert second["version"] == 2
    assert path.read_bytes() == before
    reopened = module.Workspace(workspace.root, workspace.data_dir)
    assert reopened.contracts()[0]["title"] == "금융 의미 비교"
    assert [x["version"] for x in reopened.history(draft["id"])] == [2, 1]
    assert reopened.history(draft["id"])[1]["hypothesis"] == draft["hypothesis"]


def test_stale_save_cannot_overwrite_newer_decision(workspace):
    original = workspace.contracts()[0]
    workspace.save(original)
    with pytest.raises(module.Conflict):
        workspace.save(original)
    assert len(workspace.history(original["id"])) == 1


def test_new_research_contract_is_persistent_and_editable(workspace):
    draft = {field: "" for field in module.TEXT_FIELDS}
    draft.update(id="new", version=0, title="새 실험", conclusion="unreviewed", evidence=[])
    saved = workspace.save(draft)
    assert saved["id"].startswith("experiment-")
    assert saved["version"] == 1
    assert not saved["readiness"]["complete"]
    saved["hypothesis"] = "명시한 가설"
    assert workspace.save(saved)["version"] == 2
    assert len(module.Workspace(workspace.root, workspace.data_dir).contracts()) == 4


def test_missing_criteria_never_looks_ready_and_decision_needs_evidence(workspace):
    draft = workspace.contracts()[0]
    assert draft["readiness"]["complete"] is False
    assert "판정 기준" in draft["readiness"]["missing"]
    assert "비용 상한" in draft["readiness"]["missing"]
    with pytest.raises(ValueError, match="실제 근거"):
        workspace.save({**draft, "conclusion": "adopt"})
    key = next(iter(workspace.run_paths()))
    valid = {**draft, "conclusion": "inconclusive", "observation": "일치하는 기록과 회귀 기록 존재",
             "interpretation": "추가 검토 필요", "limitation": "fixture 데이터",
             "evidence": [{"run": key, "episode": "same-id:a"}]}
    assert workspace.save(valid)["conclusion"] == "inconclusive"


@pytest.mark.parametrize("bad_ref", [
    {"run": "missing", "episode": "same-id:a"}, {"run": [], "episode": "same-id:a"},
    {"run": "missing", "episode": "same-id:a", "arbitrary": True},
])
def test_fabricated_evidence_is_rejected(workspace, bad_ref):
    with pytest.raises(ValueError):
        workspace.save({**workspace.contracts()[0], "evidence": [bad_ref]})


def test_run_identity_includes_directory_and_missing_metrics_stay_null(workspace):
    key = next(iter(workspace.run_paths()))
    data = workspace.run(key)
    assert data["samples"][1]["db_hits"] is None
    assert data["samples"][0]["sf"] is None  # Never infer scale from a database name.
    assert data["samples"][0]["database"] == "fixture-db"
    assert any("dirty" in flag for flag in data["meta"]["flags"])
    ep = workspace.episode(key, "same-id:a")
    assert ep["conversation"]["question"] == "fixture request"
    assert "score" not in ep["sample"]  # Missing Gold is not constructed from correctness.
    assert ep["observed_output"] is None
    original_path = workspace.run_paths()[key]
    other = original_path.parent.parent / "other-run"
    other.mkdir()
    (other / "report.json").write_text(original_path.read_text())
    assert len(workspace.run_paths()) == 2
    assert len(set(workspace.run_paths())) == 2


def test_observed_output_is_from_recorded_envelope_not_gold():
    rows = [{"incoming_count": 5, "total_amount": 123}]
    conv = {"stages": [{"role": "verifier", "user": json.dumps({
        "ResultEnvelope": {"rows": rows, "completeness": "complete"},
        "query_intent": {"expected_shape": {"n": "integer"}},
    })}]}
    assert module.observed_output(conv)["record"]["rows"] == rows
    assert module.observed_output({"score": {"gold": rows}}) is None
    assert module.observed_output({"stages": [{"role": "verifier", "user": "not JSON"}]}) is None
    assert module.observed_output({"observed_output": {"rows": rows}})["record"]["rows"] == rows


def test_source_endpoint_only_serves_registered_files_and_rejects_symlinks(workspace):
    key, path = next(iter(workspace.run_paths().items()))
    relative = str(path.relative_to(workspace.root))
    assert workspace.source(relative) == path.read_bytes()
    for forbidden in (".env", "../state/workspace.sqlite", "/etc/passwd", "results/../.env"):
        with pytest.raises(KeyError):
            workspace.source(forbidden)
    path.unlink()
    path.symlink_to(workspace.root / ".env")
    with pytest.raises(KeyError):
        workspace.source(relative)


def test_http_save_requires_same_origin_and_export_contains_revisions(workspace):
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.handler_for(workspace))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    draft = workspace.contracts()[0]
    try:
        with urlopen(base + "/api/catalog") as response:
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert json.load(response)["runs"][0]["sample_count"] == 2
        body = json.dumps(draft).encode()
        for headers in ({}, {"Origin": "https://unrelated.example", "X-Workspace-Request": "1"}):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base+"/api/contracts", data=body, headers=headers))
            assert error.value.code == 403
        with urlopen(Request(base+"/api/contracts", data=body, headers={
            "Content-Type": "application/json", "X-Workspace-Request": "1", "Origin": base,
        })) as response:
            assert json.load(response)["version"] == 1
        with urlopen(base+"/api/export") as response:
            exported = json.load(response)
        assert exported["history"][draft["id"]][0]["version"] == 1
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base+"/api/catalog", headers={"Host": "unrelated.example"}))
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
