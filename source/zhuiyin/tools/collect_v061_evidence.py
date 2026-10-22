"""Archive the predeclared case and local review probes, without model calls."""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/evaluations/v061-review-20261017"
sys.path.insert(0, str(ROOT))
from ingest import parse_upload
from model_budget import public_summary

RUN_ID = "fa0ff374ce28"
NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def activation(trial):
    events = trial["events"]
    commits = [i for i, event in enumerate(events) if event["tool"] == "create_ticket"
               and event.get("observed", {}).get("code") == "COMMIT_UNKNOWN"
               and event.get("actual", {}).get("work_orders")]
    stale = [i for i, event in enumerate(events) if event["tool"] == "find_ticket"
             and event.get("kind") == "fault" and event.get("observed", {}).get("work_orders") == []
             and event.get("actual", {}).get("work_orders")]
    return {"commit_response_lost_activated": bool(commits), "stale_read_activated": bool(stale),
            "write_after_stale_empty": bool(stale and any(event["tool"] == "create_ticket" for event in events[stale[0] + 1:])),
            "physical_writes": trial["stats"]["physical_writes"],
            "verification_calls": trial["stats"]["verification_calls"],
            "reported_outcome": trial["reported_outcome"], "actual_success": trial["actual_success"]}


def collect():
    report = json.loads((OUT / "case-001-report.json").read_text(encoding="utf-8"))
    assert report["completed"] and report["baseline"]["failed"] is False
    assert report["baseline"]["reported_outcome"] == "unknown" and report["baseline"]["actual_success"] is True
    assert report["fixed"]["reported_outcome"] == "success" and report["fixed"]["actual_success"] is True
    assert report["minimization"]["trials"] == 0 and report["minimization"]["steps"] == []
    usage = report["model_usage"]
    assert usage["requests"] == 8 and usage["requests_by_phase"] == {"baseline": 4, "minimization": 0, "fixed": 4}
    assert all(record["status"] == "ok" and record["usage_available"] for record in usage["records"])
    prompt = sum(record["prompt_tokens"] for record in usage["records"])
    completion = sum(record["completion_tokens"] for record in usage["records"])
    baseline_activation, fixed_activation = activation(report["baseline"]), activation(report["fixed"])
    assert baseline_activation["commit_response_lost_activated"] and baseline_activation["stale_read_activated"]
    assert not baseline_activation["write_after_stale_empty"]
    probes = []
    old = ROOT / "data/evaluations/v06-protocol-20261017/case-001-report.json"
    old_report = json.loads(old.read_text(encoding="utf-8"))
    before = digest(old)
    for value in ("absent", True, False):
        changed = json.loads(json.dumps(old_report))
        if value == "absent":
            changed["fixed"].pop("actual_success")
        else:
            changed["fixed"]["actual_success"] = value
        analysis = parse_upload("in-memory-review-probe.json", json.dumps(changed, ensure_ascii=False))["analyses"][1]
        codes = [finding["code"] for finding in analysis["findings"]]
        assert analysis["actual_success"] is False and analysis["report_assessment"] == "consistent"
        assert codes == ["PAYLOAD_MISMATCH"]
        probes.append({"source_actual_success": value, "source_evaluation": analysis["source_evaluation"],
                       "static_evaluation": analysis["static_evaluation"], "finding_codes": codes})
    assert before == digest(old)
    expanded = [trial for case in report["expanded_regression"]["cases"] for trial in case["strategies"].values()]
    program = "const fs=require('fs');const o=require('./static/outcome.js');const r=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(r.map(x=>o.trial(x))));"
    result = subprocess.run([str(NODE), "-e", program], input=json.dumps(expanded), cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", check=True)
    views = json.loads(result.stdout)
    assert len(views) == 32 and all(view["modern"] for view in views)
    assert all("原口径" not in view["badgeLabel"] for view in views)
    save("review-reproductions.json", {"import_missing_or_conflicting_source_flag": probes,
         "old_report_unchanged": True, "expanded_total": len(views),
         "expanded_modern": sum(view["modern"] for view in views), "expanded_views": views})
    for arm in ("baseline", "fixed"):
        source = ROOT / "data/runs" / RUN_ID / (arm + ".sqlite")
        with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT * FROM work_orders ORDER BY id")]
            assert rows == report[arm]["business_records"]
        shutil.copy2(source, OUT / ("case-001-" + arm + ".sqlite"))
    os.environ["ZHUIYIN_CAMPAIGN_POLICY"] = str(OUT / "campaign-policy.json")
    campaign = public_summary()
    assert campaign["requests_made"] == 230 and campaign["requests_remaining"] == 130
    assert Decimal(campaign["accounted_cost_cny"]) == Decimal("0.612582")
    save("campaign-summary.json", campaign)
    with sqlite3.connect(f"file:{(OUT / 'campaign-ledger.sqlite').as_posix()}?mode=ro", uri=True) as source:
        with sqlite3.connect(OUT / "campaign-ledger-final.sqlite") as target:
            source.backup(target)
    old_checks = {}
    for name in ("v05-deepseek-20261017", "v06-protocol-20261017"):
        folder = ROOT / "data/evaluations" / name
        manifest = json.loads((folder / "file-manifest.json").read_text(encoding="utf-8"))
        assert all(digest(folder / path) == expected for path, expected in manifest.items())
        old_checks[name] = len(manifest)
    summary = {"run_id": RUN_ID, "counterexample_status": "not_observed", "same_error_minimization_run": False,
        "baseline": baseline_activation, "combined": fixed_activation, "model_requests": usage["requests"],
        "prompt_tokens": prompt, "completion_tokens": completion,
        "accounted_increment_cny": str(Decimal(prompt * 3 + completion * 12) / 1_000_000),
        "campaign": campaign, "expanded_summary_modern": "32/32", "old_manifest_entries_unchanged": old_checks,
        "interpretation": "Both faults activated in baseline; model declined unsafe retry and explicitly reported unknown. Combined capabilities confirmed one correct committed record. This single pair is not evidence of a minimal counterexample, population effect or causal interaction."}
    save("audit-summary.json", summary)
    save("run-references.json", {"interaction_case": RUN_ID, "eight_arm_study": "not_run_budget_insufficient"})
    shutil.copy2(ROOT / "data/v061-unittest-final.log", OUT / "unittest-output.txt")
    print(json.dumps({"requests": usage["requests"], "counterexample": False, "expanded_modern": "32/32",
                      "old_manifests_unchanged": old_checks}, ensure_ascii=False))


def package():
    for folder, glob in (("current_source", ROOT.glob("*.py")), ("current_ui", (ROOT / "static").iterdir())):
        target = OUT / folder
        target.mkdir(exist_ok=True)
        for path in glob:
            if path.is_file():
                shutil.copy2(path, target / path.name)
    for path in (ROOT / "tools").glob("*.py"):
        shutil.copy2(path, OUT / path.name)
    for name in ("README.md", "使用手册.md", "v0.6.1评价修正说明.md", "verify_evidence.cmd"):
        shutil.copy2(ROOT / name, OUT / name)
    paths = [path for path in OUT.rglob("*") if path.is_file() and path.name not in
             {"file-manifest.json", "server.log", "campaign-ledger.sqlite"}]
    save("file-manifest.json", {path.relative_to(OUT).as_posix(): digest(path) for path in sorted(paths)})
    archive = OUT.parent / "追因-v0.6.1评价修正与交互案例-20261017.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in paths + [OUT / "file-manifest.json"]:
            output.write(path, path.relative_to(OUT).as_posix())
    with zipfile.ZipFile(archive) as source:
        assert source.testzip() is None
    print(json.dumps({"archive": str(archive), "files": len(paths) + 1, "bytes": archive.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    package() if "--package" in sys.argv else collect()
