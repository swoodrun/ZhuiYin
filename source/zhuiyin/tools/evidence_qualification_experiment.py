"""Fixed 24 one-request probes; keys stay in memory, no retries or business actions."""
from __future__ import annotations
import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import evidence_qualification as fixture
import model_budget
from qualification_scoring import assess, audit_citation, classify, summarize, visible_support
from tools.paired_judgment_experiment import (canonical, digest, write_json, policy_environment,
                                             ledger_rows, provider_call, ProviderError, IDENTITY)

VERSION = "evidence-qualification-collection-v1"
BASELINE_LEDGER = ROOT / "data/evaluations/paired-judgment-real-20261018/campaign-ledger-final.sqlite"
BASELINE_POLICY = ROOT / "data/research/paired-judgment-20261018-prepared/campaign/campaign-policy.json"
HISTORY_SHA = "43aca7231ae5291212fe4abbb9a8c347b6fbbcfa2ba364730b47a0c1f4e29f34"
SOURCES = ("evidence_qualification.py", "qualification_scoring.py", "model_budget.py",
           "tools/paired_judgment_experiment.py", "tools/eight_arm_launcher.py",
           "tools/evidence_qualification_experiment.py", "tools/evidence_qualification_launcher.py",
           "tools/verify_evidence_qualification.py")


def history_check(rows):
    if (len(rows) != 331 or sum(row["charged_micros"] for row in rows) != 922815
            or hashlib.sha256(canonical(rows)).hexdigest() != HISTORY_SHA):
        raise ValueError("累计起点不是已验收331次台账")


