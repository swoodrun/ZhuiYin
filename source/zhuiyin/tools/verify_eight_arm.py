"""Offline internal-consistency checks for one fixed eight-arm collection.

Imports no engine, study executor, model client or archived Python. Report SQL
is never executed. Input databases are opened read-only; outputs use a new folder.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_evidence import EXECUTION_CODES, trial_facts

VERSION = "eight-arm-offline-consistency-v1"
HISTORY = ROOT / "data/evaluations/v061-review-20261017/campaign-ledger-final.sqlite"
MAX_FILE = 16 * 1024 * 1024
STRATEGIES = ("baseline", "idempotency_only", "verification_only", "verified")
POLICIES = ("neutral", "recovery")
LIMITATIONS = [
    "仅核对输入材料内部一致性，不认证服务商身份、真实网络交互或完整模型对话。",
    "哈希不是作者签名；不能发现所有材料被同时一致改写。",
    "费用是本地保守记账，不是服务商账单；预留字节数不是 tokenizer 上界证明。",
    "网络错误、取消和故障注入事件按保存的来源声明核对，不提供外部真实性认证。",
    "不执行报告中的 SQL；只读检查另存数据库，不逐行重放全部工具行为。",
    "不核对自由文本结论、延迟指标和未列出的统计量；两轮观察不证明总体优越、交互、因果或泛化。",
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE:
        raise ValueError("输入单文件超过核对上限")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(root, name):
    part = PurePosixPath(name)
    if not isinstance(name, str) or not name or part.is_absolute() or ".." in part.parts or "\\" in name or ":" in name:
        raise ValueError("不安全输入路径")
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("输入路径超出材料目录")
    return target


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE:
        raise ValueError("输入 JSON 过大")
    return json.loads(path.read_text(encoding="utf-8"))


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    return db


def ledger_rows(path):
    with closing(readonly(path)) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY sequence")]
        meta = dict(db.execute("SELECT key,value FROM metadata"))
        seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()
        return rows, meta, seq[0] if seq else 0


def wilson(successes, size):
    """Independent quadratic score-bound calculation, with no executor import."""
    if not size:
        return None
    z2 = 3.841458820694124
    center_numerator = 2 * successes + z2
    root = math.sqrt(z2 * (z2 + 4 * successes * (1 - successes / size)))
    denominator = 2 * (size + z2)
    return [max(0.0, (center_numerator - root) / denominator),
            min(1.0, (center_numerator + root) / denominator)]


def rate(count, denominator):
    return {"numerator": count, "denominator": denominator,
            "rate": count / denominator if denominator else None,
            "ci95": wilson(count, denominator)}


def equivalent(left, right):
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-12)
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(equivalent(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def usage_facts(records, options):
    known = [r for r in records if r.get("usage_available") is True
             and all(type(r.get(key)) is int and r[key] >= 0 for key in ("prompt_tokens", "completion_tokens"))]
    prompt = sum(r.get("prompt_tokens", 0) for r in records if type(r.get("prompt_tokens")) is int and r["prompt_tokens"] >= 0)
    completion = sum(r.get("completion_tokens", 0) for r in records if type(r.get("completion_tokens")) is int and r["completion_tokens"] >= 0)
    unknown = len(records) - len(known)
    available = unknown == 0
    cost = None if not available else float((Decimal(prompt) * Decimal(str(options["input_price_per_million"]))
                                           + Decimal(completion) * Decimal(str(options["output_price_per_million"]))) / 1_000_000)
    return {"requests": len(records), "successful_requests": sum(r.get("status") == "ok" for r in records),
            "failed_requests": sum(r.get("status") != "ok" for r in records),
            "prompt_tokens": prompt if available else None, "completion_tokens": completion if available else None,
            "total_tokens": prompt + completion if available else None,
            "known_prompt_tokens": prompt, "known_completion_tokens": completion,
            "usage_unknown_requests": unknown, "estimated_cost": cost}


def activations(events):
    lost, stale = [], []
    for event in events:
        if event.get("kind") != "fault":
            continue
        seen, actual = event.get("observed"), event.get("actual")
        if event.get("tool") == "create_ticket" and isinstance(seen, dict) and seen.get("code") == "COMMIT_UNKNOWN":
            lost.append(event.get("step"))
        if (event.get("tool") == "find_ticket" and isinstance(seen, dict) and seen.get("work_orders") == []
                and isinstance(actual, dict) and actual.get("work_orders")):
            stale.append(event.get("step"))
    return {"commit_response_lost": bool(lost), "stale_read": bool(stale),
            "commit_response_lost_steps": lost, "stale_read_steps": stale,
            "definition": "仅按保存的故障事件识别激活；配置选中不等于实际激活。"}


def verify(run, prepared, policy_path, *, history=HISTORY):
    """Return structured checks; never write inputs and never call a model."""
    run, prepared = Path(run).resolve(), Path(prepared).resolve()
    checks, trials, groups = [], [], []
    accounting = None
    ledger_input = None

    def check(label, value, category="structure"):
        checks.append({"name": label, "passed": bool(value), "category": category})

    def fields(label, source, expected, category):
        for key, value in expected.items():
            check(label + ":" + key, equivalent(source.get(key), value), category)

    try:
        report = read_json(run / "study-report.json")
        design = read_json(prepared / "fixed-design.json")
        lock = read_json(prepared / "design-sha256.json")
        index = read_json(run / "trial-index.json")
        manifest = read_json(run / "file-manifest.json")
        mode = report["mode"]
        check("模式受支持", mode in ("rules", "llm"))
        check("固定设计锁", hashlib.sha256(canonical(design)).hexdigest() == lock.get("sha256"), "provenance")
        check("运行固定设计一致", read_json(run / "fixed-design.json") == design, "provenance")
        actual_files = {p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()}
        check("运行文件清单完整", set(manifest) == actual_files - {"file-manifest.json"}, "archive")
        for name, expected in manifest.items():
            check("运行文件哈希:" + name, digest(inside(run, name)) == expected, "archive")
        for name, expected in design["runtime_sha256"].items():
            check("保存源码哈希:" + name, digest(inside(prepared / "source_snapshot", name)) == expected, "provenance")
        check("采集器保存源码哈希", digest(prepared / "source_snapshot/eight_arm_experiment.py") == design["collector_sha256"], "provenance")
        registration = inside(ROOT, design["registration_file"])
        check("原注册哈希", digest(registration) == design["registration_sha256"], "provenance")
        registered = read_json(registration)
        options, config = design["options"], design["config"]
        check("固定八组两轮设计", options["strategies"] == list(STRATEGIES) and options["prompt_policies"] == list(POLICIES)
              and options["repetitions"] == 2 and design["planned_trials"] == 16
              and design["max_requests_per_trial"] == 9 and design["worst_case_requests"] == 144)
        check("固定业务配置", config["scenario"] == "ticket" and config["seed"] == 42
              and config["faults"] == ["commit_response_lost", "stale_read"])
        fields("原注册业务参数", config, {key: registered[key] for key in
               ("scenario", "ticket_title", "ticket_priority", "faults", "seed")}, "provenance")
        fields("原注册分组参数", options, {key: registered[key] for key in
               ("strategies", "prompt_policies", "repetitions", "order_mode")}, "provenance")
        check("原注册模型参数", design["model_identity"]["model"] == registered["model"]
              and design["model_identity"]["thinking"] == registered["thinking"], "provenance")
        report_config = dict(config, agent_mode=mode)
        fields("报告固定字段", report, {"kind": "study", "config": report_config, "options": options,
               "evaluation_protocol": "business-report-v2", "execution_plan": design["execution_plan"],
               "factor_design": design["factor_design"], "planned": 16}, "provenance")
        check("报告源码哈希", report["provenance"]["source_sha256"] == design["runtime_sha256"], "provenance")
        if mode == "llm":
            fields("模型标识", report["model_identity"], design["model_identity"], "provenance")
        planned = [{**item, "trial_index": row["trial_index"]}
                   for row in design["execution_plan"] for item in row["trials"]]
        fixed_order = [(1, "neutral", "baseline"), (1, "neutral", "idempotency_only"),
                       (1, "neutral", "verification_only"), (1, "neutral", "verified"),
                       (1, "recovery", "idempotency_only"), (1, "recovery", "verification_only"),
                       (1, "recovery", "verified"), (1, "recovery", "baseline"),
                       (2, "recovery", "verification_only"), (2, "recovery", "verified"),
                       (2, "recovery", "baseline"), (2, "recovery", "idempotency_only"),
                       (2, "neutral", "idempotency_only"), (2, "neutral", "verification_only"),
                       (2, "neutral", "verified"), (2, "neutral", "baseline")]
        check("种子42固定执行顺序", [(p["trial_index"], p["prompt_policy"], p["strategy"]) for p in planned] == fixed_order, "order")
        samples = report["samples"]
        check("计划前缀长度", len(samples) <= len(planned) == 16)
        check("索引样本数量", len(index["trials"]) == len(samples))
        check("独立逐轮JSON文件数量", {p.relative_to(run).as_posix() for p in (run / "trials").glob("trial-*.json")}
              == {f"trials/trial-{i:02d}.json" for i in range(1, len(samples) + 1)}, "archive")
        expected_mode = "real_model_attempts" if mode == "llm" else "rules_rehearsal_not_model_evidence"
        check("索引证据模式", index["evidence_mode"] == expected_mode)
        records = []
        sqlite_files = set()
        for number, sample in enumerate(samples, 1):
            tag = f"第{number:02d}轮"
            entry = index["trials"][number - 1]
            expected = planned[number - 1]
            fields(tag + "计划", sample, {**expected, "execution_index": number,
                   "arm_id": expected["prompt_policy"] + ":" + expected["strategy"]}, "order")
            fields(tag + "索引", entry, {"execution_index": number, "arm_id": sample["arm_id"],
                   "trial_index": sample["trial_index"], "status": sample["status"],
                   "json": f"trials/trial-{number:02d}.json"}, "order")
            raw = read_json(inside(run, entry["json"]))
            check(tag + "完整样本一致", raw["sample"] == sample, "trial")
            check(tag + "证据模式", raw["evidence_mode"] == expected_mode)
            result, events = raw["result"], raw["result"].get("events", [])
            # The study executor can retain a minimal initialization-error stub,
            # which has no database and cannot support full business adjudication.
            stub = set(result) == {"failed", "actual_success", "failure_fingerprint", "violations", "stats"}
            if stub:
                check(tag + "初始化错误桩", result["failed"] is True and result["actual_success"] is False
                      and result["failure_fingerprint"] == "MODEL_EXECUTION_ERROR"
                      and result["violations"] == [{"code": "MODEL_EXECUTION_ERROR"}]
                      and sample["status"] in ("infra_error", "cancelled"), "trial")
                facts = {"actual": False, "completed": False, "fingerprint": "MODEL_EXECUTION_ERROR"}
                check(tag + "缺失数据库明确标记", entry.get("database") is None
                      and entry.get("database_status") == "not_available", "database")
            else:
                facts = trial_facts(result, config, tag, "repeated", check)
                fields(tag + "原始执行配置", result, {"agent_mode": mode, "scenario": "ticket",
                       "strategy": sample["strategy"], "prompt_policy": sample["prompt_policy"],
                       "evaluation_protocol": "business-report-v2"}, "trial")
                database = f"trials/trial-{number:02d}.sqlite"
                fields(tag + "数据库索引", entry, {"database": database,
                       "database_status": "matched_fresh_trial_records"}, "database")
                sqlite_files.add(database)
                with closing(readonly(inside(run, database))) as db:
                    check(tag + "SQLite完整性", db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "database")
                    rows = [dict(row) for row in db.execute("SELECT * FROM work_orders ORDER BY id")]
                    check(tag + "SQLite业务行", rows == result["business_records"], "database")
                    check(tag + "请求隔离", all(r.get("request_id") == config["request_id"] for r in rows), "database")
            codes = set(facts["fingerprint"].split("|")) - {""}
            business = codes - EXECUTION_CODES - {"REPORT_MISMATCH"}
            reported = result.get("reported_outcome")
            assessment = ("unreported" if reported is None else "uncertain" if reported == "unknown" else
                          "consistent" if (reported == "success") == facts["actual"] else "inconsistent")
            reports = [event for event in events if event.get("tool") == "finish_task"]
            check(tag + "报告事件与摘要", (not reports and reported is None) or
                  len(reports) == 1 and reports[0].get("observed", {}).get("outcome") == reported
                  and reports[0].get("observed", {}).get("success") is result.get("reported_success"), "trial")
            expected_status = "valid" if facts["completed"] else sample["status"]
            check(tag + "有效或执行错误分类", sample["status"] == expected_status
                  and (facts["completed"] or sample["status"] in ("infra_error", "cancelled")), "trial")
            fields(tag + "重算样本", sample, {"completed": facts["completed"], "actual_success": facts["actual"],
                   "failure_fingerprint": facts["fingerprint"], "failed": bool(codes),
                   "business_failed": bool(business), "report_failed": "REPORT_MISMATCH" in codes,
                   "reported_outcome": reported, "reported_success": result.get("reported_success"),
                   "report_assessment": assessment, "evaluation_protocol": "business-report-v2",
                   "events": events, "business_records": result.get("business_records", result.get("reservations", [])),
                   "stats": {key: result.get("stats", {}).get(key, 0) for key in
                             ("tool_calls", "physical_writes", "idempotency_hits", "verification_calls")}}, "trial")
            for key, expected_codes in (("business_violations", business), ("reporting_violations", codes & {"REPORT_MISMATCH"}),
                                        ("execution_violations", codes & EXECUTION_CODES)):
                check(tag + ":" + key, {v["code"] for v in sample[key]} == expected_codes, "trial")
            check(tag + "事件顺序", [e.get("step") for e in events] == list(range(1, len(events) + 1)), "trial")
            if mode == "llm" and not stub:
                factor = design["factor_design"]["manifests_by_prompt_policy"][sample["prompt_policy"]][sample["strategy"]]
                check(tag + "提示及能力契约", result["factor_manifest"] == sample["factor_manifest"] == factor, "provenance")
            elif mode == "rules":
                check(tag + "规则模式无模型契约", result.get("factor_manifest") is None and sample.get("factor_manifest") is None, "provenance")
            request_records = raw["request_records"]
            check(tag + "请求上限", len(request_records) <= 9 and (mode == "llm" or not request_records), "usage")
            for pos, record in enumerate(request_records, 1):
                check(tag + f"请求{pos}状态", record.get("status") in ("ok", "http_error", "network_error", "invalid_response"), "usage")
                available = all(type(record.get(key)) is int and record[key] >= 0 for key in ("prompt_tokens", "completion_tokens"))
                check(tag + f"请求{pos}用量可用性", record.get("usage_available") is available, "usage")
            fields(tag + "请求汇总", sample["model_usage"], usage_facts(request_records, options), "usage")
            records.extend(request_records)
            trials.append({"execution_index": number, "arm_id": sample["arm_id"], "trial_index": sample["trial_index"],
                           "status": sample["status"], "actual_success": facts["actual"],
                           "reported_outcome": reported, "business_failed": bool(business),
                           "report_failed": "REPORT_MISMATCH" in codes, "failed": bool(codes),
                           "requests": len(request_records), "database_available": not stub,
                           "activation": activations(events), "fingerprint": facts["fingerprint"]})
        check("独立数据库文件集合", {p.relative_to(run).as_posix() for p in (run / "trials").glob("trial-*.sqlite")} == sqlite_files, "database")
        order = [{"sequence": number, **{key: sample[key] for key in
                  ("trial_index", "prompt_policy", "policy_order_position", "order_position", "strategy", "status")}}
                 for number, sample in enumerate(samples, 1)]
        check("实际执行顺序摘要", report["execution_order"] == order, "order")
        check("已尝试轮次计数", type(report["completed"]) is int and report["completed"] == len(samples))
        finished = len(samples) == 16 and report.get("stopped_reason") is None
        check("研究完成状态", report["status"] == ("finished" if finished else "partial"))
        state = read_json(run / "run-state.json")
        fields("运行状态文件", state, {"mode": mode, "status": report["status"], "new_model_requests": len(records),
               "trials_indexed": len(samples), "trial_json_files_retained": len(samples),
               "trial_databases_retained": len(sqlite_files)}, "structure")
        fields("研究用量", report["model_usage"], usage_facts(records, options), "usage")
        check("研究用量上限", report["model_usage"].get("limit") == 144 and len(records) <= 144, "usage")
        check("模型执行标志", report["model_usage"].get("external_model_verified") is (mode == "llm" and bool(records)), "usage")
        arms = report["arms"]
        check("八组唯一集合", len(arms) == 8 and {a["arm_id"] for a in arms}
              == {p + ":" + s for p in POLICIES for s in STRATEGIES}, "statistics")
        for arm in arms:
            identifier = arm["arm_id"]
            observed = [t for t in trials if t["arm_id"] == identifier]
            valid = [t for t in observed if t["status"] == "valid"]
            actual = sum(t["actual_success"] for t in valid)
            confirmed = sum(t["actual_success"] and t["reported_outcome"] == "success" for t in valid)
            failed = sum(t["failed"] for t in valid)
            biz = sum(t["business_failed"] for t in valid)
            reporting = sum(t["report_failed"] for t in valid)
            unknown = sum(t["reported_outcome"] == "unknown" for t in valid)
            counts = {"planned": 2, "attempted": len(observed), "valid_trials": len(valid), "business_failures": failed,
                      "successes": actual, "business_effect_failures": biz, "report_errors": reporting,
                      "uncertain_reports": unknown, "confirmed_completions": confirmed,
                      "infra_errors": sum(t["status"] == "infra_error" for t in observed),
                      "cancelled": sum(t["status"] == "cancelled" for t in observed), "unrun": 2 - len(observed),
                      "report_outcomes": {outcome: sum(t["reported_outcome"] == outcome for t in valid)
                                          for outcome in ("success", "failure", "unknown")}}
            fields(identifier + "计数", arm, counts, "statistics")
            for key, count, denominator in (("task_completion_rate", actual, len(valid)),
                    ("confirmed_completion_rate", confirmed, len(valid)), ("business_failure_rate", failed, len(valid)),
                    ("business_effect_failure_rate", biz, len(valid)), ("report_error_rate", reporting, len(valid)),
                    ("uncertain_report_rate", unknown, len(valid)), ("attempted_completion_rate", actual, len(observed))):
                check(identifier + ":" + key, equivalent(arm.get(key), rate(count, denominator)), "statistics")
            groups.append({"arm_id": identifier, **counts,
                           "confirmed_completion_rate": rate(confirmed, len(valid)),
                           "task_completion_rate": rate(actual, len(valid)),
                           "business_effect_failure_rate": rate(biz, len(valid)),
                           "report_error_rate": rate(reporting, len(valid)),
                           "uncertain_report_rate": rate(unknown, len(valid)),
                           "infrastructure_error_rate": rate(counts["infra_errors"], len(observed)),
                           "attempted_completion_rate": rate(actual, len(observed)),
                           "commit_response_lost_activated": sum(t["activation"]["commit_response_lost"] for t in observed),
                           "stale_read_activated": sum(t["activation"]["stale_read"] for t in observed)})
        policy = read_json(policy_path)
        public = {"schema_version": 1, "allowed_origin": policy["allowed_origin"], "allowed_model": policy["allowed_model"],
                  "max_requests": policy["max_requests"], "max_cost_cny": format(Decimal(str(policy["max_cost_cny"])), "f"),
                  "input_price_per_million": format(Decimal(str(policy["input_price_per_million"])), "f"),
                  "output_price_per_million": format(Decimal(str(policy["output_price_per_million"])), "f"),
                  "currency": "CNY", "input_padding": 8192, "max_output_tokens": 1200}
        policy_json = json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        policy_hash = hashlib.sha256(policy_json.encode()).hexdigest()
        check("授权政策", public["allowed_origin"] == "https://api.deepseek.com" and public["allowed_model"] == "deepseek-flash"
              and public["max_requests"] == (374 if mode == "llm" else public["max_requests"])
              and 230 <= public["max_requests"] <= 374 and Decimal(public["max_cost_cny"]) == 10
              and Decimal(public["input_price_per_million"]) == 3 and Decimal(public["output_price_per_million"]) == 12, "ledger")
        check("设计政策哈希", design["expected_campaign_policy_sha256"] == policy_hash, "ledger")
        # A portable frozen package retains the original policy, including its
        # historical absolute path. Prefer its adjacent final snapshot; never
        # silently fall back to the active ledger if that snapshot is invalid.
        snapshot = Path(policy_path).resolve().parent / "campaign-ledger-final.sqlite"
        ledger_path = snapshot if snapshot.is_file() else Path(policy["ledger_path"])
        ledger_input = {"kind": "frozen_snapshot" if ledger_path == snapshot else "active_policy_path",
                        "file": ledger_path.name}
        ledger, meta, sequence = ledger_rows(ledger_path)
        previous, _, old_sequence = ledger_rows(history)
        check("原230条基线", len(previous) == old_sequence == 230 and sum(r["charged_micros"] for r in previous) == 612582, "ledger")
        check("原230条逐字段保全", ledger[:230] == previous, "ledger")
        check("累计台账政策", meta.get("policy_json") == policy_json and meta.get("policy_sha256") == policy_hash, "ledger")
        check("累计请求连续性", sequence == len(ledger) and [r["sequence"] for r in ledger] == list(range(1, len(ledger) + 1)), "ledger")
        suffix = ledger[230:]
        check("新增台账逐请求数量", len(suffix) == len(records), "ledger")
        check("请求授权上限", len(ledger) <= public["max_requests"], "ledger")
        input_price, output_price = Decimal(public["input_price_per_million"]), Decimal(public["output_price_per_million"])
        def money(prompt, completion):
            return int((Decimal(prompt) * input_price + Decimal(completion) * output_price).to_integral_value(rounding=ROUND_CEILING))
        for number, row in enumerate(ledger, 1):
            reserve = money(row["reserved_prompt_tokens"], row["reserved_completion_tokens"])
            check(f"台账{number}预留", row["reserved_prompt_tokens"] >= 8192 and row["reserved_completion_tokens"] == 1200
                  and row["reserved_micros"] == reserve, "ledger")
            if row["state"] in ("settled", "overrun"):
                actual_cost = money(row["actual_prompt_tokens"], row["actual_completion_tokens"])
                overrun = row["actual_prompt_tokens"] > row["reserved_prompt_tokens"] or row["actual_completion_tokens"] > 1200
                check(f"台账{number}结算", row["state"] == ("overrun" if overrun else "settled")
                      and row["charged_micros"] == (max(actual_cost, reserve) if overrun else actual_cost), "ledger")
            else:
                check(f"台账{number}未知费用", row["state"] in ("reserved", "unknown")
                      and row["actual_prompt_tokens"] is None and row["actual_completion_tokens"] is None
                      and row["charged_micros"] == reserve, "ledger")
        for number, (row, record) in enumerate(zip(suffix, records), 231):
            known = record.get("status") == "ok" and record.get("usage_available") is True
            if known:
                check(f"新请求{number}用量", row["state"] in ("settled", "overrun")
                      and row["actual_prompt_tokens"] == record["prompt_tokens"]
                      and row["actual_completion_tokens"] == record["completion_tokens"], "ledger")
            else:
                check(f"新请求{number}未知用量保守留账", row["state"] in ("unknown", "reserved"), "ledger")
        total = sum(row["charged_micros"] for row in ledger)
        check("累计费用授权上限", total <= Decimal(public["max_cost_cny"]) * 1_000_000, "ledger")
        check("超量停止标志", (meta.get("halted_reason") == "usage_bound_exceeded") == any(r["state"] == "overrun" for r in ledger), "ledger")
        accounting = {"history_requests": 230, "new_requests": len(suffix), "cumulative_requests": len(ledger),
                      "new_accounted_cost_cny": f"{Decimal(total - 612582) / 1_000_000:.6f}",
                      "cumulative_accounted_cost_cny": f"{Decimal(total) / 1_000_000:.6f}",
                      "cumulative_request_limit": public["max_requests"], "cumulative_cost_limit_cny": public["max_cost_cny"]}
        if mode == "llm":
            fields("报告累计台账", report.get("campaign_budget", {}),
                   {"requests_made": len(ledger), "requests_remaining": public["max_requests"] - len(ledger),
                    "accounted_cost_cny": accounting["cumulative_accounted_cost_cny"], "policy_sha256": policy_hash}, "ledger")
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, sqlite3.Error, ArithmeticError):
        check("材料结构或读取错误", False)
    passed = bool(checks) and all(c["passed"] for c in checks)
    return {"verifier_version": VERSION, "passed": passed,
            "conclusion_status": "internally_consistent" if passed else "unverified",
            "effectiveness_claim": None,
            "checks": checks, "check_count": len(checks), "failed_checks": [c["name"] for c in checks if not c["passed"]],
            "trials": trials if passed else None, "arms": groups if passed else None,
            "accounting": accounting if passed else None,
            "ledger_input": ledger_input,
            "evidence_scope": "规则演练，不是真实模型效果" if 'mode' in locals() and mode == "rules" else "保存的真实模型尝试，内部一致性核对",
            "coverage": sorted({c["category"] for c in checks}), "not_covered": LIMITATIONS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--output", required=True, help="New output directory outside the evidence inputs")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.is_relative_to(Path(args.run).resolve()) or output.is_relative_to(Path(args.prepared).resolve()):
        raise ValueError("核验输出不能写入原始研究或准备目录")
    output.mkdir(parents=True, exist_ok=False)
    result = verify(args.run, args.prepared, args.policy)
    (output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 八组材料独立核对", "", "结论：" + ("范围内内部一致。" if result["passed"] else "未验证；不得引用分组和反例结论。"),
             f"检查 {result['check_count']} 项；失败 {len(result['failed_checks'])} 项。", "", result["evidence_scope"], "",
             "本结果不证明策略优越、交互、因果或泛化。", "", "## 未覆盖范围", ""]
    if result["ledger_input"]:
        source = result["ledger_input"]
        lines[6:6] = ["台账来源：" + ("政策同目录冻结快照" if source["kind"] == "frozen_snapshot" else "政策指定的活动台账")
                      + "（" + source["file"] + "）。", ""]
    lines.extend("- " + item for item in LIMITATIONS)
    if result["failed_checks"]:
        lines.extend(["", "## 失败项目", ""] + ["- " + item for item in result["failed_checks"]])
    (output / "核对结果.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": result["check_count"], "failed": len(result["failed_checks"])}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
