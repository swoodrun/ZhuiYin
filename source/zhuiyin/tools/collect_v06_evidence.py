"""Read-only audit of saved experimental evidence; never sends model requests."""
from decimal import Decimal
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/evaluations/v06-protocol-20261017"
sys.path.insert(0, str(ROOT))
import engine
from ingest import parse_upload
from model_budget import public_summary

RUN_ID = "b5e3bdcd5d7a"


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit():
    report = json.loads((OUT / "case-001-report.json").read_text(encoding="utf-8"))
    mini, usage = report["minimization"], report["model_usage"]
    target = report["baseline"]["failure_fingerprint"]
    assert report["completed"] and mini["completed"] and mini["stable"]
    assert report["evaluation_protocol"] == engine.EVALUATION_PROTOCOL
    assert mini["failure_fingerprint"] == target
    assert mini["one_minimal"] is False
    assert all(run["failure_fingerprint"] == target for run in mini["initial_runs"] + mini["final_runs"])
    assert mini["final_revalidation"]["same_error_count"] == 2
    runs = mini["initial_runs"] + [run for step in mini["steps"] for run in step["trial_evidence"]] + mini["final_runs"]
    assert len(runs) == mini["trials"] == 10
    for run in runs:
        assert run["completed"] and run["events"] and "business_records" in run and run["sqlite_dump"]
        assert run["model_usage"]["requests"] == len(run["model_usage"]["records"])
    remaining = report["config"]["faults"][:]
    for step in mini["steps"]:
        assert step["completed"] and step["attempted"] == step["repeats"] == 2
        assert step["candidate"] == [fault for fault in remaining if fault != step["removed_fault"]]
        assert step["accepted"] == all(run["failure_fingerprint"] == target for run in step["trial_evidence"])
        if step["accepted"]:
            remaining = step["candidate"]
        assert step["remaining"] == remaining
    assert remaining == mini["faults"] == ["corrupt_payload"]
    boundary = [mini["steps"][index] for index in mini["boundary_step_indexes"]]
    assert set(step["removed_fault"] for step in boundary) == set(remaining)
    assert all(not step["accepted"] for step in boundary)
    assert sum(usage["requests_by_phase"].values()) == usage["requests"] == 38
    assert sum(run["model_usage"]["requests"] for run in runs) == usage["requests_by_phase"]["minimization"]
    assert all(record["status"] == "ok" and record["usage_available"] for record in usage["records"])
    prompt = sum(record["prompt_tokens"] for record in usage["records"])
    completion = sum(record["completion_tokens"] for record in usage["records"])
    assert (prompt, completion) == (21897, 2097)
    new_cost = Decimal(prompt * 3 + completion * 12) / 1_000_000
    snapshot = OUT / "source_snapshot"
    provenance = report["provenance"]
    assert all(digest(snapshot / name) == value for name, value in provenance["source_sha256"].items())
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical(provenance["source_sha256"])).hexdigest() == provenance["source_tree_sha256"]
    assert hashlib.sha256(canonical(report["config"])).hexdigest() == provenance["public_config_sha256"]
    # Current core definitions still match the frozen run; UI wording may change independently.
    definitions = inspect.getsource(engine.agent_spec) + inspect.getsource(engine.llm_agent)
    assert hashlib.sha256(definitions.encode()).hexdigest() == provenance["prompt_definition_sha256"]
    old = ROOT / "data/evaluations/v05-deepseek-20261017"
    old_manifest = json.loads((old / "file-manifest.json").read_text(encoding="utf-8"))
    assert all(digest(old / name) == value for name, value in old_manifest.items())
    imported = parse_upload("case-001-report.json", json.dumps(report, ensure_ascii=False))
    assert imported["source_report"]["evaluation_protocol"] == engine.EVALUATION_PROTOCOL
    assert [analysis["verdict"] for analysis in imported["analyses"]] == ["violation", "violation"]
    assert [analysis["report_assessment"] for analysis in imported["analyses"]] == ["inconsistent", "consistent"]
    os.environ["ZHUIYIN_CAMPAIGN_POLICY"] = str(OUT / "campaign-policy.json")
    campaign = public_summary()
    assert campaign["requests_made"] == 222 and campaign["requests_remaining"] == 138
    assert campaign["unknown_usage_requests"] == campaign["unsettled_requests"] == 0
    assert Decimal(campaign["accounted_cost_cny"]) == Decimal("0.500862") + new_cost
    save("campaign-summary.json", campaign)
    save("run-references.json", {"case-001": RUN_ID, "case-002": "not_run_first_case_completed"})
    summary = {"run_id": RUN_ID, "protocol": engine.EVALUATION_PROTOCOL, "target": target,
        "original_faults": report["config"]["faults"], "final_faults": remaining,
        "initial_reproduced": "2/2", "independent_final_reproduced": "2/2",
        "candidate_groups": len(mini["steps"]), "minimization_trials": len(runs),
        "total_model_trials": len(runs) + 2, "requests": usage["requests"],
        "prompt_tokens": prompt, "completion_tokens": completion,
        "accounted_increment_cny": str(new_cost), "campaign": campaign,
        "fixed_fingerprint": report["fixed"]["failure_fingerprint"],
        "fixed_outcome": report["fixed"]["reported_outcome"],
        "source_files_verified": len(provenance["source_sha256"]),
        "post_run_source_changes": {name: {"run_sha256": value, "current_sha256": digest(ROOT / name)}
                                    for name, value in provenance["source_sha256"].items()
                                    if digest(ROOT / name) != value},
        "old_manifest_entries_unchanged": len(old_manifest), "static_reimport_verified": True,
        "infrastructure_errors": 0, "audit_passed": True}
    save("audit-summary.json", summary)
    for name in ("baseline", "fixed"):
        with sqlite3.connect(f"file:{(ROOT / 'data/runs' / RUN_ID / (name + '.sqlite')).as_posix()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT * FROM work_orders ORDER BY id")]
            assert rows == report[name]["business_records"]
        shutil.copy2(ROOT / "data/runs" / RUN_ID / (name + ".sqlite"), OUT / ("case-001-" + name + ".sqlite"))
    with sqlite3.connect(f"file:{(OUT / 'campaign-ledger.sqlite').as_posix()}?mode=ro", uri=True) as source:
        with sqlite3.connect(OUT / "campaign-ledger-final.sqlite") as target_db:
            source.backup(target_db)
    shutil.copy2(ROOT / "data/v06-unittest-final.log", OUT / "unittest-output.txt")
    return summary


