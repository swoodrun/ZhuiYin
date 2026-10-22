"""Untrusted CSV/JSON/JSONL intake and evidence-only trace checks.

This module never executes uploaded code, SQL dumps, tools, or instructions.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import io
import json
import math
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

try:
    from .engine import Config, FAULTS
    from .credentials import is_sensitive_key, sensitive_fields
except ImportError:
    from engine import Config, FAULTS
    from credentials import is_sensitive_key, sensitive_fields

MAX_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 2000
CONFIG_FIELDS = {"equipment", "stock", "quantity", "request_id", "seed", "faults", "agent_mode", "scenario", "ticket_title", "ticket_priority", "prompt_policy"}
EVALUATION_PROTOCOL = "business-report-v2"
IMPORT_VERIFICATION = "static-business-v2.1"
REPORT_OUTCOMES = {"success", "failure", "unknown"}
REPORT_ASSESSMENTS = {"consistent", "inconsistent", "uncertain", "unreported", "unverifiable"}
ALIASES = {"设备名称": "equipment", "库存": "stock", "借用数量": "quantity",
           "请求编号": "request_id", "种子": "seed", "故障": "faults", "业务场景": "scenario", "工单标题": "ticket_title", "优先级": "ticket_priority"}
FAULT_ALIASES = {**{k: k for k in FAULTS}, **{v: k for k, v in FAULTS.items()},
                 "提交后响应丢失": "commit_response_lost", "读取过期状态": "stale_read",
                 "响应内容缺失": "truncate_response", "工具调用延迟": "latency",
                 "提交成功后响应丢失": "commit_response_lost", "恢复查询返回过期结果": "stale_read",
                 "响应缺少非关键字段": "truncate_response", "额外工具延迟": "latency",
                 "首次写入前超时（未提交）": "precommit_timeout", "首次返回成功但未提交": "ack_without_commit",
                 "提交时写入错误业务字段": "corrupt_payload"}
REDACTED = "[已遮蔽]"
BASE_LIMITATIONS = [
    "检查依据上传的日志和最终状态。actual 也是上传者的声明，未经本系统独立验证。",
    "文件摘要用于识别原始内容。来源、完整性和真实性需另外确认。",
    "日志导入只做静态检查，上传的工具、SQL 和代码都不会执行。删除事件后也不会重跑原系统。",
    "检查通过只说明上传记录符合本次数量规则。业务正确性与因果关系需另外验证。",
]


def _fail(message, line=1):
    raise ValueError(f"第 {line} 行：{message}")


def _text(value, label, line, limit=500, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        _fail(f"{label} 必须是{'可为空的' if allow_empty else '非空'}文本，最多 {limit} 字符", line)
    return value.strip()


# Kept for callers of the original upload helper. Both boundary policies use
# exactly one field classifier, while their output behavior remains distinct.
_sensitive = is_sensitive_key


def _redact(value, warnings, context=None):
    if isinstance(value, dict):
        result = {}
        secret_fields = sensitive_fields(value, context)
        for key, item in value.items():
            if key in secret_fields:
                result[key] = REDACTED
                notice = "已递归遮蔽上传内容中的凭据或模型配置字段；摘要仍对应原始文件。"
                if notice not in warnings:
                    warnings.append(notice)
            elif key in ("sqlite_dump", "sql_dump"):
                result[key] = "[已忽略 SQL 文本，未执行]"
            else:
                result[key] = _redact(item, warnings, key)
        return result
    if isinstance(value, list):
        return [_redact(item, warnings, context) for item in value]
    return value


def _json(content, line_offset=0):
    def finite_number(text):
        value = float(text)
        if not math.isfinite(value):
            _fail("JSON 数值必须是有限数字", line_offset + 1)
        return value
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("JSON 对象存在重复字段", line_offset + 1)
            result[key] = value
        return result
    try:
        value = json.loads(content, object_pairs_hook=pairs, parse_float=finite_number,
                           parse_constant=lambda _: _fail("JSON 不支持 NaN 或 Infinity", line_offset + 1))
    except json.JSONDecodeError as exc:
        _fail("JSON 格式错误，请检查引号、逗号和括号", line_offset + exc.lineno)
    except RecursionError:
        _fail("JSON 嵌套过深", line_offset + 1)
    except ValueError as exc:
        if str(exc).startswith("第 "):
            raise
        _fail("JSON 数字过长或格式无效", line_offset + 1)
    # Index source locations so validation points to the actual JSON input line.
    locations, newlines, decoder = {}, [-1] + [m.start() for m in re.finditer("\n", content)], json.JSONDecoder()
    nodes = 0
    def walk(pos, path, depth):
        nonlocal nodes
        while pos < len(content) and content[pos].isspace():
            pos += 1
        line = bisect.bisect_left(newlines, pos) + line_offset
        locations[path] = max(1, line)
        nodes += 1
        if depth > 40 or nodes > 100000:
            _fail("JSON 嵌套或数据结构过于复杂", max(1, line))
        ch = content[pos]
        if ch in "[{":
            close, pos, index = ("}" if ch == "{" else "]"), pos + 1, 0
            while True:
                while content[pos].isspace():
                    pos += 1
                if content[pos] == close:
                    return pos + 1
                if ch == "{":
                    key, pos = decoder.raw_decode(content, pos)
                    while content[pos].isspace():
                        pos += 1
                    pos = walk(pos + 1, path + (key,), depth + 1)
                else:
                    pos = walk(pos, path + (index,), depth + 1)
                    index += 1
                while content[pos].isspace():
                    pos += 1
                if content[pos] == ",":
                    pos += 1
                else:
                    return pos + 1
        return decoder.raw_decode(content, pos)[1]
    walk(0, (), 0)
    return value, locations


def _config(data, line=1, legacy=False, warnings=None):
    if not isinstance(data, dict):
        _fail("场景 config 必须是对象", line)
    unknown = set(data) - CONFIG_FIELDS
    if unknown and not legacy:
        _fail("场景配置含未知字段或模型连接字段；上传只接受场景参数", line)
    if legacy and unknown and warnings is not None:
        warnings.append("旧报告配置中的额外字段及模型连接配置已忽略，重新运行只使用规则模式。")
    cfg = {k: v for k, v in data.items() if k in CONFIG_FIELDS}
    if not legacy and cfg.get("agent_mode", "rules") != "rules":
        _fail("上传场景只能使用 rules 模式；模型连接需在界面单独配置", line)
    cfg["agent_mode"] = "rules"
    if "faults" in cfg:
        faults = cfg["faults"]
        if isinstance(faults, str):
            faults = [s.strip() for s in re.split("[;；]", faults) if s.strip()]
        if not isinstance(faults, list) or any(not isinstance(x, str) or x.strip() not in FAULT_ALIASES for x in faults):
            _fail("故障列表含不支持的类型，请使用界面列出的故障名称", line)
        cfg["faults"] = list(dict.fromkeys(FAULT_ALIASES[x.strip()] for x in faults))
    try:
        return Config.from_dict(cfg).public()
    except ValueError as exc:
        _fail(str(exc), line)


def _scenario(item, index, line):
    if not isinstance(item, dict):
        _fail("每个场景必须是对象", line)
    if "config" in item:
        if set(item) - {"kind", "name", "description", "config"}:
            _fail("场景含未知字段", line)
        if item.get("kind", "scenario") != "scenario":
            _fail("单个场景 kind 必须为 scenario", line)
        name = _text(item.get("name", f"场景 {index}"), "场景名称", line, 120)
        description = _text(item.get("description", ""), "场景说明", line, 2000, True)
        config = _config(item["config"], line)
    else:
        name, description, config = f"场景 {index}", "", _config(item, line)
    return {"name": name, "description": description, "config": config}


def _csv(content):
    reader = csv.reader(io.StringIO(content, newline=""), strict=True)
    try:
        headers = next(reader)
        headers = [ALIASES.get(h.strip(), h.strip()) for h in headers]
        if not headers or len(set(headers)) != len(headers):
            _fail("CSV 列名为空或重复（中英文别名也不能重复）")
        if set(headers) - (CONFIG_FIELDS - {"agent_mode"}):
            _fail("CSV 含未知列；请使用模板中的场景字段")
        if not {"equipment", "stock", "quantity", "request_id"} <= set(headers) and not {"scenario", "ticket_title", "ticket_priority", "request_id"} <= set(headers):
            _fail("CSV 缺少设备名称、库存、借用数量或请求编号列")
        scenarios, ids = [], set()
        previous_line = reader.line_num
        for row in reader:
            line = previous_line + 1
            previous_line = reader.line_num
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(scenarios) >= 20:
                _fail("CSV 最多允许 20 个独立场景", line)
            if len(row) != len(headers):
                _fail("CSV 数据列数与表头不一致", line)
            data = dict(zip(headers, (cell.strip() for cell in row)))
            if data.get("seed") == "":
                data.pop("seed")
            # Blank and omitted CSV faults both mean no fault, never the demo defaults.
            data.setdefault("faults", "")
            config = _config(data, line)
            if config["request_id"] in ids:
                _fail("请求编号重复；每行实验必须使用不同编号", line)
            ids.add(config["request_id"])
            subject = config["ticket_title"] if config["scenario"] == "ticket" else config["equipment"]
            # This label is derived for display only. Keep validated business
            # fields intact so the same import remains valid for batch runs.
            name = f"{subject} · {config['request_id']}"
            if len(name) > 200:
                name = name[:199] + "…"
            scenarios.append({"name": name, "description": "CSV 独立实验场景", "config": config})
        if not scenarios:
            _fail("CSV 至少需要一行场景数据", 2)
        return scenarios
    except StopIteration:
        _fail("CSV 文件为空")
    except csv.Error:
        _fail("CSV 格式错误，请检查引号和分隔符", max(1, reader.line_num))


def _events(data, loc, prefix, request_id, evaluation_protocol=None):
    line = loc.get(prefix, 1)
    if not isinstance(data, list) or len(data) > MAX_EVENTS:
        _fail(f"events 必须是数组，最多 {MAX_EVENTS} 条事件", line)
    normalized, previous = [], 0
    for i, event in enumerate(data):
        line = loc.get(prefix + (i,), line)
        if not isinstance(event, dict):
            _fail(f"第 {i + 1} 条事件必须是对象", line)
        item = dict(event)
        step = item.get("step", previous + 1)
        if isinstance(step, bool) or not isinstance(step, int) or step <= previous:
            _fail(f"第 {i + 1} 条事件 step 必须为严格递增的正整数", line)
        previous, item["step"] = step, step
        item["tool"] = _text(item.get("tool", "未命名工具"), "tool", line, 160)
        for key in ("action", "effect", "kind"):
            if key in item:
                item[key] = _text(item[key], key, line, 4000, True)
        for key in ("observed", "actual"):
            if key in item and item[key] is not None and not isinstance(item[key], dict):
                _fail(f"事件 {key} 必须是对象或 null", line)
        item.setdefault("observed", None)
        item.setdefault("actual", None)
        item["request_id"] = _text(item.get("request_id", request_id), "事件 request_id", line, 120, True)
        for key in ("observed", "actual"):
            value = item[key] or {}
            for flag in ("ok", "success", "committed", "timed_out", "timeout"):
                if flag in value and not isinstance(value[flag], bool):
                    if (evaluation_protocol == EVALUATION_PROTOCOL and key == "observed" and flag == "success"
                            and value[flag] is None and value.get("outcome") == "unknown"
                            and _report_event(item)):
                        continue
                    _fail(f"事件 {key}.{flag} 必须是布尔值", line)
        observed = item["observed"] or {}
        if evaluation_protocol == EVALUATION_PROTOCOL and _report_event(item) and "outcome" in observed:
            _validate_outcome(observed["outcome"], line, allow_null=False)
            if "success" in observed and observed["success"] is not _outcome_success(observed["outcome"]):
                _fail("报告事件 outcome 与 success 不一致", line)
        normalized.append(item)
    return normalized


def _report_event(event):
    return event.get("kind") == "report" or event["tool"] in {"finish_task", "report_task", "report_result"}


def _validate_outcome(value, line, allow_null=True):
    if value is None and allow_null:
        return
    if not isinstance(value, str) or value not in REPORT_OUTCOMES:
        _fail("reported_outcome/outcome 仅支持 success、failure、unknown 或 null", line)


def _outcome_success(outcome):
    return True if outcome == "success" else False if outcome == "failure" else None


def _evaluation_fields(source, line):
    """Keep v2 source judgments as declared evidence, not a new execution."""
    result = {}
    if "reported_outcome" in source:
        _validate_outcome(source["reported_outcome"], line)
        result["reported_outcome"] = source["reported_outcome"]
    if "report_assessment" in source:
        value = source["report_assessment"]
        if not isinstance(value, str) or value not in REPORT_ASSESSMENTS:
            _fail("report_assessment 不受支持", line)
        result["report_assessment"] = value
    for key in ("actual_success", "business_failed", "report_failed"):
        if key in source:
            if source[key] is not None and not isinstance(source[key], bool):
                _fail(f"{key} 必须是布尔值或 null", line)
            result[key] = source[key]
    for key in ("business_violations", "reporting_violations", "execution_violations"):
        if key not in source:
            continue
        values = source[key]
        if not isinstance(values, list) or len(values) > 100:
            _fail(f"{key} 必须是数组，最多 100 条", line)
        result[key] = []
        for item in values:
            if not isinstance(item, dict):
                _fail(f"{key} 每条记录必须是对象", line)
            result[key].append({"code": _text(item.get("code"), f"{key}.code", line, 120),
                                "message": _text(item.get("message", ""), f"{key}.message", line, 4000, True)})
    return result


def _valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _factor_manifest(value):
    """Retain only public factor switches and fingerprints of prompt/tools."""
    if not isinstance(value, dict):
        return None
    safe = {}
    policy = value.get("prompt_policy")
    if isinstance(policy, str) and policy in {"neutral", "recovery"}:
        safe["prompt_policy"] = policy
    for key in ("idempotency", "verification"):
        if isinstance(value.get(key), bool):
            safe[key] = value[key]
    for key in ("system_prompt_sha256", "tool_schema_sha256"):
        if _valid_hash(value.get(key)):
            safe[key] = value[key]
    for key, allowed in (("parameter_scope", "server_supplied_business_fields"),
                         ("verification_enforcement", "agent_choice")):
        if value.get(key) == allowed:
            safe[key] = allowed
    return safe


def _expected_fields(contracts, line):
    """Validate a small data-only payload contract; no expressions or SQL."""
    if "expected_fields" not in contracts:
        return None
    expected = contracts["expected_fields"]
    allowed = {"title", "priority", "equipment", "quantity"}
    if not isinstance(expected, dict) or not expected or set(expected) - allowed:
        _fail("expected_fields 只接受 title、priority、equipment、quantity 业务字段", line)
    for key, value in expected.items():
        if key == "quantity":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000000:
                _fail("expected_fields.quantity 必须是 1—1000000 的整数", line)
        elif key == "priority":
            if not isinstance(value, str) or value not in {"low", "normal", "high", "urgent"}:
                _fail("expected_fields.priority 不受支持", line)
        else:
            _text(value, "expected_fields." + key, line, 200 if key == "title" else 80)
    return dict(expected)


def _effects(final_state, line):
    if final_state is None:
        return None
    if not isinstance(final_state, dict):
        _fail("final_state 必须是对象", line)
    for flag in ("success", "actual_success"):
        if flag in final_state and final_state[flag] is not None and not isinstance(final_state[flag], bool):
            _fail(f"final_state.{flag} 必须是布尔值或 null", line)
    if "effects" not in final_state:
        return None
    rows = final_state["effects"]
    if not isinstance(rows, list) or len(rows) > 10000:
        _fail("final_state.effects 必须是数组，最多 10000 条", line)
    normalized, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("final_state.effects 中每条效果必须是对象", line)
        effect = dict(row)
        raw_id = effect.get("effect_id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            _fail("effect_id 必须是非空文本或整数", line)
        effect["effect_id"] = _text(str(raw_id), "effect_id", line, 120)
        effect["request_id"] = _text(effect.get("request_id"), "效果 request_id", line, 120)
        effect["status"] = _text(effect.get("status"), "效果 status", line, 80)
        if effect["status"] not in {"active", "inactive", "cancelled", "canceled", "deleted", "failed", "rolled_back", "compensated"}:
            _fail("效果 status 仅支持 active、inactive、cancelled、canceled、deleted、failed、rolled_back、compensated；请明确哪些效果仍有效", line)
        if effect["effect_id"] in seen:
            _fail("final_state.effects 中 effect_id 重复，无法确定是否为不同效果", line)
        seen.add(effect["effect_id"])
        normalized.append(effect)
    return normalized


def _effect_history(events):
    history = defaultdict(lambda: defaultdict(set))
    for event in events:
        actual = event.get("actual") or {}
        for key in ("effect_id", "reservation_id", "ticket_id", "order_id"):
            value = actual.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                history[event["request_id"]][str(value)].add(event["step"])
        for key, rows in actual.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = next((row[k] for k in ("effect_id", "reservation_id", "ticket_id", "order_id", "id") if k in row), None)
                rid = row.get("request_id", event["request_id"])
                if isinstance(value, (str, int)) and not isinstance(value, bool) and isinstance(rid, str):
                    history[rid][str(value)].add(event["step"])
    return history


def _analyze(trace, loc, prefix=(), name="上传日志", execution_status=None, stopped_reason=None, agent_mode=None):
    line = loc.get(prefix, 1)
    protocol = trace.get("evaluation_protocol")
    if protocol is not None:
        protocol = _text(protocol, "evaluation_protocol", line, 120)
    v2 = protocol == EVALUATION_PROTOCOL
    evaluation = _evaluation_fields(trace, line) if v2 else {}
    task = trace.get("task", {})
    if not isinstance(task, dict):
        _fail("task 必须是对象", line)
    rid = _text(task.get("request_id", ""), "task.request_id", line, 120, True)
    if "goal" in task:
        _text(task["goal"], "task.goal", line, 4000, True)
    events = _events(trace.get("events", []), loc, prefix + ("events",), rid, protocol)
    final_state = trace.get("final_state")
    effects = _effects(final_state, loc.get(prefix + ("final_state",), line))
    contracts = trace.get("contracts", {})
    if not isinstance(contracts, dict):
        _fail("contracts 必须是对象", line)
    maximum = contracts.get("max_active_effects_per_request", 1)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 20:
        _fail("max_active_effects_per_request 必须是 1—20 的整数", line)
    task_feasible = contracts.get("task_feasible")
    if "task_feasible" in contracts and not isinstance(task_feasible, bool):
        _fail("task_feasible 必须是布尔值；未知时请省略该契约", line)
    expected_fields = _expected_fields(contracts, loc.get(prefix + ("contracts",), line))
    findings, limitations = [], BASE_LIMITATIONS[:]
    if not contracts:
        limitations.append("没有提供数量规则，默认每个请求最多一条 active 写入记录。")
    def add(code, severity, message, steps, evidence):
        findings.append({"code": code, "severity": severity, "message": message,
                         "steps": sorted(set(steps)), "evidence": evidence})
    history = _effect_history(events)
    for event in events:
        actual, observed, step = event["actual"] or {}, event["observed"] or {}, event["step"]
        error = (observed.get("ok") is False or observed.get("success") is False or bool(observed.get("error")) or
                 observed.get("timeout") is True or observed.get("timed_out") is True or
                 str(observed.get("status", "")).lower() in ("error", "timeout", "timed_out", "failed") or
                 "TIMEOUT" in str(observed.get("code", "")).upper() or observed.get("code") == "COMMIT_UNKNOWN")
        if actual.get("committed") is True and error:
            add("AMBIGUOUS_COMMIT", "warning", "上传记录声明已提交，但工具返回错误或超时；直接重试可能重复执行。",
                [step], {"request_id": event["request_id"], "tool": event["tool"], "actual": actual, "observed": observed})
        empty_keys = [key for key, rows in actual.items() if isinstance(rows, list) and rows and observed.get(key) == []]
        if empty_keys:
            add("STALE_READ", "warning", "同一步查询返回空列表，而上传的 actual 声明存在记录。",
                [step], {"tool": event["tool"], "fields": empty_keys, "actual": actual, "observed": observed})
    active = Counter(row["request_id"] for row in (effects or []) if row["status"] == "active")
    payload_incomplete = False
    payload_mismatch_requests = set()
    if effects is not None:
        for request, count in active.items():
            if count > maximum:
                rows = [row for row in effects if row["request_id"] == request and row["status"] == "active"]
                steps = [s for values in history.get(request, {}).values() for s in values]
                add("DUPLICATE_EFFECT", "error", f"上传最终状态中同一请求有 {count} 条 active 写入记录，超过规则上限 {maximum}。",
                    steps, {"request_id": request, "active_count": count, "maximum": maximum, "effects": rows})
        if expected_fields is not None:
            limitations.append("业务字段检查仅将上传的 active 最终快照与来源场景形成的 expected_fields 契约静态比较，未经本系统独立数据库核验。")
            if not rid:
                payload_incomplete = True
                limitations.append("业务字段契约缺少 task.request_id，无法将期望字段归属到指定请求。")
            for row in effects:
                if row["status"] != "active" or row["request_id"] != rid:
                    continue
                missing = [key for key in expected_fields if key not in row]
                differences = {key: {"expected": value, "actual": row[key]} for key, value in expected_fields.items()
                               if key in row and (type(row[key]) is not type(value) or row[key] != value)}
                steps = history.get(rid, {}).get(row["effect_id"], set())
                if differences:
                    payload_mismatch_requests.add(rid)
                    add("PAYLOAD_MISMATCH", "error", "上传最终快照的有效业务记录与期望业务字段不一致。",
                        steps, {"request_id": rid, "effect_id": row["effect_id"], "differences": differences})
                if missing:
                    payload_incomplete = True
                    add("PAYLOAD_EVIDENCE_MISSING", "warning", "上传最终快照缺少期望业务字段，无法完成字段核对。",
                        steps, {"request_id": rid, "effect_id": row["effect_id"], "missing_fields": missing,
                                "expected_fields": expected_fields})
    else:
        limitations.append("请补充 final_state.effects。历史事件只能说明过去的写入，最终是否仍然重复需要最终状态确认。")
        for request, values in history.items():
            if len(values) > maximum:
                add("POSSIBLE_DUPLICATE_EFFECT", "warning", "历史记录出现多个不同写入编号；最终状态缺失，当前是否仍然重复未知。",
                    [s for steps in values.values() for s in steps], {"request_id": request, "effect_ids": sorted(values)})
    reports = []
    for event in events:
        observed = event["observed"] or {}
        if not _report_event(event):
            continue
        if v2 and "outcome" in observed:
            reports.append((event["request_id"], observed["outcome"], event["step"]))
        elif "success" in observed:
            outcome = ("success" if observed["success"] else "failure") if v2 else observed["success"]
            reports.append((event["request_id"], outcome, event["step"]))
    if "reported_success" in trace:
        if trace["reported_success"] is not None and not isinstance(trace["reported_success"], bool):
            _fail("reported_success 必须是布尔值或 null", line)
        if v2 and "reported_outcome" in trace:
            if trace["reported_success"] is not _outcome_success(trace["reported_outcome"]):
                _fail("reported_outcome 与 reported_success 不一致", line)
        elif trace["reported_success"] is not None:
            outcome = ("success" if trace["reported_success"] else "failure") if v2 else trace["reported_success"]
            reports.append((rid, outcome, None))
    if v2 and trace.get("reported_outcome") is not None:
        reports.append((rid, trace["reported_outcome"], None))
    # Evaluate the supplied snapshot before comparing any report assertions.
    # Uploaded actual_success is a source claim, not an overriding static judge.
    def static_business_success(request):
        if effects is None or not request or execution_status == "not_run":
            return None
        if not 1 <= active[request] <= maximum or request in payload_mismatch_requests:
            return False
        if expected_fields is not None and (request != rid or payload_incomplete):
            return None
        return True

    if (v2 and task_feasible is True and effects is not None and rid and active[rid] == 0
            and execution_status != "not_run"
            and not any(item["code"] == "MODEL_EXECUTION_ERROR" for item in evaluation.get("execution_violations", []))):
        add("TASK_INCOMPLETE", "error", "上传契约声明任务具备执行条件，但最终快照没有该请求的 active 业务记录。", [],
            {"request_id": rid, "active_count": 0, "task_feasible": True})
    assessments, report_checks = [], []
    for request, reported, step in reports:
        if v2 and reported == "unknown":
            assessments.append("uncertain")
            report_checks.append({"request_id": request, "reported_outcome": reported,
                                  "step": step, "assessment": "uncertain"})
            continue
        # A report cannot be assigned to one request merely by guessing from tool names.
        if effects is None or not request:
            if v2:
                assessments.append("unverifiable")
                report_checks.append({"request_id": request, "reported_outcome": reported,
                                      "step": step, "assessment": "unverifiable", "actual_success": None})
            continue
        if v2 or expected_fields is not None:
            expected = static_business_success(request)
        else:
            # Preserve the original count/source-flag semantics for old protocols.
            expected = 1 <= active[request] <= maximum
            if isinstance(final_state.get("success"), bool) and request == rid:
                expected = final_state["success"]
            if isinstance(final_state.get("actual_success"), bool) and request == rid:
                expected = final_state["actual_success"]
        success = _outcome_success(reported) if v2 else reported
        if expected is None:
            assessments.append("unverifiable")
            report_checks.append({"request_id": request, "reported_outcome": reported,
                                  "step": step, "assessment": "unverifiable", "actual_success": None})
            continue
        assessment = "consistent" if success == expected else "inconsistent"
        assessments.append(assessment)
        report_checks.append({"request_id": request, "reported_outcome": reported,
                              "step": step, "assessment": assessment, "actual_success": expected})
        if success != expected:
            add("REPORT_MISMATCH", "error", "任务报告 success 与上传最终状态的判定不一致。", [] if step is None else [step],
                {"request_id": request, "reported_success": success, "final_success": expected,
                 "active_count": active[request], "maximum": maximum})
    # A later consistent/unknown statement does not erase an earlier false assertion.
    final_assessment = next((value for value in ("inconsistent", "unverifiable", "uncertain", "consistent")
                             if value in assessments), "unreported")
    actual_present = any(isinstance(event["actual"], dict) and bool(event["actual"]) for event in events)
    if not actual_present:
        limitations.append("事件缺少 actual 记录，无法检查实际提交与工具返回之间的差异。")
    incomplete = (effects is None or not actual_present or not events or payload_incomplete or
                  (v2 and final_assessment == "unverifiable"))
    verdict = "violation" if any(f["severity"] == "error" for f in findings) else "inconclusive" if incomplete else "no_violation_found"
    summary = {"violation": "上传记录违反数量规则，或任务报告与最终状态不一致。", "inconclusive": "记录不足，无法判定；请补充事件 actual 与最终写入状态。",
               "no_violation_found": "检查完成，上传记录中未发现违反当前数量规则的情况。"}[verdict]
    if expected_fields is not None:
        summary = {"violation": "上传快照违反数量或业务字段契约，或任务报告与最终状态不一致。",
                   "inconclusive": "记录不足，无法完成数量、业务字段及报告检查；请查看缺失证据。",
                   "no_violation_found": "静态检查完成，上传快照未违反本次数量及期望业务字段契约。"}[verdict]
    if execution_status in {"not_run", "incomplete"}:
        verdict = "inconclusive"
        summary = ("该组未运行，业务结果未知。" if execution_status == "not_run"
                   else "该组执行未完成，业务结果尚无法判断；已保留现有记录。")
    requests = {e["request_id"] for e in events if e["request_id"]} | {e["request_id"] for e in (effects or [])}
    if rid:
        requests.add(rid)
    normalized_final = None if final_state is None else dict(final_state)
    if effects is not None:
        normalized_final["effects"] = effects
    result = {"name": name, "task": {"request_id": rid, "goal": task.get("goal", "")},
            "contracts": {"max_active_effects_per_request": maximum}, "final_state": normalized_final,
            "verdict": verdict, "summary": summary, "findings": findings,
            "stats": {"events": len(events), "requests": len(requests), "effects": len(effects) if effects is not None else sum(map(len, history.values()))},
            "events": events, "limitations": limitations}
    if expected_fields is not None:
        result["contracts"]["expected_fields"] = expected_fields
    if task_feasible is not None:
        result["contracts"]["task_feasible"] = task_feasible
    if execution_status is not None:
        result.update(execution_status=execution_status, stopped_reason=stopped_reason,
                      agent_mode=agent_mode)
    if protocol is not None:
        result["evaluation_protocol"] = protocol
    if v2:
        source_evaluation = dict(evaluation)
        for key in ("evaluation_protocol", "prompt_policy", "completed"):
            if key in trace:
                source_evaluation[key] = trace[key]
        if "reported_success" in trace:
            source_evaluation["reported_success"] = trace["reported_success"]
        if final_state is not None:
            for key in ("actual_success", "success"):
                if key in final_state:
                    source_evaluation[key] = final_state[key]
        business_violations = [dict(f) for f in findings if f["severity"] == "error" and f["code"] != "REPORT_MISMATCH"]
        reporting_violations = [dict(f) for f in findings if f["code"] == "REPORT_MISMATCH"]
        actual_success = static_business_success(rid)
        static_evaluation = {"actual_success": actual_success,
            "business_failed": bool(business_violations) if business_violations else
                               (None if actual_success is None else False),
            "report_assessment": final_assessment,
            "report_failed": True if reporting_violations else None if final_assessment in {"unverifiable", "unreported"} else False,
            "business_violations": business_violations, "reporting_violations": reporting_violations,
            "scope": "quantity_and_expected_fields" if expected_fields is not None else "quantity_only",
            "evidence_complete": (effects is not None and bool(rid) and not payload_incomplete
                                  and execution_status != "not_run"),
            "report_checks": report_checks}
        static_evaluation["source_disagreements"] = [
            {"field": key, "source": source_evaluation[key], "static": static_evaluation[key]}
            for key in ("actual_success", "business_failed", "report_failed", "report_assessment")
            if key in source_evaluation and static_evaluation[key] is not None
            and source_evaluation[key] != static_evaluation[key]]
        result.update(static_evaluation)
        result.update(import_verification=IMPORT_VERIFICATION,
                      source_evaluation=source_evaluation, static_evaluation=static_evaluation)
        # Execution classifications are retained source evidence; static import
        # cannot execute the original workflow or independently rejudge them.
        result["execution_violations"] = evaluation.get("execution_violations", [])
        result["reported_outcome"] = (reports[-1][1] if reports else evaluation.get("reported_outcome"))
        result["reported_success"] = _outcome_success(result["reported_outcome"])
        policy = trace.get("prompt_policy")
        if isinstance(policy, str) and policy in {"neutral", "recovery"}:
            result["prompt_policy"] = policy
        manifest = _factor_manifest(trace.get("factor_manifest"))
        if manifest is not None:
            result["factor_manifest"] = manifest
    return result


def _report_trial(trial, scenario, line, report_stop, report_mode, report_protocol=None):
    """Convert only known report fields; SQL dumps remain inert and are omitted."""
    if not isinstance(trial, dict):
        _fail("旧报告的 baseline 和 fixed 必须是对象", line)
    for key in ("skipped", "completed"):
        if key in trial and not isinstance(trial[key], bool):
            _fail(f"旧报告 {key} 必须是布尔值", line)
    status = trial.get("status")
    if status is not None and (not isinstance(status, str) or status not in {"not_run", "infra_error", "finished", "completed"}):
        _fail("旧报告组状态不受支持", line)
    not_run = trial.get("skipped") is True or status == "not_run"
    incomplete = not_run or trial.get("completed") is False or status == "infra_error"
    state = "not_run" if not_run else "incomplete" if incomplete else "completed"
    reason = trial.get("stopped_reason", report_stop if incomplete else None)
    if reason is not None:
        reason = _text(reason, "stopped_reason", line, 120)
    mode = trial.get("agent_mode", report_mode)
    if mode is not None and (not isinstance(mode, str) or mode not in {"rules", "llm"}):
        _fail("旧报告组 agent_mode 仅支持 rules 或 llm", line)
    events = trial.get("events", [])
    rows = trial.get("reservations", trial.get("business_records"))
    protocol = trial.get("evaluation_protocol", report_protocol)
    if protocol is not None:
        protocol = _text(protocol, "evaluation_protocol", line, 120)
    if not_run and (trial.get("completed") is True or events or rows or
                    any(trial.get(key) is not None for key in ("failed", "actual_success", "reported_success", "reported_outcome"))):
        _fail("未运行组不能同时声明已完成、业务结果或执行记录", line)
    final = None
    if rows is not None:
        if not isinstance(rows, list):
            _fail("旧报告 reservations 必须是数组", line)
        effects = []
        for row in rows:
            if not isinstance(row, dict):
                _fail("旧报告每条 reservation 必须是对象", line)
            effects.append({**row, "effect_id": row.get("id"),
                            "request_id": row.get("request_id", scenario["request_id"])})
        final = {"effects": effects}
        if "actual_success" in trial:
            final["actual_success"] = trial["actual_success"]
    elif not_run:
        final = {"effects": [], "actual_success": None}
    trace = {"task": {"request_id": scenario["request_id"]}, "events": events,
             "reported_success": trial.get("reported_success"), "final_state": final}
    if protocol is not None:
        trace["evaluation_protocol"] = protocol
    if protocol == EVALUATION_PROTOCOL:
        trace.update(_evaluation_fields(trial, line))
        if "completed" in trial:
            trace["completed"] = trial["completed"]
        trace["prompt_policy"] = trial.get("prompt_policy", scenario.get("prompt_policy"))
        trace["factor_manifest"] = _factor_manifest(trial.get("factor_manifest"))
        trace["contracts"] = {"max_active_effects_per_request": 1,
                              "task_feasible": scenario["scenario"] == "ticket" or scenario["stock"] >= scenario["quantity"],
                              "expected_fields": ({"title": scenario["ticket_title"], "priority": scenario["ticket_priority"]}
                                                  if scenario["scenario"] == "ticket" else
                                                  {"equipment": scenario["equipment"], "quantity": scenario["quantity"]})}
        # A v2 source may omit the compatibility bool. Only an explicitly
        # supplied bool can contradict its tri-state outcome.
        if "reported_success" not in trial:
            trace.pop("reported_success")
    return trace, {"execution_status": state, "stopped_reason": reason, "agent_mode": mode}


def _report_metadata(data):
    """Keep optional, bounded audit labels without copying connection settings."""
    result = {}
    source_report = {}
    if "status" in data:
        status = data["status"]
        if not isinstance(status, str) or status not in {"finished", "partial", "done", "error", "interrupted", "cancelled"}:
            _fail("来源报告 status 不受支持")
        source_report["status"] = status
    if "completed" in data:
        if not isinstance(data["completed"], bool):
            _fail("来源报告 completed 必须是布尔值")
        source_report["completed"] = data["completed"]
    if "stopped_reason" in data:
        reason = data["stopped_reason"]
        source_report["stopped_reason"] = None if reason is None else _text(reason, "来源报告 stopped_reason", 1, 120)
    if "evaluation_protocol" in data:
        protocol = _text(data["evaluation_protocol"], "evaluation_protocol", 1, 120)
        result["evaluation_protocol"] = source_report["evaluation_protocol"] = protocol
    policy = data.get("config", {}).get("prompt_policy") if isinstance(data.get("config"), dict) else None
    if isinstance(policy, str) and policy in {"neutral", "recovery"}:
        source_report["prompt_policy"] = policy
    if source_report:
        result["source_report"] = source_report
    mode = data.get("mode", data.get("config", {}).get("agent_mode"))
    if isinstance(mode, str) and mode in {"rules", "llm"}:
        result["mode"] = mode
    identity = data.get("model_identity")
    if identity is None and "model_identity" in data:
        result["model_identity"] = None
    elif isinstance(identity, dict):
        model = identity.get("model")
        if isinstance(model, str) and len(model) <= 500:
            origin = None
            raw_origin = identity.get("api_origin")
            if isinstance(raw_origin, str) and len(raw_origin) <= 2000:
                try:
                    parsed = urlsplit(raw_origin)
                    if (parsed.scheme in {"http", "https"} and parsed.hostname
                            and parsed.username is None and parsed.password is None
                            and not parsed.path and not parsed.query and not parsed.fragment):
                        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
                        origin = f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
                except (TypeError, ValueError):
                    pass
            result["model_identity"] = {"model": model, "api_origin": origin}
            if isinstance(identity.get("thinking"), str) and identity["thinking"] in {"enabled", "disabled"}:
                result["model_identity"]["thinking"] = identity["thinking"]
    provenance = data.get("provenance")
    if isinstance(provenance, dict):
        safe = {}
        for key in ("app_version", "prompt_version", "evaluation_protocol"):
            value = provenance.get(key)
            if isinstance(value, str) and 0 < len(value) <= 120:
                safe[key] = value
        if isinstance(provenance.get("prompt_policy"), str) and provenance["prompt_policy"] in {"neutral", "recovery"}:
            safe["prompt_policy"] = provenance["prompt_policy"]
        settings = provenance.get("model_request_settings")
        if (isinstance(settings, dict) and isinstance(settings.get("thinking"), str)
                and settings["thinking"] in {"enabled", "disabled"}):
            safe["model_request_settings"] = {"thinking": settings["thinking"]}
        for key in ("prompt_definition_sha256", "source_tree_sha256", "public_config_sha256"):
            if _valid_hash(provenance.get(key)):
                safe[key] = provenance[key]
        hashes = provenance.get("source_sha256")
        if isinstance(hashes, dict) and len(hashes) <= 100:
            safe["source_sha256"] = {name: value for name, value in hashes.items()
                                     if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.py", name)
                                     and _valid_hash(value)}
        design = provenance.get("factor_design")
        if isinstance(design, dict):
            factors = {}
            policy = design.get("instruction_policy")
            if isinstance(policy, str) and policy in {"neutral", "recovery"}:
                factors["instruction_policy"] = policy
            if isinstance(design.get("system_prompt_shared_within_policy"), bool):
                factors["system_prompt_shared_within_policy"] = design["system_prompt_shared_within_policy"]
            capabilities = design.get("capability_factors")
            if (isinstance(capabilities, list) and len(capabilities) <= 2
                    and all(isinstance(item, str) and item in {"idempotency", "verification"} for item in capabilities)):
                factors["capability_factors"] = list(dict.fromkeys(capabilities))
            manifests = design.get("manifests")
            if isinstance(manifests, dict):
                factors["manifests"] = {key: _factor_manifest(manifests[key]) for key in
                                         ("baseline", "idempotency_only", "verification_only", "verified")
                                         if key in manifests and isinstance(manifests[key], dict)}
            safe["factor_design"] = factors
        result["provenance"] = safe
    return result


def _jsonl(content, warnings):
    trace, locations, seen = {"events": []}, {}, set()
    for number, text in enumerate(content.splitlines(), 1):
        if not text.strip():
            continue
        value, _ = _json(text, number - 1)
        if not isinstance(value, dict):
            _fail("JSONL 每行必须是对象", number)
        _redact(value, warnings)
        kind = value.get("type", "event")
        if kind == "meta":
            if "meta" in seen:
                _fail("JSONL 只能有一条 meta 记录", number)
            seen.add("meta")
            for key in ("name", "task", "contracts", "evaluation_protocol", "reported_outcome", "reported_success"):
                if key in value:
                    trace[key] = value[key]
                    locations[(key,)] = number
        elif kind == "final_state":
            if "final_state" in seen:
                _fail("JSONL 只能有一条 final_state 记录", number)
            seen.add("final_state")
            trace["final_state"] = value.get("final_state", {k: v for k, v in value.items() if k != "type"})
            locations[("final_state",)] = number
        elif kind == "event":
            event = value.get("event", {k: v for k, v in value.items() if k != "type"})
            locations[("events", len(trace["events"]))] = number
            trace["events"].append(event)
            if len(trace["events"]) > MAX_EVENTS:
                _fail(f"日志最多允许 {MAX_EVENTS} 条事件", number)
        else:
            _fail("JSONL type 只支持 meta、event、final_state", number)
    if not trace["events"]:
        _fail("日志至少需要一条事件")
    return trace, locations


def parse_upload(filename: str, content: str) -> dict:
    """Parse an upload into sanitized scenarios or static trace analyses.

    Raises ValueError with a Chinese source-line message for invalid data.
    """
    if not isinstance(filename, str) or not filename.strip() or not isinstance(content, str):
        _fail("需要文件名和 UTF-8 文本内容")
    filename = re.split(r"[/\\]", filename)[-1]
    if not filename or len(filename) > 180 or any(ord(ch) < 32 for ch in filename):
        _fail("文件名无效或过长")
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError:
        _fail("文件内容不是有效 UTF-8 文本")
    if not raw or len(raw) > MAX_BYTES:
        _fail("文件为空或超过 2MB UTF-8 大小限制")
    if "\x00" in content:
        _fail("文件包含二进制内容，请上传 UTF-8 文本")
    digest, content = hashlib.sha256(raw).hexdigest(), content.removeprefix("\ufeff")
    if not content.strip():
        _fail("文件没有数据")
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix not in {"csv", "json", "jsonl", "ndjson"}:
        _fail("只支持 .csv、.json、.jsonl 或 .ndjson 文本文件")
    warnings = []
    base = {"filename": filename, "source_sha256": digest, "warnings": warnings}
    if suffix == "csv":
        return {**base, "kind": "scenarios", "scenarios": _csv(content)}
    if suffix in {"jsonl", "ndjson"}:
        data, locations = _jsonl(content, warnings)
    else:
        data, locations = _json(content)
    if isinstance(data, list):
        data = {"events": data}
        locations = {("events",) + key: val for key, val in locations.items()}
    if not isinstance(data, dict):
        _fail("JSON 顶层必须是对象，或日志事件数组")
    if "kind" in data and not isinstance(data["kind"], str):
        _fail("顶层 kind 必须是文本", locations.get(("kind",), 1))
    if data.get("kind") in {"scenario", "scenarios"} or (not data.get("kind") and "events" not in data and "baseline" not in data and bool(set(data) & CONFIG_FIELDS)):
        if data.get("kind") == "scenarios":
            if set(data) - {"kind", "scenarios"}:
                _fail("scenarios 顶层含未知字段")
            items = data.get("scenarios")
            prefix = ("scenarios",)
        else:
            items, prefix = [data], ()
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            _fail("需要 1—20 个场景")
        scenarios, ids = [], set()
        for index, item in enumerate(items):
            line = locations.get(prefix + (index,), 1) if prefix else 1
            scenario = _scenario(item, index + 1, line)
            rid = scenario["config"]["request_id"]
            if rid in ids:
                _fail("场景请求编号重复", line)
            ids.add(rid)
            scenarios.append(scenario)
        return {**base, "kind": "scenarios", "scenarios": scenarios}
    scenario, report_metadata = None, {}
    if "baseline" in data or "fixed" in data:
        if not all(key in data for key in ("config", "baseline", "fixed")):
            _fail("旧报告需包含 config、baseline、fixed")
        scenario = _config(data["config"], locations.get(("config",), 1), True, warnings)
        report_metadata = _report_metadata(data)
        report_mode = report_metadata.get("mode")
        traces = []
        for key, name in (("baseline", "原始 · 执行记录"), ("fixed", "幂等＋核验 · 执行记录")):
            trial = data[key]
            line = locations.get((key,), 1)
            converted, state = _report_trial(trial, scenario, line, data.get("stopped_reason"), report_mode,
                                             report_metadata.get("evaluation_protocol"))
            traces.append((converted, (key,), name, state))
        warnings.append("已将旧报告的 reservations 转为最终写入记录；SQL 文本和额外字段已忽略，不会执行或导入。")
    else:
        if data.get("kind", "trace") != "trace":
            _fail("日志 kind 必须为 trace")
        if "events" not in data:
            _fail("没有找到 events；场景请使用下载的场景模板")
        name = _text(data.get("name", "上传日志"), "日志名称", locations.get(("name",), 1), 120)
        traces = [(data, (), name, {})]
        report_metadata = _report_metadata({key: data[key] for key in
                                          ("mode", "model_identity", "provenance", "evaluation_protocol") if key in data})
    if sum(len(t[0].get("events", [])) for t in traces if isinstance(t[0].get("events", []), list)) > MAX_EVENTS:
        _fail(f"整份文件最多允许 {MAX_EVENTS} 条事件")
    # Redaction happens before any evidence enters the response. Raw content is
    # used only for parsing and SHA256 and is never stored by this module.
    _redact(data, warnings)
    analyses = [_analyze(trace, locations, prefix, name, **state) for trace, prefix, name, state in traces]
    result = {**base, "kind": "trace", "analyses": _redact(analyses, warnings), **report_metadata}
    if scenario is not None:
        result["scenario"] = scenario
    return result
