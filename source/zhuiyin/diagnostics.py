"""Read-only, rule-based links between saved findings and execution evidence."""
from __future__ import annotations

from collections import Counter
from credentials import sensitive_fields


SCHEMA = "diagnostics-v1"
FAULT_LABELS = {
    "commit_response_lost": "提交后响应丢失", "stale_read": "读取过期状态",
    "truncate_response": "响应内容缺失", "latency": "工具调用延迟",
    "precommit_timeout": "写入前超时", "ack_without_commit": "未提交却返回成功",
    "corrupt_payload": "业务内容损坏",
}
STRATEGY_LABELS = {"baseline": "原始执行", "fixed": "幂等＋核验", "verified": "幂等＋核验",
                   "idempotency_only": "仅幂等", "verification_only": "仅核验"}
SUGGESTIONS = {
    "DUPLICATE_RESERVATION": "核对重复写入是否使用同一请求编号，并检查服务端幂等事务；普通查询为空不能单独保证重试安全。",
    "DUPLICATE_TICKET": "核对重复写入是否使用同一请求编号，并检查服务端幂等事务；普通查询为空不能单独保证重试安全。",
    "REPORT_MISMATCH": "对照最后的任务报告与已保存业务记录，确认报告依据；需要确认成功时补充适用的核验凭证。",
    "PAYLOAD_MISMATCH": "逐项核对请求字段与落库字段，检查字段映射和默认值；检测到损坏不表示记录已经修复。",
    "STOCK_INCONSISTENT": "核对初始库存、剩余库存与有效预约数量，检查扣减与写入是否在同一事务内。",
    "NEGATIVE_STOCK": "检查库存更新的条件和事务约束，并用同一请求重复提交验证。",
    "TASK_INCOMPLETE": "核对最终是否存在有效记录；结合提交结果和请求编号决定下一步取证或重试。",
    "AGENT_DID_NOT_FINISH": "查看最后一个工具返回和停止原因，保留部分结果后另建实验检查终止条件。",
    "MODEL_EXECUTION_ERROR": "检查本次连接、请求预算与执行状态；执行异常与业务判定分别处理。",
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _safe(value, context=None, depth=0):
    if depth > 24:
        return "层级过深，未展开"
    if isinstance(value, dict):
        private = sensitive_fields(value, context)
        return {str(key): _safe(item, key, depth + 1) for key, item in value.items()
                if key not in private and key not in {"model_config", "sqlite_dump", "sql_dump"}}
    if isinstance(value, list):
        return [_safe(item, context, depth + 1) for item in value]
    return value


def _records(value):
    obj = _dict(value)
    for key in ("business_records", "reservations", "work_orders", "effects"):
        if isinstance(obj.get(key), list):
            return [row for row in obj[key] if isinstance(row, dict)]
    return []


def _active(rows):
    return [row for row in rows if str(row.get("status", "active")).lower() not in
            {"cancelled", "canceled", "closed", "deleted", "inactive"}]


def _fault(event):
    if event.get("kind") != "fault":
        return None
    explicit = event.get("fault_id") or event.get("fault")
    if isinstance(explicit, str) and explicit in FAULT_LABELS:
        return explicit
    seen, actual = _dict(event.get("observed")), _dict(event.get("actual"))
    action = str(event.get("action", ""))
    if seen.get("code") == "COMMIT_UNKNOWN":
        if actual.get("committed") is True:
            return "commit_response_lost"
        if actual.get("committed") is False:
            return "precommit_timeout"
    if actual.get("committed") is False and seen.get("ok") is True:
        return "ack_without_commit"
    if event.get("tool") == "storage_boundary":
        return "corrupt_payload"
    if not _records(seen) and _records(actual) and event.get("tool") in {"find_ticket", "query_reservations"}:
        return "stale_read"
    if "delay_ms" in seen or "延迟" in action:
        return "latency"
    if "删除响应" in action or "缺失" in action or "截断" in action:
        return "truncate_response"
    return "other_recorded_fault"


def _is_trial(value):
    return isinstance(value, dict) and any(key in value for key in
        ("actual_success", "reported_outcome", "report_assessment", "failure_fingerprint", "business_failed", "static_evaluation"))


def _collect(value, path="report", label="本次执行", config=None, static=False):
    """Select primary trials; minimization/replayed controls are not pooled."""
    value = _dict(value)
    config = {**_dict(config), **_dict(value.get("config"))}
    for key in ("result", "partial_result"):
        if isinstance(value.get(key), dict):
            return _collect(value[key], path + "." + key, label, config, static)
    if _is_trial(value):
        return [(path, label, value, config, static or bool(value.get("static_evaluation")))]
    rows = []
    if isinstance(value.get("baseline"), dict):
        for key in ("baseline", "fixed", "verified"):
            if isinstance(value.get(key), dict):
                name = STRATEGY_LABELS[key] if label == "本次执行" else label + " · " + STRATEGY_LABELS[key]
                rows.append((path + "." + key, name, value[key], config, static))
        return rows
    if isinstance(value.get("samples"), list):
        for index, sample in enumerate(value["samples"]):
            if isinstance(sample, dict):
                name = f"第{sample.get('trial_index', index + 1)}轮 · {sample.get('prompt_policy', '指令未记录')} · {STRATEGY_LABELS.get(sample.get('strategy'), sample.get('strategy', '执行组'))}"
                rows.append((f"{path}.samples[{index}]", name, sample, config, static))
        return rows
    if isinstance(value.get("cases"), list):
        for index, case in enumerate(value["cases"]):
            if not isinstance(case, dict):
                continue
            source = f"{path}.cases[{index}]"
            name = str(case.get("name") or case.get("case_id") or f"用例{index + 1}")
            cfg = {**config, **_dict(case.get("config"))}
            if isinstance(case.get("strategies"), dict) and case["strategies"]:
                for key, trial in case["strategies"].items():
                    if isinstance(trial, dict):
                        rows.append((source + ".strategies." + str(key), name + " · " + STRATEGY_LABELS.get(key, str(key)), trial, cfg, static))
            elif isinstance(case.get("analysis"), dict):
                rows.append((source + ".analysis", name + " · 静态核对", case["analysis"], cfg, True))
            elif isinstance(case.get("result"), dict):
                rows.extend(_collect(case["result"], source + ".result", name, cfg, static))
            elif isinstance(case.get("partial_result"), dict):
                rows.extend(_collect(case["partial_result"], source + ".partial_result", name, cfg, static))
            elif isinstance(case.get("report"), dict):
                rows.extend(_collect(case["report"], source + ".report", name, cfg, static))
            else:
                rows.append((source, name, case, cfg, static))
        return rows
    if isinstance(value.get("analysis"), dict):
        return _collect(value["analysis"], path + ".analysis", label + " · 静态核对", config, True)
    return [(path, label, value, config, static)]


def diagnose_trial(trial, config=None, *, path="report", label="本次执行", static=False):
    """Return findings with zero-based event/record indices into saved evidence."""
    trial = _dict(trial)
    effective = {**trial, **_dict(trial.get("static_evaluation"))}
    config = _dict(config)
    static = static or bool(trial.get("static_evaluation"))
    events = [_safe(item) for item in _list(trial.get("events")) if isinstance(item, dict)]
    records = _safe(_records(trial) or _records(trial.get("final_state")))
    not_run = trial.get("skipped") is True or trial.get("status") in {"not_run", "ready", "duplicate", "conflict", "import_error"}
    execution_codes = {str(item.get("code")) for item in _list(effective.get("execution_violations")) if isinstance(item, dict)}
    incomplete = not not_run and (trial.get("completed") is False or bool(trial.get("infra_error")) or
        trial.get("status") in {"infra_error", "execution_error", "cancelled", "interrupted", "error"} or bool(execution_codes))
    outcome = effective.get("reported_outcome")
    if outcome is None and isinstance(effective.get("reported_success"), bool):
        outcome = "success" if effective["reported_success"] else "failure"
    actual = None if static or not_run or incomplete else effective.get("actual_success")
    execution_status = "not_run" if not_run else "incomplete" if incomplete else "completed" if trial.get("completed") is True or trial.get("status") in {"valid", "completed"} or outcome in {"success", "failure", "unknown"} else "unknown"
    assessment = effective.get("report_assessment")
    status = {
        "business": "仅静态核对上传材料" if static else "未运行" if not_run else "执行未完成，状态未确认" if incomplete else "已实际完成" if actual is True else "未实际完成" if actual is False else "实际状态未记录",
        "report": "未运行" if not_run else {"success": "报告成功", "failure": "报告失败", "unknown": "模型报告无法确认"}.get(outcome, "未提交或未记录报告"),
        "assessment": {"consistent": "与保存的判定一致", "inconsistent": "与保存的判定不一致", "uncertain": "模型主动无法确认", "unreported": "未提交报告", "unverifiable": "证据不足，无法核对"}.get(assessment, "未记录报告检查"),
        "execution": {"not_run": "未运行", "incomplete": "未完成", "completed": "执行已结束", "unknown": "执行状态未记录"}[execution_status],
    }
    activated, first_anomaly = [], None
    for index, event in enumerate(events):
        fault = _fault(event)
        if fault:
            activated.append({"id": fault, "label": FAULT_LABELS.get(fault, "其他已记录故障"), "event_index": index})
        if first_anomaly is None and (fault or len(_active(_records(event.get("actual")))) > 1):
            first_anomaly = index
    findings, known = [], set()
    for key in ("violations", "business_violations", "reporting_violations", "execution_violations"):
        for item in _list(effective.get(key)):
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "RECORDED_FINDING")
            if code in known:
                continue
            known.add(code)
            indexes = []
            if code.startswith("DUPLICATE_"):
                indexes = [i for i, event in enumerate(events) if len(_active(_records(event.get("actual")))) > 1]
            elif code in {"REPORT_MISMATCH", "AGENT_DID_NOT_FINISH"}:
                indexes = [i for i, event in enumerate(events) if event.get("kind") == "report" or event.get("tool") == "finish_task"]
            elif code in {"PAYLOAD_MISMATCH", "STOCK_INCONSISTENT"}:
                indexes = [item["event_index"] for item in activated if item["id"] == "corrupt_payload"]
            findings.append({"code": code, "message": str(item.get("message") or code),
                             "event_indexes": indexes, "record_indexes": list(range(len(records))) if code not in {"AGENT_DID_NOT_FINISH", "MODEL_EXECUTION_ERROR"} else [],
                             "suggestion": SUGGESTIONS.get(code, "对照原始事件、业务约束与最终记录，补充能够独立核对的材料。")})
    if not not_run and outcome == "unknown":
        findings.append({"code": "UNCERTAIN_REPORT", "message": "模型主动报告无法确认；是否合理需要适用的证据合同。", "event_indexes": [i for i, event in enumerate(events) if event.get("kind") == "report" or event.get("tool") == "finish_task"], "record_indexes": [], "suggestion": "检查是否已有完整且匹配当前请求的凭证；如材料不足，可在新实验中增加取证步骤。"})
    configured = config.get("faults") if isinstance(config.get("faults"), list) else None
    return {"path": path, "label": label, "scope": "static_import_only" if static else "saved_execution",
            "execution_status": execution_status, "actual_success": actual, "reported_outcome": outcome,
            "status": status, "findings": findings, "events": events, "records": records,
            "configured_faults": None if configured is None else [{"id": item, "label": FAULT_LABELS.get(item, item)} for item in configured if isinstance(item, str)],
            "activated_faults": activated, "first_anomaly_index": first_anomaly,
            "note": "规则说明只描述保存材料；最早记录异常不等于根因。静态导入不代表独立读取外部业务系统。"}


def analyze_report(report_or_trial):
    """Analyze primary saved results without I/O, model calls, or replay."""
    trials = [diagnose_trial(value, cfg, path=path, label=label, static=static)
              for path, label, value, cfg, static in _collect(report_or_trial)]
    counts = Counter(row["execution_status"] for row in trials)
    return {"schema": SCHEMA, "trials": trials, "counts": {"total": len(trials),
            **{key: counts[key] for key in ("completed", "incomplete", "not_run", "unknown")}},
            "note": "执行组分别展示，不合并不同模式或研究阶段的成功率。"}