def package():
    # Keep the exact run's source_snapshot. Current UI wording is separately retained.
    current_ui = OUT / "current_ui"
    current_ui.mkdir(exist_ok=True)
    for path in (ROOT / "static").iterdir():
        if path.is_file():
            shutil.copy2(path, current_ui / path.name)
    current_source = OUT / "current_source"
    current_source.mkdir(exist_ok=True)
    for path in ROOT.glob("*.py"):
        shutil.copy2(path, current_source / path.name)
    for name in ("README.md", "v0.6升级说明.md", "使用手册.md"):
        shutil.copy2(ROOT / name, OUT / name)
    shutil.copy2(Path(__file__), OUT / "collect_v06_evidence.py")
    paths = [path for path in OUT.rglob("*") if path.is_file() and path.name not in
             {"campaign-ledger.sqlite", "server.log", "file-manifest.json"}]
    save("file-manifest.json", {path.relative_to(OUT).as_posix(): digest(path) for path in sorted(paths)})
    archive = OUT.parent / "追因-v0.6升级与真实同错精简-20261017.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in paths + [OUT / "file-manifest.json"]:
            output.write(path, path.relative_to(OUT).as_posix())
    with zipfile.ZipFile(archive) as source:
        assert source.testzip() is None
    print(json.dumps({"archive": str(archive), "files": len(paths) + 1,
                      "bytes": archive.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    if "--package" in sys.argv:
        package()
    else:
        result = audit()
        print(json.dumps({key: result[key] for key in ("audit_passed", "requests", "accounted_increment_cny",
              "source_files_verified", "old_manifest_entries_unchanged", "static_reimport_verified")}, ensure_ascii=False))
