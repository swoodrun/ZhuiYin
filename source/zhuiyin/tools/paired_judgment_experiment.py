"""Fixed, one-request judgments about frozen paired database snapshots.

No business tool is executed, including a tool proposed by the model. The
credential is an in-memory argument. The provider client has no retry path.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_budget
from paired_evidence import assess
from paired_judgment_design import build_design

VERSION = "paired-frozen-judgment-collection-v1"
BASELINE = ROOT / "data/evaluations/eight-arm-real-20261018/campaign"
ENDPOINT = "https://api.deepseek.com/chat/completions"
IDENTITY = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    temporary.replace(path)


@contextmanager
def policy_environment(path):
    old = os.environ.get(model_budget.POLICY_ENV)
    os.environ[model_budget.POLICY_ENV] = str(Path(path).resolve())
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(model_budget.POLICY_ENV, None)
        else:
            os.environ[model_budget.POLICY_ENV] = old


def ledger_rows(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY sequence")]


def request_payload(design, world):
    payload = deepcopy(world["payload"])
    payload.update(model="deepseek-flash", thinking={"type": "disabled"}, temperature=0,
                   max_tokens=1200, stream=False,
                   tool_choice={"type": "function", "function": {"name": "finish_task"}})
    payload["messages"].append({"role": "user", "content": design["judgment_instruction"]})
    return payload


def request_map(design):
    return {(branch["branch_id"], world["world_id"]): request_payload(design, world)
            for branch in design["branches"] for world in branch["worlds"]}


def prepare(output, baseline=BASELINE):
    output, baseline = Path(output).resolve(), Path(baseline).resolve()
    if output.exists():
        raise FileExistsError("准备目录已存在，不能覆盖固定方案")
    design = build_design()
    payloads = request_map(design)
    policy = json.loads((baseline / "campaign-policy.json").read_text(encoding="utf-8"))
    source_ledger = baseline / "campaign-ledger-final.sqlite"
    history = ledger_rows(source_ledger)
    if len(history) != 301 or sum(row["charged_micros"] for row in history) != 809685:
        raise ValueError("累计起点与已验收301次台账不符")
    if policy["max_requests"] != 374 or Decimal(policy["max_cost_cny"]) != 10:
        raise ValueError("累计授权不得变更")
    # Check the complete conservative capacity before accepting a credential.
    maximum = sum((len(json.dumps(payloads[(sample["branch_id"], sample["world_id"])], ensure_ascii=True).encode())
                   + model_budget.INPUT_PADDING) * 3 + 1200 * 12 for sample in design["execution_plan"])
    if len(history) + design["planned_requests"] > 374 or 809685 + maximum > 10_000_000:
        raise ValueError("完整计划超出已有累计授权")
    output.mkdir(parents=True)
    campaign = output / "campaign"
    campaign.mkdir()
    for name in ("campaign-ledger-initial.sqlite", "campaign-ledger.sqlite"):
        with closing(sqlite3.connect(source_ledger.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
            with closing(sqlite3.connect(campaign / name)) as target:
                source.backup(target)
    policy["ledger_path"] = str(campaign / "campaign-ledger.sqlite")
    write_json(campaign / "campaign-policy.json", policy)
    policy_path = campaign / "campaign-policy.json"
    with policy_environment(policy_path):
        initial = model_budget.public_summary()
    write_json(campaign / "initial-summary.json", initial)
    write_json(output / "fixed-design.json", design)
    (output / "requests").mkdir()
    for (branch, world), payload in payloads.items():
        (output / "requests" / f"{branch}--{world}.json").write_bytes(canonical(payload))
    (output / "source_snapshot").mkdir()
    for file in ("paired_judgment_design.py", "paired_evidence.py", "model_budget.py", "engine.py",
                 "tools/verify_paired_evidence.py", "tools/paired_judgment_experiment.py"):
        dest = output / "source_snapshot" / file
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / file, dest)
    write_json(output / "readiness.json", {"status": "ready_no_model_calls", "model_calls": 0,
               "collector_version": VERSION, "initial_requests": 301, "initial_cost_cny": "0.809685",
               "planned_requests": design["planned_requests"], "planned_worst_reservation_cny": f"{maximum / 1_000_000:.6f}",
               "max_cumulative_requests_after_plan": 301 + design["planned_requests"],
               "cumulative_request_ceiling": 374, "cumulative_cost_ceiling_cny": "10",
               "execution_rule": "30 fixed one-request samples; no tool execution, no retries, stop on infrastructure or budget failure"})
    files = {file.relative_to(output).as_posix(): digest(file) for file in output.rglob("*") if file.is_file()
             and file.name != "campaign-ledger.sqlite"}
    write_json(output / "preparation-manifest.json", {"files": files,
               "mutable_file": "campaign/campaign-ledger.sqlite", "historical_ledger_sha256": digest(source_ledger)})
    return validate_prepared(output)


def validate_prepared(prepared):
    prepared = Path(prepared).resolve()
    manifest = json.loads((prepared / "preparation-manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["files"].items():
        path = (prepared / relative).resolve()
        if not path.is_relative_to(prepared) or digest(path) != expected:
            raise ValueError("固定材料哈希发生变化")
    design = json.loads((prepared / "fixed-design.json").read_text(encoding="utf-8"))
    if design != build_design():
        raise ValueError("冻结设计或原始配对证据发生变化")
    for (branch, world), payload in request_map(design).items():
        if canonical(payload) != (prepared / "requests" / f"{branch}--{world}.json").read_bytes():
            raise ValueError("实际请求与固定设计不一致")
    for file in (prepared / "source_snapshot").rglob("*.py"):
        if digest(file) != digest(ROOT / file.relative_to(prepared / "source_snapshot")):
            raise ValueError("执行代码与已固定源码不同")
    return design


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


class ProviderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def provider_call(body, api_key):
    request = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                     headers={"Authorization": "Bearer " + api_key,
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=70) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ProviderError("response_too_large")
        # A provider must never cause the credential to enter an artifact.
        if api_key.encode() in raw:
            raise ProviderError("credential_echo_rejected")
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except urllib.error.HTTPError as exc:
        raise ProviderError("http_" + str(exc.code)) from None
    except (OSError, ValueError, UnicodeError):
        raise ProviderError("connection_or_response_error") from None


def classify(response):
    """Separate no report, malformed report, interrupted generation and unknown."""
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return {"status": "invalid_response", "outcome": None}
    choice = choices[0]
    if choice.get("finish_reason") not in ("stop", "tool_calls"):
        return {"status": "generation_incomplete", "outcome": None}
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return {"status": "invalid_response", "outcome": None}
    calls = message.get("tool_calls")
    if not calls:
        return {"status": "no_report", "outcome": None}
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return {"status": "invalid_report", "outcome": None}
    call = calls[0]
    if call.get("type") != "function" or not isinstance(call.get("function"), dict):
        return {"status": "invalid_report", "outcome": None}
    if call["function"].get("name") != "finish_task":
        return {"status": "non_judgment_tool_proposed", "outcome": None}
    try:
        args = json.loads(call["function"]["arguments"], object_pairs_hook=unique_object)
        if (not isinstance(args, dict) or set(args) != {"outcome", "reason"}
                or args["outcome"] not in ("success", "failure", "unknown")
                or not isinstance(args["reason"], str) or not args["reason"].strip()):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        return {"status": "invalid_report", "outcome": None}
    return {"status": "valid", "outcome": args["outcome"], "reason": args["reason"]}


def wilson(successes, total):
    if not total:
        return None
    z = 1.959963984540054
    p, den = successes / total, 1 + z * z / total
    center = (p + z * z / (2 * total)) / den
    half = z * math.sqrt(p * (1-p) / total + z*z / (4*total*total)) / den
    return {"lower": max(0, center-half), "upper": min(1, center+half), "confidence": .95,
            "method": "Wilson descriptive interval; repeated fixed prompt, not independent task generalization"}


def summarize(design, samples):
    groups = []
    for branch in design["branches"]:
        for world in branch["worlds"]:
            rows = [s for s in samples if (s["branch_id"], s["world_id"]) == (branch["branch_id"], world["world_id"])]
            valid = [r for r in rows if r["status"] == "valid"]
            counts = {key: sum(r.get("outcome") == key for r in valid) for key in ("success", "failure", "unknown")}
            qualified = sum(r.get("assessment", {}).get("evidence_assessment") == "supported_correct" for r in valid)
            groups.append({"branch_id": branch["branch_id"], "world_id": world["world_id"], "planned": 5,
                           "attempted": len(rows), "valid": len(valid), "not_run": 5-len(rows),
                           "non_report_or_exception": len(rows)-len(valid), "outcomes": counts,
                           "actual_success": world["actual_success"], "supported_outcome": world["supported_outcome"],
                           "qualified_correct": qualified, "qualified_correct_ci95_over_attempted": wilson(qualified, len(rows)),
                           "false_success": sum(r.get("assessment", {}).get("false_success", False) for r in valid),
                           "false_failure": sum(r.get("assessment", {}).get("false_failure", False) for r in valid),
                           "unsupported_claim": sum(r.get("assessment", {}).get("evidence_assessment") == "unsupported_claim" for r in valid)})
    return {"groups": groups, "attempted": len(samples), "valid": sum(s["status"] == "valid" for s in samples),
            "not_run": design["planned_requests"]-len(samples), "business_actions_executed": 0,
            "scope": "Current frozen state judgments only; correct failure is not task completion; no recovery or optimal evidence claim"}


def execute(prepared, output, api_key, transport=provider_call):
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    if not isinstance(api_key, str) or len(api_key.strip()) < 8:
        raise ValueError("需要仅在内存中的连接凭据")
    design = validate_prepared(prepared)
    policy_path = prepared / "campaign/campaign-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    initial_rows = ledger_rows(prepared / "campaign/campaign-ledger-initial.sqlite")
    if ledger_rows(policy["ledger_path"]) != initial_rows:
        raise ValueError("本方案台账已使用，禁止自动重跑")
    output.mkdir(parents=True, exist_ok=False)
    (output / "samples").mkdir()
    report = {"collector_version": VERSION, "task_mode": "frozen_state_judgment", "status": "running",
              "preparation_manifest_sha256": digest(prepared / "preparation-manifest.json"),
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "samples": [], "stop_reason": None}
    worlds = {(b["branch_id"], w["world_id"]): w for b in design["branches"] for w in b["worlds"]}
    payloads = request_map(design)
    with policy_environment(policy_path):
        report["budget_before"] = model_budget.public_summary()
        write_json(output / "study-report.json", report)
        for sample in design["execution_plan"]:
            item = deepcopy(sample)
            branch, world = item["branch_id"], item["world_id"]
            body = canonical(payloads[(branch, world)])
            item.update(request_sha256=hashlib.sha256(body).hexdigest(), business_actions_executed=0)
            reservation = None
            response = None
            usage_record = {"status": "error", "usage_available": False}
            started = time.monotonic()
            try:
                reservation = model_budget.reserve_request(IDENTITY, payloads[(branch, world)])
                if reservation is None:
                    raise model_budget.CampaignBudgetError("policy_required")
                item["reservation_id"] = reservation.reservation_id
                response = transport(body, api_key)
                if not isinstance(response, dict) or api_key in json.dumps(response, ensure_ascii=False):
                    raise ProviderError("invalid_or_credential_response")
                usage = response.get("usage", {})
                if (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens"))):
                    usage_record = {"status": "ok", "usage_available": True, "prompt_tokens": usage["prompt_tokens"],
                                    "completion_tokens": usage["completion_tokens"]}
                item.update(classify(response))
                if item["status"] == "valid":
                    w = worlds[(branch, world)]
                    item["assessment"] = assess(item["outcome"], w["supported_outcome"], w["actual_success"])
            except model_budget.CampaignBudgetError as exc:
                item.update(status="budget_error", outcome=None, error_code=exc.reason)
                report["stop_reason"] = "budget_guard"
            except ProviderError as exc:
                item.update(status="infra_error", outcome=None, error_code=exc.code)
                report["stop_reason"] = "provider_or_connection_error"
            except Exception:
                item.update(status="infra_error", outcome=None, error_code="unexpected_collection_error")
                report["stop_reason"] = "collection_error"
            finally:
                if reservation is not None:
                    try:
                        model_budget.settle_request(reservation, usage_record)
                    except model_budget.CampaignBudgetError as exc:
                        item["settlement_error"] = exc.reason
                        report["stop_reason"] = "budget_guard"
                item["elapsed_seconds"] = round(time.monotonic()-started, 3)
            number = len(report["samples"]) + 1
            folder = output / "samples" / f"{number:02d}"
            folder.mkdir()
            (folder / "request.json").write_bytes(body)
            if response is not None and item.get("error_code") != "invalid_or_credential_response":
                write_json(folder / "response.json", response)
            item["usage"] = usage_record
            write_json(folder / "sample.json", item)
            report["samples"].append(item)
            report["summary"] = summarize(design, report["samples"])
            report["budget_after"] = model_budget.public_summary()
            write_json(output / "study-report.json", report)
            if report["stop_reason"]:
                break
        report["status"] = "finished" if len(report["samples"]) == design["planned_requests"] and not report["stop_reason"] else "partial"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "study-report.json", report)
    final_rows = ledger_rows(policy["ledger_path"])
    if final_rows[:len(initial_rows)] != initial_rows:
        raise ValueError("累计历史台账发生改变")
    with closing(sqlite3.connect(Path(policy["ledger_path"]).as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(output / "campaign-ledger-final.sqlite")) as target:
            source.backup(target)
    write_json(output / "file-manifest.json", {"files": {p.relative_to(output).as_posix(): digest(p) for p in output.rglob("*") if p.is_file()}})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", required=True)
    args = parser.parse_args()
    design = prepare(args.prepare)
    print(json.dumps({"status": "prepared", "planned_requests": design["planned_requests"], "calls_made": 0}))


if __name__ == "__main__":
    main()
