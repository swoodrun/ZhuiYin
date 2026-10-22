"""Independent, offline verification of a saved Zhuiyin v0.6 evidence package.

No project engine is imported, no network is used, no archived Python or SQL is
executed. The input stays unchanged; each invocation writes a new output folder.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import zipfile

MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 80 * 1024 * 1024
VERIFIER_VERSION = "evidence-consistency-v1.1"
EXECUTION_CODES = {"MODEL_EXECUTION_ERROR", "AGENT_DID_NOT_FINISH"}

COVERAGE = {
    "archive": "文件清单、字节哈希及归档结构",
    "trial": "逐轮业务快照、指纹、违规分类、完成状态及 failed 汇总",
    "minimization": "初始、候选和末轮逐项记录及其计数、状态和删减摘要",
    "database": "已执行原始组和组合组的 SQLite 快照与 JSON 记录",
    "provenance": "保存源码、提示定义和公开配置的哈希",
    "ledger": "政策、累计台账、新增请求用量及本地记账",
}
NOT_COVERED = [
    "模型服务来源或实际网络响应的外部认证",
    "服务商扣款账单、费用换算假设及预算预留的 tokenizer 上界证明",
    "归档作者身份、数字签名，或所有材料同时被一致改写时的真实性",
    "报告 SQL 文本的执行或语义验证；只读检查单独保存的 SQLite 快照",
    "取消、网络错误等来源声明在外部实际发生的真实性",
    "未列入检查的自由文本、图表及其他说明表述",
    "总体可靠性、统计独立、全局最小、因果及八组模型效果",
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Package:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.bytes_read = 0
        self.zip = None if self.path.is_dir() else zipfile.ZipFile(self.path)
        if self.zip:
            infos = self.zip.infolist()
            if (len(infos) > 2000 or len({i.filename for i in infos}) != len(infos)
                    or sum(i.file_size for i in infos) > MAX_TOTAL):
                raise ValueError("归档大小或条目数量不受支持")

    def close(self):
        if self.zip:
            self.zip.close()

    def read(self, name):
        part = PurePosixPath(name)
        if not name or part.is_absolute() or ".." in part.parts or "\\" in name or ":" in name:
            raise ValueError("归档含不安全路径")
        if self.zip:
            if self.zip.getinfo(name).file_size > MAX_FILE:
                raise ValueError("归档单文件过大")
            data = self.zip.read(name)
        else:
            path = (self.path / name).resolve()
            if not path.is_relative_to(self.path) or path.stat().st_size > MAX_FILE:
                raise ValueError("归档路径或大小不受支持")
            data = path.read_bytes()
        self.bytes_read += len(data)
        if self.bytes_read > MAX_TOTAL:
            raise ValueError("核验读取总量超过限制")
        return data

    def json(self, name):
        return json.loads(self.read(name))


@contextmanager
def readonly_database(data):
    with tempfile.TemporaryDirectory(prefix="zhuiyin-verify-") as directory:
        path = Path(directory) / "snapshot.sqlite"
        path.write_bytes(data)
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()


def recompute(trial, config):
    """Recompute business/report codes from saved rows, never from source labels."""
    rows = trial.get("business_records", trial.get("reservations", []))
    active = [r for r in rows if r["status"] == "active"]
    ticket = config["scenario"] == "ticket"
    fields = {"title": config["ticket_title"], "priority": config["ticket_priority"]} if ticket else {
        "equipment": config["equipment"], "quantity": config["quantity"]}
    matching = lambda row: all(type(row.get(k)) is type(v) and row.get(k) == v for k, v in fields.items())
    actual = len(active) == 1 and matching(active[0])
    codes = set()
    if len(active) > 1:
        codes.add("DUPLICATE_TICKET" if ticket else "DUPLICATE_RESERVATION")
    if any(not matching(row) for row in active):
        codes.add("PAYLOAD_MISMATCH")
    if not ticket:
        if trial["stock_remaining"] < 0:
            codes.add("NEGATIVE_STOCK")
        if config["stock"] - trial["stock_remaining"] != sum(r["quantity"] for r in active):
            codes.add("STOCK_INCONSISTENT")
    recorded_codes = {v["code"] for v in trial.get("violations", [])}
    if "MODEL_EXECUTION_ERROR" in recorded_codes:
        codes.add("MODEL_EXECUTION_ERROR")
    if not active and (ticket or config["stock"] >= config["quantity"]) and "MODEL_EXECUTION_ERROR" not in codes:
        codes.add("TASK_INCOMPLETE")
    outcome = trial.get("reported_outcome")
    if outcome is None:
        codes.add("AGENT_DID_NOT_FINISH")
    elif outcome in ("success", "failure") and (outcome == "success") != actual:
        codes.add("REPORT_MISMATCH")
    return actual, "|".join(sorted(codes))


def trial_facts(trial, config, label, role, check):
    """Validate source flags, but derive completion and errors from their facts."""
    outcomes = {"success": True, "failure": False, "unknown": None, None: None}
    local = []
    def require(suffix, value):
        local.append(bool(value))
        check(label + " " + suffix, value, "trial")
    not_run = trial.get("skipped") is True or trial.get("status") == "not_run"
    if not_run:
        require("未运行状态", role == "fixed" and trial.get("status") == "not_run"
                and trial.get("skipped") is True and trial.get("completed") is False)
        require("未运行空结果", all(trial.get(key) is None for key in
                ("failed", "actual_success", "reported_success", "reported_outcome", "business_failed", "report_failed")))
        require("未运行空证据", trial.get("failure_fingerprint") == "" and all(not trial.get(key) for key in
                ("events", "business_records", "reservations", "violations", "business_violations",
                 "reporting_violations", "execution_violations")))
        return {"actual": None, "fingerprint": "", "completed": False, "not_run": True,
                "counterexample": False, "consistent": all(local)}
    actual, fingerprint = recompute(trial, config)
    codes = set(fingerprint.split("|")) - {""}
    execution = codes & EXECUTION_CODES
    outcome = trial.get("reported_outcome")
    completed = outcome in ("success", "failure", "unknown") and not execution
    completed = bool(completed)
    require("实际完成", trial.get("actual_success") is actual)
    require("指纹", trial.get("failure_fingerprint") == fingerprint)
    require("违规集合", [v["code"] for v in trial.get("violations", [])] == sorted(codes)
            or (len(trial.get("violations", [])) == len(codes)
                and {v["code"] for v in trial.get("violations", [])} == codes))
    require("完成状态", trial.get("completed") is completed)
    expected_failed = bool(codes) if completed or role == "repeated" and trial.get("status") != "infra_error" else None
    require("failed 汇总", trial.get("failed") is expected_failed)
    require("运行标记", trial.get("skipped") in (None, False)
            and (trial.get("status") not in ("infra_error", "cancelled") or not completed))
    if trial.get("evaluation_protocol") == "business-report-v2":
        assessment = ("unreported" if outcome is None else "uncertain" if outcome == "unknown" else
                      "consistent" if (outcome == "success") == actual else "inconsistent")
        business = codes - EXECUTION_CODES - {"REPORT_MISMATCH"}
        require("三态值", outcome in outcomes)
        require("报告评价", trial.get("report_assessment") == assessment)
        require("三态兼容值", outcome in outcomes and trial.get("reported_success") is outcomes[outcome])
        require("业务分类", trial.get("business_failed") is bool(business)
                and {v["code"] for v in trial.get("business_violations", [])} == business)
        require("报告分类", trial.get("report_failed") is ("REPORT_MISMATCH" in codes)
                and {v["code"] for v in trial.get("reporting_violations", [])} == codes & {"REPORT_MISMATCH"})
        require("执行分类", {v["code"] for v in trial.get("execution_violations", [])} == execution)
    return {"actual": actual, "fingerprint": fingerprint, "completed": completed, "not_run": False,
            "counterexample": completed and bool(codes), "consistent": all(local)}


def minimization_facts(report, baseline, initial, groups, final, prefix, check):
    """Check all summaries even if an uploaded stable/completed flag is false."""
    mini, original = report["minimization"], report["config"]["faults"]
    target = baseline["fingerprint"] if baseline["counterexample"] else ""
    local = []
    def require(suffix, value):
        local.append(bool(value))
        check(prefix + " " + suffix, value, "minimization")
    def integer(value, minimum=0):
        return type(value) is int and value >= minimum
    require("原始故障计数", type(mini["original_count"]) is int and mini["original_count"] == len(original))
    require("最终故障计数", type(mini["final_count"]) is int and mini["final_count"] == len(mini["faults"]))
    require("目标不变", mini["failure_fingerprint"] == target)
    require("稳定状态类型", type(mini.get("stable")) is bool)
    require("完成状态类型", type(mini.get("completed")) is bool)
    if report.get("mode") == "llm":
        require("模型不声称严格最小", mini["one_minimal"] is False)
    repeated = initial + [fact for group in groups for fact in group] + final
    require("重复执行证据数量", integer(mini["trials"]) and mini["trials"] == len(repeated))
    initial_fps = [r["fingerprint"] for r in initial]
    if initial or "observed_initial_fingerprints" in mini:
        require("初始指纹摘要", mini.get("observed_initial_fingerprints") == initial_fps)
    initial_stable = bool(target) and len(initial) >= 2 and all(r["completed"] and r["fingerprint"] == target for r in initial)
    require("初始执行故障集", all(r.get("evaluated_faults") == original for r in mini.get("initial_runs", [])))
    remaining, last_accepted = original[:], -1
    all_groups_completed = True
    steps = mini.get("steps", [])
    for index, (step, facts) in enumerate(zip(steps, groups)):
        tag = f"候选{index + 1} "
        repeats = step.get("repeats")
        valid_repeats = integer(repeats, 2)
        require(tag + "至少两次重复", valid_repeats)
        if initial_stable:
            require(tag + "重复次数与初始组一致", repeats == len(initial))
        candidate = [fault for fault in remaining if fault != step["removed_fault"]]
        require(tag + "故障集", step["removed_fault"] in remaining and candidate == step["candidate"])
        require(tag + "执行条件", all(r.get("evaluated_faults") == candidate for r in step.get("trial_evidence", [])))
        counts = {"same_error": 0, "other_error": 0, "no_error": 0, "infra_error": 0}
        for fact in facts:
            kind = ("infra_error" if not fact["completed"] else "same_error" if target and fact["fingerprint"] == target else
                    "other_error" if fact["fingerprint"] else "no_error")
            counts[kind] += 1
        require(tag + "尝试次数", integer(step.get("attempted")) and step["attempted"] == len(facts) and bool(facts))
        require(tag + "指纹摘要", step.get("fingerprints") == [r["fingerprint"] for r in facts])
        for kind, count in counts.items():
            require(tag + kind + "计数", type(step.get(kind + "_count")) is int and step[kind + "_count"] == count)
        summary_counts = step.get("outcome_counts")
        require(tag + "分类汇总", isinstance(summary_counts, dict) and summary_counts == counts
                and all(type(value) is int for value in summary_counts.values()))
        require(tag + "复现摘要", type(step.get("reproduced")) is int and step["reproduced"] == counts["same_error"]
                and step.get("failed") is (counts["same_error"] > 0)
                and step.get("same_error_reproduced") is (counts["same_error"] > 0))
        stopped = step.get("stopped_reason")
        complete = valid_repeats and len(facts) == repeats and all(r["completed"] for r in facts) and stopped is None
        require(tag + "完成状态", type(step.get("completed")) is bool and step["completed"] is complete)
        require(tag + "停止原因", complete or isinstance(stopped, str) and bool(stopped))
        require(tag + "未超重复上限", valid_repeats and len(facts) <= repeats)
        eligible = complete and counts["same_error"] == repeats and bool(target)
        require(tag + "删除决定", step.get("accepted") is eligible)
        if eligible:
            remaining, last_accepted = candidate, index
        require(tag + "保留集", step.get("remaining") == remaining)
        if not complete:
            require(tag + "停止后无后续执行", index == len(steps) - 1 and not final)
        all_groups_completed = all_groups_completed and complete
    require("最终故障集", mini["faults"] == remaining and report.get("minimized_faults") == remaining)
    require("末轮执行故障集", all(r.get("evaluated_faults") == remaining for r in mini.get("final_runs", [])))
    if steps or final:
        require("删减前初始同错", initial_stable)
    fr = mini.get("final_revalidation")
    final_complete, final_stable, final_drift = False, False, False
    if fr is None:
        require("末轮摘要存在", not final)
    else:
        require("末轮摘要对象", isinstance(fr, dict))
        if not isinstance(fr, dict):
            fr = {}
        repeats = fr.get("repeats")
        valid_repeats = integer(repeats, 2)
        require("末轮重复次数", valid_repeats)
        if initial_stable:
            require("末轮次数与初始组一致", repeats == len(initial))
        same = sum(r["completed"] and bool(target) and r["fingerprint"] == target for r in final)
        require("末轮尝试次数", integer(fr.get("attempted")) and fr["attempted"] == len(final))
        require("末轮同错计数", integer(fr.get("same_error_count")) and fr["same_error_count"] == same)
        if final or "fingerprints" in fr:
            require("末轮指纹摘要", fr.get("fingerprints") == [r["fingerprint"] for r in final])
        require("末轮未超重复上限", valid_repeats and len(final) <= repeats)
        stopped = fr.get("stopped_reason")
        final_complete = valid_repeats and len(final) == repeats and all(r["completed"] for r in final) and stopped is None
        final_stable = final_complete and bool(target) and same == repeats
        final_drift = final_complete and not final_stable
        expected_status = "partial" if stopped is not None else "not_run" if not final else "completed" if final_stable else "unstable" if final_drift else "partial"
        require("末轮完成状态", type(fr.get("completed")) is bool and fr["completed"] is final_complete)
        require("末轮状态", fr.get("status") == expected_status)
        require("末轮停止原因", expected_status != "partial" or isinstance(stopped, str) and bool(stopped))
        if expected_status == "not_run":
            require("未运行末轮空证据", not final and fr.get("attempted") == fr.get("same_error_count") == 0)
    local_stop = mini.get("stopped_reason")
    if baseline["counterexample"]:
        if mini.get("stable") is True:
            require("稳定声明有初始证据", initial_stable and not final_drift)
        if steps or final:
            require("稳定声明与已执行组一致", mini.get("stable") is (not final_drift))
        if local_stop == "unstable_initial_failure":
            require("初始不稳定原因", not initial_stable)
        if final_drift:
            require("末轮漂移原因", local_stop == "unstable_final_failure" and mini.get("completed") is False)
        if fr is not None and fr.get("status") == "partial":
            require("末轮停止原因一致", local_stop == fr.get("stopped_reason"))
        if steps and not all_groups_completed:
            require("候选停止原因一致", local_stop == steps[-1].get("stopped_reason"))
        # A completed final group already closed minimization. Later cancellation
        # can mark the whole report partial, but cannot undo this completed phase.
        derived_complete = initial_stable and all_groups_completed and final_stable
        require("精简完成汇总", mini.get("completed") is derived_complete)
        require("未完成精简有原因", derived_complete or isinstance(local_stop, str) and bool(local_stop))
        if derived_complete:
            require("完成精简稳定声明", mini.get("stable") is True)
            require("完成精简无停止原因", local_stop is None)
    else:
        derived_complete = baseline["completed"]
        require("无反例不运行删减", not repeated and not steps and not target and mini.get("stable") is False)
        require("无反例完成分支", mini.get("completed") is derived_complete)
    expected_boundary = list(range(last_accepted + 1, len(steps)))
    if "boundary_step_indexes" in mini or steps:
        require("单项边界索引", mini.get("boundary_step_indexes") == expected_boundary
                and all(type(index) is int for index in mini["boundary_step_indexes"]))
    if baseline["counterexample"] and derived_complete:
        boundary = [steps[i] for i in expected_boundary]
        require("单项删除边界", len(boundary) == len(remaining) and set(s["removed_fault"] for s in boundary) == set(remaining)
                and all(s.get("accepted") is False for s in boundary))
    if mini.get("status") == "not_run":
        require("未运行精简状态", not repeated and not steps and mini.get("completed") is False and mini.get("stable") is False)
    return {"completed": derived_complete, "consistent": all(local),
            "initial_trials": len(initial), "candidate_groups": len(groups), "final_trials": len(final),
            "same_error_reduction": baseline["counterexample"] and derived_complete}


def verify(path):
    package = Package(path)
    checks, reports = [], []
    def check(label, value, category="archive"):
        checks.append({"check": label, "passed": bool(value), "category": category})
    try:
        manifest = package.json("file-manifest.json")
        if not isinstance(manifest, dict) or len(manifest) > 2000:
            raise ValueError("文件清单格式不受支持")
        for name, expected in manifest.items():
            check("文件哈希: " + name, isinstance(expected, str) and sha(package.read(name)) == expected)
        check("文件清单非空", bool(manifest))
        report_names = sorted(name for name in manifest if name.endswith("-report.json") and "/" not in name)
        all_usage = []
        for name in report_names:
            report = package.json(name)
            config, mini = report["config"], report["minimization"]
            prefix = name[:-len("-report.json")]
            initial, final = mini.get("initial_runs", []), mini.get("final_runs", [])
            steps = mini.get("steps", [])
            report_start = len(checks)
            baseline = trial_facts(report["baseline"], config, prefix + " 原始组", "baseline", check)
            initial_facts = [trial_facts(trial, config, f"{prefix} 初始{index + 1}", "repeated", check)
                             for index, trial in enumerate(initial)]
            group_facts = [[trial_facts(trial, config, f"{prefix} 候选{index + 1}执行{number + 1}", "repeated", check)
                            for number, trial in enumerate(step.get("trial_evidence", []))]
                           for index, step in enumerate(steps)]
            final_facts = [trial_facts(trial, config, f"{prefix} 末轮{index + 1}", "repeated", check)
                           for index, trial in enumerate(final)]
            fixed = trial_facts(report["fixed"], config, prefix + " 组合组", "fixed", check)
            derived_mini = minimization_facts(report, baseline, initial_facts, group_facts, final_facts, prefix, check)
            stopped = report.get("stopped_reason")
            finished = report.get("status") == "finished"
            check(prefix + " 整体状态", report.get("status") in ("finished", "partial")
                  and report.get("completed") is finished, "trial")
            check(prefix + " 整体停止原因", stopped is None if finished else isinstance(stopped, str) and bool(stopped), "trial")
            check(prefix + " 完整报告阶段完成", not finished or
                  baseline["completed"] and derived_mini["completed"] and fixed["completed"], "trial")
            repeated = initial + [trial for step in steps for trial in step.get("trial_evidence", [])] + final
            if repeated:
                check(prefix + " 重复执行证据完整", all(r.get("events") and "business_records" in r
                      and "sqlite_dump" in r and "model_usage" in r for r in repeated), "minimization")
            usage = report["model_usage"]
            records = usage["records"]
            all_usage.extend(records)
            check(prefix + " 阶段请求合计", len(records) == usage["requests"] == sum(usage["requests_by_phase"].values()), "ledger")
            provenance = report["provenance"]
            for source, expected in provenance["source_sha256"].items():
                check(prefix + " 运行源码: " + source, sha(package.read("source_snapshot/" + source)) == expected, "provenance")
            check(prefix + " 源码树", sha(canonical(provenance["source_sha256"])) == provenance["source_tree_sha256"], "provenance")
            check(prefix + " 公开配置", sha(canonical(config)) == provenance["public_config_sha256"], "provenance")
            source = package.read("source_snapshot/engine.py").decode("utf-8-sig").replace("\r\n", "\n")
            nodes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
            lines = source.splitlines(keepends=True)
            definition = "".join("".join(lines[nodes[key].lineno - 1:nodes[key].end_lineno]) for key in ("agent_spec", "llm_agent"))
            check(prefix + " 提示定义", sha(definition.encode()) == provenance["prompt_definition_sha256"], "provenance")
            for arm in ("baseline", "fixed"):
                db_name = prefix + "-" + arm + ".sqlite"
                if report[arm].get("skipped") or report[arm].get("status") == "not_run":
                    continue
                check(prefix + " 数据库快照声明: " + arm, db_name in manifest, "database")
                if db_name not in manifest:
                    continue
                with readonly_database(package.read(db_name)) as db:
                    table = "work_orders" if config["scenario"] == "ticket" else "reservations"
                    rows = [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY id")]
                    check(prefix + " 数据库: " + arm, rows == report[arm]["business_records"], "database")
            reports.append({"file": name, "status": report["status"], "requests": usage["requests"],
                            "minimization_completed": derived_mini["completed"],
                            "observed_counterexample": baseline["counterexample"],
                            "same_error_reduction": derived_mini["same_error_reduction"],
                            "recomputed_baseline_fingerprint": baseline["fingerprint"],
                            "checked_trial_counts": {key: derived_mini[key] for key in
                                ("initial_trials", "candidate_groups", "final_trials")},
                            "summary_consistent": all(c["passed"] for c in checks[report_start:]),
                            "final_faults": mini["faults"]})
        check("至少包含一份受支持的实验报告", bool(reports))
        summary = package.json("campaign-summary.json")
        initial = package.json("campaign-initial.json")
        policy = package.json("campaign-policy.json")
        with readonly_database(package.read("campaign-ledger-final.sqlite")) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY sequence")]
            metadata = dict((row[0], row[1]) for row in db.execute("SELECT key,value FROM metadata"))
        check("台账连续", [r["sequence"] for r in rows] == list(range(1, len(rows) + 1)), "ledger")
        check("台账累计请求", len(rows) == summary["requests_made"], "ledger")
        check("授权请求上限", len(rows) <= summary["max_requests"] <= 360, "ledger")
        check("台账政策哈希", sha(metadata["policy_json"].encode()) == metadata["policy_sha256"] == summary["policy_sha256"], "ledger")
        public_policy = json.loads(metadata["policy_json"])
        for key in ("allowed_model", "allowed_origin", "max_requests"):
            check("政策文件一致: " + key, policy.get(key) == public_policy.get(key) == summary.get(key), "ledger")
        for key in ("max_cost_cny", "input_price_per_million", "output_price_per_million"):
            check("政策金额一致: " + key, Decimal(str(policy[key])) == Decimal(str(public_policy[key])) == Decimal(str(summary[key])), "ledger")
        check("政策请求上限", type(policy["max_requests"]) is int and len(rows) <= policy["max_requests"] <= 360, "ledger")
        check("授权费用上限", Decimal(summary["accounted_cost_cny"]) <= Decimal(summary["max_cost_cny"]) <= 10, "ledger")
        charged = sum(int(row["charged_micros"]) for row in rows)
        check("台账累计记账", Decimal(charged) / 1_000_000 == Decimal(summary["accounted_cost_cny"]), "ledger")
        added = rows[initial["requests_made"]:]
        check("新增请求与报告数量", len(added) == len(all_usage), "ledger")
        for index, (entry, record) in enumerate(zip(added, all_usage), 1):
            known = record.get("usage_available") is True
            if known:
                check(f"新增台账{index} 用量", (entry["actual_prompt_tokens"], entry["actual_completion_tokens"])
                      == (record["prompt_tokens"], record["completion_tokens"]), "ledger")
                price = Decimal(policy["input_price_per_million"]) * record["prompt_tokens"] + Decimal(policy["output_price_per_million"]) * record["completion_tokens"]
                check(f"新增台账{index} 记账", Decimal(entry["charged_micros"]) == price.to_integral_value(rounding="ROUND_CEILING"), "ledger")
            else:
                check(f"新增台账{index} 未知用量留预留", entry["state"] in ("unknown", "reserved"), "ledger")
    except Exception as exc:
        checks.append({"check": "归档结构可核验", "passed": False, "category": "archive", "error_type": type(exc).__name__})
    finally:
        package.close()
    passed = bool(checks) and all(c["passed"] for c in checks)
    for report in reports:
        report["conclusion_status"] = "verified_within_scope" if passed else "unverified"
        if not passed:
            # A failed package must not publish a positive, verified conclusion
            # taken from a contradictory or only partly inspected source report.
            for key in ("observed_counterexample", "minimization_completed", "same_error_reduction"):
                report[key] = None
    coverage = []
    for key, description in COVERAGE.items():
        selected = [c for c in checks if c.get("category") == key]
        coverage.append({"id": key, "description": description, "checks": len(selected),
                         "passed": sum(c["passed"] for c in selected),
                         "status": "not_checked" if not selected else "passed" if all(c["passed"] for c in selected) else "failed"})
    return {"verifier_version": VERIFIER_VERSION, "passed": passed, "checks": checks, "reports": reports,
            "coverage": coverage, "not_covered": NOT_COVERED,
            "scope": "仅核对本次明确列出的内部一致性项目；通过不等于材料外部认证，也不保证所有表述正确。未通过时不作已验证的反例或精简成功结论。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    source, output_root = Path(args.archive).resolve(), Path(args.output_root).resolve()
    if source.is_dir() and output_root.is_relative_to(source):
        parser.error("核验输出目录须位于被核验归档之外")
    result = verify(source)
    output = output_root / datetime.now().strftime("verification-%Y%m%d-%H%M%S-%f")
    output.mkdir(parents=True, exist_ok=False)
    (output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [c["check"] for c in result["checks"] if not c["passed"]]
    text = "# 追因离线核验\n\n" + ("所有检查通过。" if result["passed"] else "发现不一致或不支持的证据，请查看明细。")
    text += f"\n\n核验器版本：{result['verifier_version']}。共 {len(result['checks'])} 项检查，{len(failed)} 项未通过。\n\n" + result["scope"]
    text += "\n\n## 本次检查覆盖\n\n| 项目 | 已通过 / 已执行 | 状态 |\n| --- | --- | --- |\n"
    statuses = {"passed": "通过", "failed": "有不一致", "not_checked": "未执行"}
    for item in result["coverage"]:
        text += f"| {item['description']} | {item['passed']} / {item['checks']} | {statuses[item['status']]} |\n"
    text += "\n## 报告事实结论\n\n"
    for report in result["reports"]:
        if report["conclusion_status"] != "verified_within_scope":
            conclusion = "归档未通过，反例和精简结论未验证。"
        elif not report["observed_counterexample"]:
            conclusion = "原始组没有已完成的违规反例；不构成同错精简成功。"
        elif report["same_error_reduction"]:
            conclusion = "存在已完成的违规反例，且同错删减与独立末轮复查在覆盖范围内一致。"
        else:
            conclusion = "存在已完成的违规反例，但精简没有完成。"
        text += "- " + report["file"] + "：" + conclusion + "\n"
    text += "\n## 未覆盖范围\n\n" + "\n".join("- " + item for item in result["not_covered"])
    if failed:
        text += "\n\n## 未通过项目\n\n" + "\n".join("- " + label for label in failed)
    (output / "核验结果.md").write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]), "output": str(output)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
