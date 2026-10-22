"""Fixed eight-arm preparation and fresh-trial collection; offline by default."""
from __future__ import annotations

import argparse
import copy
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine
from job_store import sanitize
import model_budget
import study

REGISTRATION = ROOT / "data/evaluations/v061-review-20261017/eight-arm-preregistration.json"
POLICY = ROOT / "data/evaluations/v061-review-20261017/campaign-policy.json"
VERSION = "eight-arm-collection-v1"


class ExperimentCheckError(ValueError):
    """Expose a fixed diagnostic code without printing provider exception text."""
    def __init__(self, reason_code, message):
        super().__init__(message)
        self.reason_code = reason_code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(sanitize(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fixed_design(policy_path=POLICY):
    registration = json.loads(REGISTRATION.read_text(encoding="utf-8"))
    config = engine.Config(scenario=registration["scenario"], ticket_title=registration["ticket_title"],
                           ticket_priority=registration["ticket_priority"], faults=registration["faults"][:],
                           request_id="eight-arm-20261018", seed=registration["seed"], agent_mode="llm",
                           prompt_policy="neutral", max_requests=registration["worst_case_requests"])
    options = study.validate_study_options({
        "repetitions": registration["repetitions"], "strategies": registration["strategies"][:],
        "prompt_policies": registration["prompt_policies"][:], "order_mode": registration["order_mode"],
        "max_requests": registration["worst_case_requests"], "input_price_per_million": 3,
        "output_price_per_million": 12, "currency": "CNY"})
    plan = study.execution_plan(options, config.seed)
    factors = study._factor_design(config, options)
    return {"collector_version": VERSION, "registration_file": REGISTRATION.relative_to(ROOT).as_posix(),
            "registration_sha256": digest(REGISTRATION), "config": config.public(), "options": options,
            "model_identity": {"api_origin": "https://api.deepseek.com", "model": registration["model"], "thinking": "disabled"},
            "factor_design": factors, "execution_plan": plan,
            "planned_trials": sum(len(row["trials"]) for row in plan),
            "max_requests_per_trial": engine.MAX_TRIAL_REQUESTS,
            "worst_case_requests": registration["worst_case_requests"],
            "primary_metric": registration["primary_metric"], "secondary_metrics": registration["secondary_metrics"],
            "missing_data": "保留已尝试的执行错误与取消，分层列出有效、已尝试和未运行数；不补采、不挑选、不混入旧样本。",
            "early_stop": "取消、预算、费用或采集写入故障立即保留已有证据；同一输出目录不可续跑或覆盖。网络执行错误保留为样本，依现有study协议继续后续组。",
            "interpretation": "每组两轮是探索性观察；Wilson区间描述性展示，不据此证明总体优越、交互、因果或统计独立。两轮不构成完整四位置平衡周期。",
            "runtime_sha256": engine.report_provenance(config)["source_sha256"],
            "collector_sha256": digest(__file__),
            "expected_campaign_policy_sha256": readonly_budget(policy_path)["policy_sha256"]}


def readonly_budget(policy_path=POLICY):
    """Read existing guard records without initializing or updating its ledger."""
    with campaign_environment(policy_path):
        policy = model_budget._load_policy()
    if policy is None:
        raise ValueError("缺少累计预算政策")
    raw = json.loads(policy.public_json)
    ledger = Path(policy.ledger_path)
    if not ledger.is_file():
        raise ValueError("预算台账不存在")
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        count, amount = db.execute("SELECT COUNT(*), COALESCE(SUM(charged_micros),0) FROM requests").fetchone()
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        db.rollback()
    # The engine still enforces and validates the policy at every real request.
    policy_valid = (policy.public_json == metadata["policy_json"] and
                    policy.fingerprint == metadata["policy_sha256"])
    return {"policy_consistent": policy_valid, "policy_sha256": metadata["policy_sha256"],
            "requests_made": count, "max_requests": raw["max_requests"],
            "requests_remaining": raw["max_requests"] - count,
            "accounted_cost_cny": format(Decimal(amount) / 1_000_000, ".6f"),
            "max_cost_cny": str(raw["max_cost_cny"]),
            "remaining_cost_cny": format(Decimal(str(raw["max_cost_cny"])) - Decimal(amount) / 1_000_000, ".6f"),
            "halted_reason": metadata.get("halted_reason") or None,
            "ledger_sha256": digest(ledger)}


def readiness(design, budget):
    required = design["worst_case_requests"]
    reasons = []
    if not budget["policy_consistent"]:
        reasons.append("policy_inconsistent")
    if budget["requests_remaining"] < required:
        reasons.append("full_design_request_capacity_insufficient")
    if budget["halted_reason"]:
        reasons.append("campaign_halted")
    if Decimal(budget["remaining_cost_cny"]) <= 0:
        reasons.append("cost_capacity_exhausted")
    return {"status": "blocked" if reasons else "capacity_check_passed", "reasons": reasons,
            "required_requests": required, "available_requests": budget["requests_remaining"],
            "request_gap": max(0, required - budget["requests_remaining"]),
            "proposed_cumulative_request_limit": budget["requests_made"] + required,
            "proposal_active": False,
            "note": "仅按本地台账检查容量，不扩大授权，不保证网络、服务、费用预留或所有轮次有效。"}


def prepare(output, policy_path=POLICY):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    design = fixed_design(policy_path)
    budget = readonly_budget(policy_path)
    write_json(output / "fixed-design.json", design)
    write_json(output / "prepared-at.json", {"prepared_at_utc": datetime.now(timezone.utc).isoformat(),
               "state": "prepared_not_executed", "new_model_requests": 0})
    write_json(output / "design-sha256.json", {"sha256": hashlib.sha256(canonical(design)).hexdigest()})
    write_json(output / "budget-readiness.json", {"budget": budget, "readiness": readiness(design, budget)})
    sources = output / "source_snapshot"
    sources.mkdir()
    for name in design["runtime_sha256"]:
        (sources / name).write_bytes((ROOT / name).read_bytes())
    (sources / "eight_arm_experiment.py").write_bytes(Path(__file__).read_bytes())
    (output / "NOT_A_MODEL_RESULT.md").write_text(
        "# 八组研究准备材料\n\n这是执行前固定的设计、来源快照与只读预算检查，不是真实模型结果。"
        "budget-readiness.json 中的额度建议未生效；既有请求和费用上限未改变。\n", encoding="utf-8")
    return design


class TrialArchive:
    """Accept only fresh results observed inside this invocation, never imports."""
    def __init__(self, output, evidence_mode):
        self.output = Path(output)
        self.evidence_mode = evidence_mode
        self.entries = []
        self.usage_seen = 0
        (self.output / "trials").mkdir()

    def observe(self, sample, result):
        index = sample["execution_index"]
        if type(index) is not int or index != len(self.entries) + 1:
            raise ValueError("逐轮采集序号不连续")
        name = f"trial-{index:02d}"
        records = getattr(engine.MODEL_CONTEXT, "usage_records", [])
        usage = copy.deepcopy(records[self.usage_seen:])
        if len(usage) != sample["model_usage"]["requests"]:
            raise ValueError("逐轮原始请求记录与样本请求数不一致")
        self.usage_seen = len(records)
        record = {"evidence_mode": self.evidence_mode, "sample": sample, "result": result,
                  "request_records": usage,
                  "trace_scope": "工具业务事件和服务商返回的逐请求用量；不包含完整原始模型对话。"}
        # Durable JSON is first: if database reconstruction fails, keep the
        # attempted trial rather than lose its events and accounting record.
        write_json(self.output / "trials" / (name + ".json"), record)
        dump = result.get("sqlite_dump")
        database_status = "not_available"
        if isinstance(dump, str) and dump:
            database_path = self.output / "trials" / (name + ".sqlite")
            with closing(sqlite3.connect(database_path)) as db:
                # This method is called only by run_study with fresh Lab output.
                # No CLI option accepts uploaded SQL or recorded trial content.
                db.executescript(dump)
                table = "work_orders" if result.get("scenario") == "ticket" else "reservations"
                db.row_factory = sqlite3.Row
                rows = [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY id")]
                if rows != result.get("business_records", result.get("reservations", [])):
                    raise ValueError("逐轮数据库与业务记录不一致")
                db.commit()
            database_status = "matched_fresh_trial_records"
        self.entries.append({"execution_index": index, "arm_id": sample["arm_id"], "trial_index": sample["trial_index"],
                             "status": sample["status"], "json": "trials/" + name + ".json",
                             "database": "trials/" + name + ".sqlite" if database_status != "not_available" else None,
                             "database_status": database_status})
        write_json(self.output / "trial-index.json", {"evidence_mode": self.evidence_mode, "trials": self.entries})


@contextmanager
def campaign_environment(policy_path):
    previous = os.environ.get("ZHUIYIN_CAMPAIGN_POLICY")
    try:
        if policy_path is None:
            os.environ.pop("ZHUIYIN_CAMPAIGN_POLICY", None)
        else:
            os.environ["ZHUIYIN_CAMPAIGN_POLICY"] = str(Path(policy_path).resolve())
        yield
    finally:
        if previous is None:
            os.environ.pop("ZHUIYIN_CAMPAIGN_POLICY", None)
        else:
            os.environ["ZHUIYIN_CAMPAIGN_POLICY"] = previous


def execute(prepared, output, mode="rules", *, policy_path=POLICY, execute_real_model=False):
    prepared = Path(prepared).resolve()
    design = json.loads((prepared / "fixed-design.json").read_text(encoding="utf-8"))
    lock = json.loads((prepared / "design-sha256.json").read_text(encoding="utf-8"))["sha256"]
    if hashlib.sha256(canonical(design)).hexdigest() != lock or design != fixed_design(policy_path):
        raise ExperimentCheckError("prepared_design_changed",
                                   "设计或源码已变化，或预算政策与准备时不同，请重新准备并复核，不能沿用旧的固定方案")
    if mode not in ("rules", "llm"):
        raise ExperimentCheckError("invalid_execution_mode", "仅支持规则演练或真实模型")
    if mode == "llm":
        budget = readonly_budget(policy_path)
        if budget["policy_sha256"] != design["expected_campaign_policy_sha256"]:
            raise ExperimentCheckError("prepared_policy_changed", "预算政策与准备时不同，请重新准备并复核")
        if not execute_real_model:
            raise ExperimentCheckError("explicit_model_switch_required",
                                       "真实模型执行需要显式执行开关；准备和规则演练不会调用模型")
        if readiness(design, budget)["status"] != "capacity_check_passed":
            raise ExperimentCheckError("campaign_capacity_insufficient",
                                       "现有预算不足以容纳完整固定方案，未启动任何模型请求")
        secret = os.environ.get("DEEPSEEK_API_KEY")
        if not secret:
            raise ExperimentCheckError("process_model_credential_missing", "缺少进程内模型密钥；不要写入方案文件")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    archive = TrialArchive(output, "rules_rehearsal_not_model_evidence" if mode == "rules" else "real_model_attempts")
    raw_config = {**design["config"], "agent_mode": mode}
    if mode == "llm":
        raw_config["model_config"] = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": secret}
    config = engine.Config.from_dict(raw_config)
    write_json(output / "fixed-design.json", design)
    write_json(output / "run-state.json", {"mode": mode, "status": "starting", "new_model_requests": 0})
    try:
        with campaign_environment(policy_path if mode == "llm" else None):
            report = study.run_study(config, design["options"], on_trial=archive.observe,
                                     on_checkpoint=lambda value: write_json(output / "study-report.json", value))
        write_json(output / "run-state.json", {"mode": mode, "status": report["status"],
                  "new_model_requests": report["model_usage"]["requests"], "trials_indexed": len(archive.entries),
                  "trial_json_files_retained": len(list((output / "trials").glob("trial-*.json"))),
                  "trial_databases_retained": len(list((output / "trials").glob("trial-*.sqlite")))})
        manifest = {path.relative_to(output).as_posix(): digest(path)
                    for path in sorted(output.rglob("*")) if path.is_file()}
        write_json(output / "file-manifest.json", manifest)
        return report
    except BaseException:
        write_json(output / "run-state.json", {"mode": mode, "status": "interrupted",
                  "reason": "execution_or_collection_interrupted", "trials_indexed": len(archive.entries),
                  "trial_json_files_retained": len(list((output / "trials").glob("trial-*.json"))),
                  "trial_databases_retained": len(list((output / "trials").glob("trial-*.sqlite"))),
                  "note": "已发出的请求以累计台账为准；保留已有逐轮文件，不自动重试或续跑。"})
        raise
    finally:
        config.model_config.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "rehearse", "run"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--prepared")
    parser.add_argument("--policy", type=Path, default=POLICY,
                        help="选择累计预算政策；政策改变后必须重新 prepare，默认保留原政策。")
    parser.add_argument("--execute-real-model", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            design = prepare(args.output, policy_path=args.policy)
            print(json.dumps({"prepared": True, "planned_trials": design["planned_trials"],
                              "model_requests": 0, "output": str(Path(args.output).resolve())}, ensure_ascii=False))
        else:
            if not args.prepared:
                raise ExperimentCheckError("prepared_directory_required", "请先指定已固定的准备目录")
            report = execute(args.prepared, args.output, "rules" if args.mode == "rehearse" else "llm",
                             policy_path=args.policy,
                             execute_real_model=args.execute_real_model)
            print(json.dumps({"mode": report["mode"], "status": report["status"], "attempted": report["completed"],
                              "model_requests": report["model_usage"]["requests"]}, ensure_ascii=False))
    except Exception as error:
        # Provider or filesystem exception text must never leak credentials.
        reason = (error.reason_code if isinstance(error, ExperimentCheckError)
                  else "output_already_exists" if isinstance(error, FileExistsError)
                  else "preparation_or_execution_failed")
        print(json.dumps({"status": "not_completed", "reason_code": reason,
                          "message": "准备、预算或采集检查未通过。请保留已有文件；没有自动重跑。"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