def clone_ledger(source, destination, immutable=False):
    uri = Path(source).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(uri, uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dest:
            src.backup(dest)


def request_map(design):
    return {(case["condition_id"], protocol): payload for case in design["cases"]
            for protocol, payload in case["payloads"].items()}


def prepare(output, source_fixture=fixture.DEFAULT_SOURCE):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("准备目录已存在，不覆盖固定方案")
    design = fixture.build_design(source_fixture)
    history = ledger_rows(BASELINE_LEDGER)
    history_check(history)
    policy = json.loads(BASELINE_POLICY.read_text(encoding="utf-8"))
    if (policy["max_requests"] != 374 or Decimal(policy["max_cost_cny"]) != 10
            or policy["allowed_origin"] != IDENTITY["base_url"] or policy["allowed_model"] != IDENTITY["model"]
            or Decimal(policy["input_price_per_million"]) != 3 or Decimal(policy["output_price_per_million"]) != 12):
        raise ValueError("既定累计授权或记账价格发生变化")
    payloads = request_map(design)
    worst = sum((len(json.dumps(payloads[(p["condition_id"], p["protocol_id"])], ensure_ascii=True).encode())
                 + model_budget.INPUT_PADDING)*3 + 1200*12 for p in design["execution_plan"])
    if 331+24 > 374 or 922815+worst > 10_000_000:
        raise ValueError("完整24次计划超出累计授权")
    output.mkdir(parents=True)
    shutil.copytree(source_fixture, output / "fixture")
    campaign = output / "campaign"
    campaign.mkdir()
    for name in ("campaign-ledger-initial.sqlite", "campaign-ledger.sqlite"):
        clone_ledger(BASELINE_LEDGER, campaign / name, True)
    policy["ledger_path"] = str(campaign / "campaign-ledger.sqlite")
    write_json(campaign / "campaign-policy.json", policy)
    with policy_environment(campaign / "campaign-policy.json"):
        initial = model_budget.public_summary()
    write_json(campaign / "initial-summary.json", initial)
    write_json(output / "fixed-design.json", design)
    (output / "requests").mkdir()
    for (condition, protocol), payload in payloads.items():
        (output / "requests" / f"{condition}--{protocol}.json").write_bytes(canonical(payload))
    for name in SOURCES:
        dest = output / "source_snapshot" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    write_json(output / "readiness.json", {"status": "ready_no_model_calls", "model_calls": 0,
        "collector_version": VERSION, "initial_requests": 331, "initial_cost_cny": "0.922815",
        "planned_requests": 24, "planned_worst_reservation_cny": f"{worst/1_000_000:.6f}",
        "max_cumulative_requests_after_plan": 355, "cumulative_request_ceiling": 374,
        "cumulative_cost_ceiling_cny": "10", "execution_rule": "24 fixed single-request probes; no retries; stop on infrastructure/budget failure; invalid reports remain counted"})
    write_json(output / "preparation-manifest.json", {"files": {
        p.relative_to(output).as_posix(): digest(p) for p in output.rglob("*") if p.is_file()
        and p.name != "campaign-ledger.sqlite"}, "mutable_file": "campaign/campaign-ledger.sqlite",
        "historical_ledger_sha256": digest(BASELINE_LEDGER)})
    return validate_prepared(output)


def validate_prepared(prepared):
    prepared = Path(prepared).resolve()
    manifest = json.loads((prepared / "preparation-manifest.json").read_text(encoding="utf-8"))
    actual = {p.relative_to(prepared).as_posix() for p in prepared.rglob("*") if p.is_file()
              and p.name != "preparation-manifest.json" and p.relative_to(prepared).as_posix() != "campaign/campaign-ledger.sqlite"}
    if set(manifest["files"]) != actual or manifest["mutable_file"] != "campaign/campaign-ledger.sqlite":
        raise ValueError("固定材料文件清单改变")
    for relative, sha in manifest["files"].items():
        path = (prepared / relative).resolve()
        if not path.is_relative_to(prepared) or digest(path) != sha:
            raise ValueError("固定材料哈希改变")
    design = fixture.build_design(prepared / "fixture")
    if canonical(design) != canonical(json.loads((prepared / "fixed-design.json").read_text(encoding="utf-8"))):
        raise ValueError("方案与冻结数据库不一致")
    for (condition, protocol), payload in request_map(design).items():
        if (prepared / "requests" / f"{condition}--{protocol}.json").read_bytes() != canonical(payload):
            raise ValueError("请求与固定设计不同")
        case = next(c for c in design["cases"] if c["condition_id"] == condition)
        if visible_support(case["receipt"], design["task"]) != case["supported_outcome"]:
            raise ValueError("证据判据与固定合同矛盾")
    for name in SOURCES:
        if digest(prepared / "source_snapshot" / name) != digest(ROOT / name):
            raise ValueError("执行代码与预注册源码不同")
    history_check(ledger_rows(prepared / "campaign/campaign-ledger-initial.sqlite"))
    return design


def execute(prepared, output, api_key, transport=provider_call):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    if not isinstance(api_key, str) or len(api_key.strip()) < 8:
        raise ValueError("需要仅在内存中的连接凭据")
    design = validate_prepared(prepared)
    # Independent standard-library contract audit is mandatory before a request.
    from tools.verify_evidence_qualification import verify_prepared
    audit = verify_prepared(prepared)
    if not audit.get("passed"):
        raise ValueError("独立预注册核验未通过")
    policy_path = prepared / "campaign/campaign-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    initial_rows = ledger_rows(prepared / "campaign/campaign-ledger-initial.sqlite")
    if ledger_rows(policy["ledger_path"]) != initial_rows:
        raise ValueError("本方案已使用，禁止自动重跑")
    output.mkdir(parents=True, exist_ok=False)
    (output / "samples").mkdir()
    report = {"collector_version": VERSION, "task_mode": "frozen_evidence_qualification", "status": "running",
              "preparation_manifest_sha256": digest(prepared / "preparation-manifest.json"),
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "samples": [], "stop_reason": None}
    cases = {c["condition_id"]: c for c in design["cases"]}
    payloads = request_map(design)
    with policy_environment(policy_path):
        report["budget_before"] = model_budget.public_summary()
        report["summary"] = summarize(design, [])
        write_json(output / "study-report.json", report)
        try:
            for plan in design["execution_plan"]:
                item = deepcopy(plan)
                condition, protocol = item["condition_id"], item["protocol_id"]
                payload = payloads[(condition, protocol)]
                body = canonical(payload)
                item.update(request_sha256=hashlib.sha256(body).hexdigest(), business_actions_executed=0)
                folder = output / "samples" / f"{item['sample_index']:02d}"
                folder.mkdir()
                (folder / "request.json").write_bytes(body)
                reservation = response = None
                usage = {"status": "error", "usage_available": False}
                started = time.monotonic()
                try:
                    reservation = model_budget.reserve_request(IDENTITY, payload)
                    if reservation is None:
                        raise model_budget.CampaignBudgetError("policy_required")
                    item["reservation_id"] = reservation.reservation_id
                    # Persist the pending position before network I/O. A process crash cannot erase its reservation.
                    write_json(folder / "pending.json", {**item, "status": "pending", "usage": usage})
                    response = transport(body, api_key)
                    if (not isinstance(response, dict) or api_key in json.dumps(response, ensure_ascii=False, allow_nan=False)):
                        response = None
                        raise ProviderError("invalid_or_credential_response")
                    supplied = response.get("usage", {})
                    if isinstance(supplied, dict) and all(type(supplied.get(k)) is int and supplied[k] >= 0 for k in ("prompt_tokens", "completion_tokens")):
                        usage = {"status": "ok", "usage_available": True, "prompt_tokens": supplied["prompt_tokens"],
                                 "completion_tokens": supplied["completion_tokens"]}
                    item.update(classify(response, protocol))
                    if item["status"] == "valid":
                        case = cases[condition]
                        item["assessment"] = assess(item["outcome"], case["supported_outcome"], case["actual_success"])
                        if protocol == "cited":
                            item["citation"] = audit_citation(item["evidence"], payload, design["task"], case["supported_outcome"], item["outcome"])
                except model_budget.CampaignBudgetError as exc:
                    item.update(status="budget_error", outcome=None, error_code=exc.reason)
                    report["stop_reason"] = "budget_guard"
                except ProviderError as exc:
                    item.update(status="infra_error", outcome=None, error_code=exc.code)
                    report["stop_reason"] = "provider_or_connection_error"
                except Exception:
                    response = None
                    item.update(status="infra_error", outcome=None, error_code="unexpected_collection_error")
                    report["stop_reason"] = "collection_error"
                finally:
                    if reservation is not None:
                        try:
                            model_budget.settle_request(reservation, usage)
                        except model_budget.CampaignBudgetError as exc:
                            item["settlement_error"] = exc.reason
                            report["stop_reason"] = "budget_guard"
                    item["elapsed_seconds"] = round(time.monotonic()-started, 3)
                if response is not None:
                    write_json(folder / "response.json", response)
                item["usage"] = usage
                write_json(folder / "sample.json", item)
                report["samples"].append(item)
                report["summary"] = summarize(design, report["samples"])
                report["budget_after"] = model_budget.public_summary()
                write_json(output / "study-report.json", report)
                if report["stop_reason"]:
                    break
        finally:
            report["status"] = "finished" if len(report["samples"]) == 24 and not report["stop_reason"] else "partial"
            report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            report["budget_after"] = model_budget.public_summary()
            write_json(output / "study-report.json", report)
            final_rows = ledger_rows(policy["ledger_path"])
            if final_rows[:331] != initial_rows:
                raise ValueError("累计历史台账发生改变")
            clone_ledger(policy["ledger_path"], output / "campaign-ledger-final.sqlite")
            write_json(output / "file-manifest.json", {"files": {p.relative_to(output).as_posix(): digest(p)
                       for p in output.rglob("*") if p.is_file() and p.name != "file-manifest.json"}})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", required=True)
    args = parser.parse_args()
    result = prepare(args.prepare)
    print(json.dumps({"status": "prepared", "planned_requests": result["planned_requests"], "calls_made": 0}))
