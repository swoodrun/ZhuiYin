"""Read-only, denominator-aware comparison of saved experiments.

Only saved primary executions are counted. Regression, ablation, minimization,
static imports and duplicate input copies never become extra observations.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
from datetime import datetime
import hashlib
import json
import math
from urllib.parse import urlsplit

from credentials import sensitive_fields

VERSION = "history-compare-v1"
CONFIG_FIELDS = ("scenario", "equipment", "stock", "quantity", "request_id", "seed", "faults",
                 "agent_mode", "ticket_title", "ticket_priority", "max_requests", "prompt_policy",
                 "billing", "template_snapshot")
METRICS = {"actual_success": "数据库实际完成", "business_failed": "业务违规",
           "report_failed": "报告不一致", "uncertain": "报告 unknown", "contract_failed": "模板规则不符"}
STRATEGY_NAMES = {"baseline": "原始", "idempotency_only": "仅幂等", "verification_only": "仅核验", "verified": "幂等＋核验"}
POLICY_NAMES = {"neutral": "基础任务指令", "recovery": "任务指令＋恢复建议"}
SKIP_STATUSES = {"duplicate", "conflict", "import_error"}
UNRUN_STATUSES = {"not_run", "ready", "pending", "skipped"}
ERROR_STATUSES = {"infra_error", "execution_error", "error", "cancelled", "interrupted"}


def _object(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _clean(value, context=None):
    """Remove credential-shaped fields without evaluating any saved content."""
    if isinstance(value, dict):
        private = sensitive_fields(value, context)
        return {key: _clean(item, key) for key, item in value.items()
                if key not in private and str(key).lower() not in {"model_config", "base_url", "connection"}}
    if isinstance(value, list):
        return [_clean(item, context) for item in value]
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _identifier(value):
    return value[:160] if isinstance(value, str) and value else "unknown"


def _report(job):
    if not isinstance(job, dict):
        raise ValueError("历史记录必须是对象")
    if isinstance(job.get("result"), dict):
        return job["result"]
    if isinstance(job.get("partial_result"), dict):
        return job["partial_result"]
    return job if any(key in job for key in ("baseline", "samples", "cases", "reported_outcome", "actual_success", "analyses")) else {}


def _public_config(value):
    return _clean({key: copy.deepcopy(value[key]) for key in CONFIG_FIELDS if key in value})


def reusable_config(job):
    """Return saved public inputs only; never infer defaults or a model connection."""
    report = _report(job)
    config = _public_config(_object(report.get("config")) or _object(job.get("config")))
    snapshot = report.get("template_snapshot")
    if isinstance(snapshot, dict) and "template_snapshot" not in config:
        config["template_snapshot"] = _clean(copy.deepcopy(snapshot))
    return config


def _mode(value, config, parent):
    mode = value.get("agent_mode") or config.get("agent_mode") or parent.get("mode") or parent.get("agent_mode")
    if mode not in ("rules", "llm"):
        mode = "llm" if parent.get("profile") == "agent" else "rules" if parent.get("profile") in ("quick", "full") else "unknown"
    return mode


def _outcome(value):
    outcome = value.get("reported_outcome")
    if outcome in ("success", "failure", "unknown"):
        return outcome
    legacy = value.get("reported_success")
    return ("success" if legacy else "failure") if isinstance(legacy, bool) else None


def _state(value, inherited_status=None):
    status = value.get("status")
    if status is None and "completed" not in value and _outcome(value) is None:
        status = inherited_status
    status = status if isinstance(status, str) else None
    if value.get("skipped") is True or status in UNRUN_STATUSES:
        return "not_run"
    codes = {item.get("code") for item in _items(value.get("execution_violations")) if isinstance(item, dict)}
    codes.update(item.get("code") for item in _items(value.get("violations")) if isinstance(item, dict)
                 and item.get("code") in {"MODEL_EXECUTION_ERROR", "AGENT_DID_NOT_FINISH", "SUITE_EXECUTION_ERROR"})
    if status in ERROR_STATUSES or codes or value.get("infra_error") or value.get("completed") is False:
        return "execution_error"
    if status == "valid" or value.get("completed") is True:
        return "valid"
    # Legacy reports retain their explicit booleans, but absent observations stay unknown.
    if _outcome(value) is not None and isinstance(value.get("actual_success"), bool):
        return "valid"
    return "unknown"


def _flag(value, name, violation_key):
    if isinstance(value.get(name), bool):
        return value[name]
    if isinstance(value.get(violation_key), list):
        return bool(value[violation_key])
    if name == "report_failed" and value.get("report_assessment") in ("consistent", "inconsistent"):
        return value["report_assessment"] == "inconsistent"
    return None


def _business_key(config):
    scenario = config.get("scenario")
    fields = ("scenario", "request_id", "equipment", "stock", "quantity") if scenario == "equipment" else (
        "scenario", "request_id", "ticket_title", "ticket_priority") if scenario == "ticket" else (
        "scenario", "request_id", "equipment", "stock", "quantity", "ticket_title", "ticket_priority")
    value = {key: config.get(key) for key in fields}
    value["faults"] = sorted(config["faults"]) if isinstance(config.get("faults"), list) and all(isinstance(x, str) for x in config["faults"]) else None
    return _canonical(value)


def _task_identity_known(config):
    """A shared set of absent fields cannot identify a shared business task."""
    scenario = config.get("scenario")
    if scenario not in ("equipment", "ticket"):
        return False
    request = config.get("request_id")
    faults = config.get("faults")
    if not isinstance(request, str) or not request.strip() or not isinstance(faults, list) or not all(isinstance(item, str) for item in faults):
        return False
    if scenario == "equipment":
        return (isinstance(config.get("equipment"), str) and bool(config["equipment"].strip())
                and type(config.get("stock")) is int and config["stock"] >= 0
                and type(config.get("quantity")) is int and config["quantity"] > 0)
    return (isinstance(config.get("ticket_title"), str) and bool(config["ticket_title"].strip())
            and config.get("ticket_priority") in ("low", "normal", "high", "urgent"))


def _collect(job):
    report = _report(job)
    base = reusable_config(job)
    rows, excluded = [], Counter()

    def add(value, config, path, label, strategy=None, repetition=None, parent=None, inherited_status=None):
        if not isinstance(value, dict):
            return
        parent = parent or report
        config = {**base, **_public_config(config)}
        if isinstance(value.get("config"), dict):
            config.update(_public_config(value["config"]))
        policy = _identifier(value.get("prompt_policy") or config.get("prompt_policy") or parent.get("prompt_policy"))
        strategy = _identifier(value.get("strategy") or strategy)
        strategy = "verified" if strategy == "fixed" else strategy
        mode = _mode(value, config, parent)
        protocol = _identifier(value.get("evaluation_protocol") or parent.get("evaluation_protocol") or _object(parent.get("provenance")).get("evaluation_protocol"))
        outcome = _outcome(value)
        state = _state(value, inherited_status)
        contract_status = _object(value.get("template_contract")).get("status")
        flags = {"actual_success": value.get("actual_success") if isinstance(value.get("actual_success"), bool) else None,
                 "business_failed": _flag(value, "business_failed", "business_violations"),
                 "report_failed": _flag(value, "report_failed", "reporting_violations"),
                 "uncertain": outcome == "unknown" if outcome is not None else None,
                 "contract_failed": contract_status == "fail" if contract_status in ("pass", "fail") else None}
        if state != "valid":
            flags = {key: None for key in flags}
        config.update(prompt_policy=policy)
        if mode in ("rules", "llm"):
            config["agent_mode"] = mode
        rows.append({"source": path, "label": str(label or path)[:240], "config": config,
                     "task_key": _business_key(config), "strategy": strategy, "prompt_policy": policy,
                     "task_identity_known": _task_identity_known(config),
                     "mode": mode, "evaluation_protocol": protocol,
                     "repetition": repetition if isinstance(repetition, int) and not isinstance(repetition, bool) and repetition >= 0 else None,
                     "schedule": value.get("schedule") if isinstance(value.get("schedule"), str) else None,
                     "state": state, "outcome": outcome,
                     "contract_status": contract_status if contract_status in ("pass", "fail", "unknown") else None,
                     "failure_fingerprint": value.get("failure_fingerprint") if isinstance(value.get("failure_fingerprint"), str) else None,
                     "metrics": flags, "failed": value.get("failed") if isinstance(value.get("failed"), bool) else None})

    def walk(value, config, path, label, depth=0):
        if not isinstance(value, dict) or depth > 5:
            return
        if value.get("kind") == "trace" or value.get("execution_scope") == "static_import_only" or "import_verification" in value:
            excluded["static_trace"] += 1
            return
        local = {**config, **_public_config(_object(value.get("config")))}
        if isinstance(value.get("samples"), list):
            for index, sample in enumerate(value["samples"]):
                sample = _object(sample)
                add(sample, local, f"{path}.samples[{index}]", f"第 {sample.get('trial_index', index + 1)} 轮", repetition=sample.get("trial_index", index + 1), parent=value)
            return
        if isinstance(value.get("cases"), list) or isinstance(value.get("items"), list):
            key = "cases" if isinstance(value.get("cases"), list) else "items"
            for index, case in enumerate(value[key]):
                case = _object(case)
                source = f"{path}.{key}[{index}]"
                status = case.get("status") if isinstance(case.get("status"), str) else None
                if status in SKIP_STATUSES or case.get("kind") in ("trace", "import_error") or case.get("execution_scope") == "static_import_only":
                    excluded[status or "static_trace"] += 1
                    continue
                cfg = {**local, **_public_config(_object(case.get("config")))}
                if isinstance(case.get("template_snapshot"), dict):
                    cfg["template_snapshot"] = _clean(copy.deepcopy(case["template_snapshot"]))
                name = case.get("name") or case.get("case_id") or f"用例 {index + 1}"
                if isinstance(case.get("strategies"), dict):
                    for strategy, trial in case["strategies"].items():
                        add(trial, cfg, source + ".strategies." + strategy, name, strategy=strategy, parent=value, inherited_status=status)
                elif isinstance(case.get("result"), dict):
                    result = case["result"]
                    if any(key in result for key in ("baseline", "fixed", "samples", "cases")):
                        walk(result, cfg, source + ".result", name, depth + 1)
                    else:
                        add(result, cfg, source + ".result", name, strategy=case.get("strategy"), parent=value, inherited_status=status)
                elif any(key in case for key in ("actual_success", "reported_outcome", "failed")):
                    add(case, cfg, source, name, parent=value, inherited_status=status)
                elif status in UNRUN_STATUSES or status in ERROR_STATUSES:
                    add(case, cfg, source, name, parent=value, inherited_status=status)
                else:
                    excluded["no_primary_execution"] += 1
            return
        found = False
        for key in ("baseline", "fixed"):
            if isinstance(value.get(key), dict):
                add(value[key], local, path + "." + key, label, strategy=key, parent=value)
                found = True
        if found:
            return
        if any(key in value for key in ("actual_success", "reported_outcome", "failed")):
            add(value, local, path, label, parent=value)
        elif any(key in value for key in ("analyses", "scenarios")):
            excluded["static_import"] += len(_items(value.get("analyses"))) + len(_items(value.get("scenarios")))

    walk(report, base, "result", job.get("label") or "保存的实验")
    if len(rows) > 5000:
        raise ValueError("历史比较最多处理每侧 5000 条主执行记录")
    return rows, dict(excluded)


def _metric(rows, key):
    values = [row["metrics"][key] for row in rows if isinstance(row["metrics"][key], bool)]
    count = sum(values)
    return {"count": count if values else None, "known": len(values), "missing": len(rows) - len(values),
            "rate": count / len(values) if values else None}


def _group_key(row):
    return (row["mode"], row["strategy"], row["prompt_policy"], row["evaluation_protocol"])


def _summaries(rows, report):
    grouped = defaultdict(list)
    for row in rows:
        grouped[_group_key(row)].append(row)
    declarations = {}
    for arm in _items(report.get("arms")):
        if not isinstance(arm, dict) or _number(arm.get("unrun")) is None:
            continue
        key = (_mode({}, _object(report.get("config")), report), _identifier(arm.get("strategy")),
               _identifier(arm.get("prompt_policy")), _identifier(report.get("evaluation_protocol")))
        declarations[key] = arm["unrun"]
        grouped.setdefault(key, [])
    groups = []
    for key, values in sorted(grouped.items()):
        states = Counter(row["state"] for row in values)
        groups.append({"key": list(key), "mode": key[0], "strategy": key[1], "prompt_policy": key[2],
                       "evaluation_protocol": key[3], "records": len(values),
                       "valid": states["valid"], "not_run": max(states["not_run"], declarations.get(key, 0)),
                       "not_run_source": "saved_arm_summary" if key in declarations else "saved_records_only",
                       "execution_errors": states["execution_error"], "execution_unknown": states["unknown"],
                       "metrics": {name: _metric(values, name) for name in METRICS},
                       "independent_tasks": (len({row["task_key"] for row in values if row["task_identity_known"]})
                                             if any(row["task_identity_known"] for row in values) else None),
                       "task_identity_unknown": sum(not row["task_identity_known"] for row in values),
                       "repeated_observations": sum(row["repetition"] is not None for row in values)})
    return groups


def _identity(report):
    identity = _object(report.get("model_identity"))
    result = {"model": identity.get("model") if isinstance(identity.get("model"), str) else None}
    origin = identity.get("api_origin") or identity.get("origin")
    try:
        parts = urlsplit(origin) if isinstance(origin, str) else None
        result["origin"] = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "") if parts and parts.scheme in ("http", "https") and parts.hostname else None
    except ValueError:
        result["origin"] = None
    return result


def _elapsed(report):
    value = _number(report.get("elapsed_ms"))
    if value is not None:
        return value
    value = _number(report.get("elapsed_seconds"))
    if value is not None:
        return value * 1000
    try:
        start, end = report.get("started_at"), report.get("finished_at")
        if isinstance(start, str) and isinstance(end, str):
            elapsed = (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() * 1000
            return elapsed if elapsed >= 0 else None
    except (ValueError, TypeError):
        pass
    return None


def _usage(report):
    usage = _object(report.get("model_usage"))
    cny = _number(usage.get("estimated_cost_cny"))
    cost = cny if cny is not None else _number(usage.get("estimated_cost"))
    currency = "CNY" if cny is not None else usage.get("currency")
    if usage.get("cost_complete") is False:
        cost = None
    requests, total = _number(usage.get("requests")), _number(usage.get("total_tokens"))
    source = "saved_summary" if total is not None else "unknown"
    records = usage.get("records")
    if "total_tokens" not in usage and isinstance(records, list) and requests == len(records):
        totals = []
        for record in records:
            record = _object(record)
            prompt, completion = _number(record.get("prompt_tokens")), _number(record.get("completion_tokens"))
            if record.get("usage_available") is not True or prompt is None or completion is None:
                break
            totals.append(prompt + completion)
        if len(totals) == len(records):
            total, source = sum(totals), "complete_request_records"
    return {"requests": requests, "total_tokens": total, "total_tokens_source": source,
            "estimated_cost": cost, "currency": currency if isinstance(currency, str) else None,
            "known_cost_cny": _number(usage.get("known_cost_cny")), "elapsed_ms": _elapsed(report)}


def _configuration(job):
    report = _report(job)
    provenance = _object(report.get("provenance"))
    return {"config": reusable_config(job), "model": _identity(report),
            "options": _clean(copy.deepcopy(_object(report.get("options")))),
            "agent_specs": _clean(copy.deepcopy(_object(report.get("agent_specs")))),
            "version": _clean({key: provenance.get(key) for key in (
                "app_version", "prompt_version", "evaluation_protocol", "backend_sha256", "source_tree_sha256",
                "source_sha256", "prompt_definition_sha256", "model_request_settings", "factor_design") if key in provenance}),
            "evaluation_protocol": report.get("evaluation_protocol") or provenance.get("evaluation_protocol")}


def _differences(left, right, prefix=""):
    differences = []
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in left or key not in right:
                differences.append({"field": path, "left": left.get(key), "right": right.get(key),
                                    "left_recorded": key in left, "right_recorded": key in right})
            else:
                differences.extend(_differences(left[key], right[key], path))
    elif _canonical(left) != _canonical(right):
        differences.append({"field": prefix, "left": left, "right": right,
                            "left_recorded": True, "right_recorded": True})
    return differences


def _eligible(config):
    scenario = config.get("scenario")
    required = ("request_id", "equipment", "stock", "quantity", "faults") if scenario == "equipment" else (
        "request_id", "ticket_title", "ticket_priority", "faults") if scenario == "ticket" else ()
    if not required or any(key not in config for key in required):
        return False, "缺少完整业务参数，不能直接复用"
    if config.get("agent_mode") not in ("rules", "llm"):
        return False, "执行模式未记录，不能直接复用"
    if not isinstance(config.get("faults"), list):
        return False, "故障配置未记录或格式无效"
    try:
        from engine import Config
        Config.from_dict({**config, "agent_mode": "rules", "model_config": {}})
    except (ValueError, TypeError):
        return False, "保存的业务参数未通过当前输入校验"
    return True, "仅载入保存参数；启动新测试仍需当前连接和预算"


def retest_candidates(job):
    """Return explicit selections for preview; this function never starts work."""
    rows, _ = _collect(job)
    result = []
    for row in rows:
        eligible, reason = _eligible(row["config"])
        failed = row["state"] == "valid" and (row["failed"] is True or any(
            row["metrics"].get(key) is True for key in ("business_failed", "report_failed", "contract_failed")))
        attention = (failed or row["state"] != "valid" or row["outcome"] == "unknown" or row["contract_status"] == "unknown"
                     or any(row["metrics"][key] is None for key in ("actual_success", "business_failed", "report_failed", "uncertain")))
        result.append({"candidate_id": hashlib.sha256(row["source"].encode()).hexdigest()[:20],
                       "source": row["source"], "source_run_id": job.get("run_id"), "name": row["label"],
                       "config": copy.deepcopy(row["config"]), "strategy": row["strategy"],
                       "prompt_policy": row["prompt_policy"], "mode": row["mode"],
                       "trial_index": row["repetition"], "state": row["state"],
                       "failed": failed, "source_failed": row["failed"], "attention": attention, "eligible": eligible,
                       "reason": reason, "reported_outcome": row["outcome"],
                       "failure_fingerprint": row["failure_fingerprint"]})
    return result


def _pair_key(row, include_policy=True):
    return (row["task_key"], row["strategy"], row["mode"], row["evaluation_protocol"],
            row["schedule"], row["repetition"]) + ((row["prompt_policy"],) if include_policy else ())


def _pair(left, right, changed_conditions):
    improvement, regression = [], []
    if left["state"] == right["state"] == "valid":
        for name in ("actual_success", "business_failed", "report_failed", "contract_failed"):
            a, b = left["metrics"][name], right["metrics"][name]
            if not isinstance(a, bool) or not isinstance(b, bool) or a == b:
                continue
            better = b if name == "actual_success" else not b
            (improvement if better else regression).append(name)
        known_pair = any(isinstance(left["metrics"][name], bool) and isinstance(right["metrics"][name], bool) for name in METRICS)
        status = ("mixed" if improvement and regression else "improved" if improvement else "regressed" if regression
                  else "report_changed" if left["outcome"] != right["outcome"] and left["outcome"] and right["outcome"]
                  else "unchanged" if known_pair else "unverifiable")
    else:
        status = "execution_changed" if left["state"] != right["state"] else "unverifiable"
    return {"name": left["label"], "task": json.loads(left["task_key"]), "strategy": left["strategy"],
            "repetition": left["repetition"], "status": status,
            "improved_metrics": improvement, "regressed_metrics": regression,
            "condition_changed": (changed_conditions or left["prompt_policy"] != right["prompt_policy"]
                                  or _canonical(left["config"]) != _canonical(right["config"])
                                  or any(row[key] == "unknown" for row in (left, right)
                                         for key in ("mode", "prompt_policy", "evaluation_protocol"))),
            "left": {key: left[key] for key in ("source", "state", "outcome", "metrics", "prompt_policy", "failure_fingerprint")},
            "right": {key: right[key] for key in ("source", "state", "outcome", "metrics", "prompt_policy", "failure_fingerprint")}}


def compare_jobs(left_job, right_job):
    """Describe saved differences, not causal effects or new reliability evidence."""
    left_report, right_report = _report(left_job), _report(right_job)
    left_rows, left_excluded = _collect(left_job)
    right_rows, right_excluded = _collect(right_job)
    left_config, right_config = _configuration(left_job), _configuration(right_job)
    differences = _differences(left_config, right_config)
    warnings = ["只比较已保存结果，不执行工具、不发送模型请求；改善与退步是观察描述，不证明因果。",
                "主实验、各策略和提示层分别计数；故障精简、规则回归、静态日志及重复输入不追加为样本。",
                "同任务的重复轮次不是独立业务任务；指标仅对字段已知的有效执行给出分母。"]
    conditions_changed = any(d["field"] not in ("config.request_id",) for d in differences)
    if differences:
        warnings.append("两次记录的配置或来源存在差异，请结合差异表解释结果，不能将变化直接归因于单一修改。")
    if left_job.get("run_id") and left_job.get("run_id") == right_job.get("run_id"):
        warnings.append("两侧是同一份保存记录，不是两次独立实验。")
    left_map, right_map = defaultdict(list), defaultdict(list)
    for row in left_rows:
        if row["task_identity_known"]:
            left_map[_pair_key(row)].append(row)
    for row in right_rows:
        if row["task_identity_known"]:
            right_map[_pair_key(row)].append(row)
    pairs, used_left, used_right = [], set(), set()
    for key in sorted(set(left_map) & set(right_map), key=str):
        a, b = left_map[key], right_map[key]
        if len(a) == len(b) == 1:
            pairs.append(_pair(a[0], b[0], conditions_changed))
            used_left.add(a[0]["source"]); used_right.add(b[0]["source"])
    # A unique task may still be shown across changed prompt policies. It never
    # enters a pooled prompt group or a claim about an isolated intervention.
    remaining_left, remaining_right = defaultdict(list), defaultdict(list)
    for row in left_rows:
        if row["task_identity_known"] and row["source"] not in used_left:
            remaining_left[_pair_key(row, False)].append(row)
    for row in right_rows:
        if row["task_identity_known"] and row["source"] not in used_right:
            remaining_right[_pair_key(row, False)].append(row)
    for key in sorted(set(remaining_left) & set(remaining_right), key=str):
        a, b = remaining_left[key], remaining_right[key]
        if len(a) == len(b) == 1:
            pairs.append(_pair(a[0], b[0], True))
            used_left.add(a[0]["source"]); used_right.add(b[0]["source"])
    unmatched = {"left": [{key: row[key] for key in ("source", "label", "strategy", "mode", "prompt_policy", "state")} for row in left_rows if row["source"] not in used_left],
                 "right": [{key: row[key] for key in ("source", "label", "strategy", "mode", "prompt_policy", "state")} for row in right_rows if row["source"] not in used_right]}
    if unmatched["left"] or unmatched["right"]:
        warnings.append("存在未对齐记录：任务、故障、模式、评价协议、策略或重复位置不同，以及重复键含糊的记录，均不强行配对。")
    if any(not row["task_identity_known"] for row in left_rows + right_rows):
        warnings.append("部分记录缺少完整业务任务身份，仅保留分组统计，不将共同缺失的参数当作同一任务配对。")
    if not left_rows or not right_rows:
        warnings.append("至少一侧没有可识别的主执行记录；静态导入或只有汇总的旧报告不补造逐轮数据。")
    left_groups, right_groups = _summaries(left_rows, left_report), _summaries(right_rows, right_report)
    by_left = {tuple(group["key"]): group for group in left_groups}
    by_right = {tuple(group["key"]): group for group in right_groups}
    group_comparisons = []
    for key in sorted(set(by_left) | set(by_right)):
        a, b = by_left.get(key), by_right.get(key)
        group_comparisons.append({"key": list(key), "left": a, "right": b,
            "metric_deltas": {name: (b["metrics"][name]["rate"] - a["metrics"][name]["rate"]
                if a and b and a["metrics"][name]["rate"] is not None and b["metrics"][name]["rate"] is not None else None) for name in METRICS}})
    def side(job, report, groups, excluded, rows):
        return {"run_id": job.get("run_id"), "label": job.get("label") or "保存的实验", "kind": job.get("kind") or report.get("kind") or "experiment",
                "status": job.get("status") or report.get("status"), "records": len(rows), "groups": groups,
                "excluded": excluded, "usage": _usage(report)}
    return {"comparison_version": VERSION, "read_only": True,
            "left": side(left_job, left_report, left_groups, left_excluded, left_rows),
            "right": side(right_job, right_report, right_groups, right_excluded, right_rows),
            "configuration_differences": differences, "groups": group_comparisons,
            "paired": pairs, "paired_counts": dict(Counter(pair["status"] for pair in pairs)),
            "unmatched": unmatched, "warnings": warnings,
            "matching_note": "按完整业务参数、请求编号、故障、策略、模式、评价协议及重复位置对齐；相同提示层优先。仅有唯一匹配时允许展示不同提示层，并明确标记条件不同。"}
