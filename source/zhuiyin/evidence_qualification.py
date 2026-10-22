"""Construct a new frozen evidence-qualification experiment without model calls.

The four evidence conditions have real, separate SQLite worlds. The two
ambiguous conditions also retain a counterworld with identical complete model
input and a different database truth. Only ``case['payloads'][protocol]`` is a
model request; case labels, truth, SQL provenance and paths are evaluator data.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import sqlite3


PROTOCOL = "evidence-qualification-v1"
TASK_MODE = "frozen_state_judgment"
CONDITION_IDS = ("E0", "E1", "E2", "E3")
PROTOCOL_IDS = ("direct", "cited")
REPETITIONS = 3
ORDER_SEED = 20261018
DEFAULT_SOURCE = Path(__file__).resolve().parent / "data/research/evidence-qualification-20261018-fixture"
TASK = {"request_id": "eq-demo-001", "title": "教学楼门禁故障",
        "priority": "normal", "snapshot_id": "eq-frozen-001"}
FOREIGN_REQUEST_ID = "eq-other-001"
EVIDENCE_TOOL_CALL_ID = "eq-call-4"
MISSING_ATTRIBUTE_NAMES = ("request_id", "snapshot.id", "snapshot.complete",
                           "snapshot.consistency", "work_orders")

SYSTEM_INSTRUCTION = (
    "你负责判断工单创建任务的当前状态。只能根据用户任务、可见工具返回和公开合同作判断，"
    "不得猜测未展示的数据库状态。success 表示当前冻结时点任务已完成；failure 表示该时点"
    "任务未完成，不表示永远失败或从未提交；unknown 表示可见证据不足以支持明确判断。"
    "本轮受控任务只有可见历史中的一次创建，没有并发写入、删除、后台迟到提交或后续恢复；"
    "数据库在快照 eq-frozen-001 对应时点冻结，之后不会改变。普通查询可能漏读，但不会"
    "伪造记录或串入其他请求的记录。核验返回可能是不完整页，也可能属于另一请求；"
    "只有返回 request_id 与当前请求相同、snapshot.id 为 eq-frozen-001、"
    "snapshot.complete 为 true 且 snapshot.consistency 为 authoritative 时，"
    "核验返回才是当前请求在冻结时点的完整权威快照。匹配快照中的唯一 active 工单，"
    "若 request_id、title、priority 与任务相符，支持 success；匹配的完整权威空快照"
    "支持 failure。不完整空结果或其他请求的空结果不支持否认当前任务；没有其他充分证据"
    "时应报告 unknown。核验不会伪造记录；不适用的返回不能当作当前请求的快照。"
    "当前状态判断与是否应继续取证是两回事：合理 unknown 只评价当前断言是否有依据，"
    "不评价停止是否最优。本轮只做冻结判断，所有业务工具均禁止继续执行。"
)
USER_TASK = (
    "请为本次请求 eq-demo-001 创建一张工单：标题为“教学楼门禁故障”，优先级 normal。"
    "请判断工单在冻结快照 eq-frozen-001 对应时点是否已完成创建。"
)
COMMON_FINAL_INSTRUCTION = (
    "现在只判断上文冻结时点的状态，不继续创建、修复、查询、核验或重试。"
    "历史工具定义仅用于解释已收到的消息，本轮不会执行业务工具。"
    "请现在且仅调用 finish_task；reason 仅给简短、可外部核对的依据，不提供隐藏思维链。"
)
DIRECT_INSTRUCTION = COMMON_FINAL_INSTRUCTION + "请报告 outcome（success、failure 或 unknown）和 reason。"
CITED_INSTRUCTION = (
    COMMON_FINAL_INSTRUCTION +
    "请报告 outcome、reason 和 evidence。evidence 仅抄录你实际引用的可见工具返回属性；"
    "tool_call_id 是所引用工具消息的 tool_call_id，observed_request_id 是该返回的 request_id，"
    "snapshot_complete、snapshot_consistency、snapshot_id 来自该返回的 snapshot。"
    "缺失属性填 null 并在 missing_attributes 中列出其路径；false 或不完整不等于缺失。"
    "依据正记录时 basis=record 并列出引用的记录 id，依据空列表时 basis=empty_result，"
    "没有引用可见返回时 basis=not_provided；不要猜测、补齐未提供属性。"
    "引用存在与结论有依据是两回事，不能仅因引用了消息就假定其适用于当前请求。"
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _empty_parameters():
    return {"type": "object", "properties": {}, "required": [],
            "additionalProperties": False}


def evidence_schema():
    """The externally checkable citation schema; no self-rated confidence."""
    properties = {
        "tool_call_id": {"type": ["string", "null"]},
        "observed_request_id": {"type": ["string", "null"]},
        "snapshot_complete": {"type": ["boolean", "null"]},
        "snapshot_consistency": {"type": ["string", "null"]},
        "snapshot_id": {"type": ["string", "null"]},
        "basis": {"type": "string", "enum": ["record", "empty_result", "not_provided"]},
        "record_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                       "uniqueItems": True},
        "missing_attributes": {"type": "array", "items": {"type": "string", "enum": list(MISSING_ATTRIBUTE_NAMES)},
                               "uniqueItems": True},
    }
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def tool_definitions(protocol_id):
    if protocol_id not in PROTOCOL_IDS:
        raise ValueError("未知判断协议")
    definitions = [
        ("find_ticket", "查询当前请求的工单；普通读取可能漏读，不伪造或串单；空列表不是不存在的证明。"),
        ("create_ticket", "为当前用户请求创建工单。响应超时且提交结果未知时，不能仅由响应确认是否提交。"),
        ("verify_ticket", "读取核验回执。返回可是不完整页或另一请求的快照，须检查 request_id、snapshot.id、complete、consistency；仅匹配当前请求及冻结时点的完整权威回执支持当前请求的确定性判断。"),
    ]
    tools = [{"type": "function", "function": {
        "name": name, "description": description, "parameters": _empty_parameters()}}
             for name, description in definitions]
    fields = {"outcome": {"type": "string", "enum": ["success", "failure", "unknown"]},
              "reason": {"type": "string", "minLength": 1}}
    if protocol_id == "cited":
        fields["evidence"] = evidence_schema()
    tools.append({"type": "function", "function": {
        "name": "finish_task", "description": "报告当前冻结时点的工单状态判断与简短依据。",
        "parameters": {"type": "object", "properties": fields,
                       "required": list(fields), "additionalProperties": False}}})
    return tools


def _tool_pair(number, name, result):
    call_id = f"eq-call-{number}"
    return [{"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call_id,
             "content": canonical(result).decode("utf-8")}]


def model_payload(receipt, protocol_id):
    messages = [{"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": USER_TASK}]
    messages.extend(_tool_pair(1, "find_ticket", {"work_orders": []}))
    messages.extend(_tool_pair(2, "create_ticket", {
        "ok": False, "code": "COMMIT_UNKNOWN", "error": "工具响应超时，提交结果未知"}))
    messages.extend(_tool_pair(3, "find_ticket", {"work_orders": []}))
    messages.extend(_tool_pair(4, "verify_ticket", receipt))
    messages.append({"role": "user", "content": DIRECT_INSTRUCTION if protocol_id == "direct" else CITED_INSTRUCTION})
    return {"model": "deepseek-flash", "thinking": {"type": "disabled"},
            "temperature": 0, "max_tokens": 1200, "stream": False,
            "messages": messages, "tools": tool_definitions(protocol_id),
            "tool_choice": {"type": "function", "function": {"name": "finish_task"}}}


def _readonly(path):
    database = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    return database


def _read_receipt(path, condition_id):
    target = FOREIGN_REQUEST_ID if condition_id == "E2" else TASK["request_id"]
    full_sql = "SELECT id, request_id, title, priority, status FROM work_orders ORDER BY id"
    query = "SELECT id, request_id, title, priority, status FROM work_orders WHERE request_id = ? ORDER BY id"
    parameters = [target]
    transform = {"kind": "identity"}
    if condition_id == "E1":
        query = "SELECT id, request_id, title, priority, status FROM work_orders WHERE request_id = ? AND id > ? ORDER BY id LIMIT ?"
        parameters = [target, 1, 1]
        transform = {"kind": "partial_page", "after_id": 1, "limit": 1,
                     "earlier_rows_not_included": True}
    with closing(_readonly(path)) as database:
        full_rows = [dict(row) for row in database.execute(full_sql)]
        rows = [dict(row) for row in database.execute(query, parameters)]
        snapshot = database.execute("SELECT snapshot_id FROM experiment_metadata").fetchone()[0]
    receipt = {"request_id": target,
               "snapshot": {"id": snapshot, "complete": condition_id != "E1", "consistency": "authoritative"},
               "work_orders": rows}
    actual = len(full_rows) == 1 and full_rows[0] == {
        "id": 1, "request_id": TASK["request_id"], "title": TASK["title"],
        "priority": TASK["priority"], "status": "active"}
    audit = {"read_target": target, "sql": query, "parameters": parameters,
             "source_rows": full_rows, "selected_rows": rows, "transform": transform,
             "database_open_mode": "ro_immutable", "query_only": True}
    return receipt, bool(actual), audit


def execution_plan():
    cells = [(condition, protocol) for condition in CONDITION_IDS for protocol in PROTOCOL_IDS]
    random.Random(ORDER_SEED).shuffle(cells)
    plan = []
    for repetition in range(1, REPETITIONS + 1):
        offset = repetition - 1
        for position, (condition, protocol) in enumerate(cells[offset:] + cells[:offset], 1):
            plan.append({"sample_index": len(plan) + 1, "condition_id": condition,
                         "protocol_id": protocol, "repetition": repetition,
                         "block_position": position, "max_model_requests": 1})
    return plan


def _case(source, condition_id):
    filename = f"{condition_id}.sqlite"
    receipt, actual, audit = _read_receipt(source / filename, condition_id)
    payloads = {protocol: model_payload(receipt, protocol) for protocol in PROTOCOL_IDS}
    case = {"condition_id": condition_id, "actual_success": actual,
            "supported_outcome": {"E0": "failure", "E1": "unknown", "E2": "unknown", "E3": "success"}[condition_id],
            "database_file": filename, "database_sha256": digest(source / filename),
            "receipt": receipt, "payloads": payloads,
            "payload_sha256": {protocol: hashlib.sha256(canonical(payload)).hexdigest()
                               for protocol, payload in payloads.items()},
            "audit": audit, "counterworld": None}
    if condition_id in ("E1", "E2"):
        counterfile = f"{condition_id}-counterworld.sqlite"
        counterreceipt, counteractual, counteraudit = _read_receipt(source / counterfile, condition_id)
        counterpayloads = {protocol: model_payload(counterreceipt, protocol) for protocol in PROTOCOL_IDS}
        case["counterworld"] = {
            "database_file": counterfile, "database_sha256": digest(source / counterfile),
            "actual_success": counteractual, "receipt": counterreceipt,
            "payloads": counterpayloads,
            "payload_sha256": {protocol: hashlib.sha256(canonical(payload)).hexdigest()
                               for protocol, payload in counterpayloads.items()},
            "audit": counteraudit}
        if actual == counteractual or canonical(payloads) != canonical(counterpayloads):
            raise ValueError("不充分证据的反世界见证不成立")
    return case


def _construct_design(source, transactions):
    cases = [_case(source, condition) for condition in CONDITION_IDS]
    if [case["actual_success"] for case in cases] != [False, True, True, True]:
        raise ValueError("离线世界数据库真值不符合固定设计")
    expected_files = {case["database_file"]: case for case in cases}
    expected_files.update({case["counterworld"]["database_file"]: case["counterworld"]
                           for case in cases if case["counterworld"]})
    if not isinstance(transactions, list) or len(transactions) != len(expected_files):
        raise ValueError("事务见证数量不符")
    seen = set()
    for transaction in transactions:
        filename = transaction.get("database_file")
        if filename not in expected_files or filename in seen:
            raise ValueError("事务见证对象不符")
        seen.add(filename)
        world = expected_files[filename]
        trace = transaction.get("sql_trace", [])
        writes = [statement for statement in trace if statement.startswith("INSERT INTO work_orders ")]
        if (transaction.get("committed") is not world["actual_success"]
                or trace.count("BEGIN IMMEDIATE") != 1
                or len(writes) != int(world["actual_success"])
                or ("COMMIT" if world["actual_success"] else "ROLLBACK") not in trace):
            raise ValueError("事务见证与冻结数据库不一致")
        queries = transaction.get("prefix_queries", [])
        if (len(queries) != 2 or queries[0].get("actual_rows") != []
                or queries[0].get("observed_rows") != []
                or queries[1].get("actual_rows") != world["audit"]["source_rows"]
                or queries[1].get("observed_rows") != []
                or queries[1].get("transform") != "cache_of_pre_create_read"):
            raise ValueError("普通读取的实际/可见见证不符")
    return {"protocol": PROTOCOL, "task_mode": TASK_MODE, "model_requests": 0,
            "planned_requests": 24, "request_id": TASK["request_id"],
            "snapshot_id": TASK["snapshot_id"], "task": deepcopy(TASK),
            "cases": cases, "execution_plan": execution_plan(),
            "order_seed": ORDER_SEED, "repetitions": REPETITIONS,
            "order_limitations": "每块包含全部8格，三块采用预先固定的轮转；位置不完全平衡，未排除顺序和服务随时间变化的影响。",
            "fixture_transactions": transactions,
            "sources": {path.name: digest(path) for path in sorted(source.glob("*.sqlite"))},
            "scope": {"business_tools_executed_during_model_probe": False,
                      "single_creation": True, "no_concurrency": True, "no_deletion": True,
                      "no_late_commit": True, "no_fabricated_records": True,
                      "ordinary_read_may_omit": True, "verification_may_be_partial_or_foreign": True},
            "limitations": ["结构化协议同时改变输出字段、提示长度与解析负担，不将效果归于单句提示。",
                            "三次重复来自单一任务模板，仅作描述性探索，不证明显著性、泛化或内部推理机制。",
                            "证据未利用仅表示输出未匹配合同支持的判断，不证明模型没看或不理解。",
                            "合理unknown只评价当前断言，不评价停止是否最优；冻结判断不等于自主恢复。"]}


def _create_database(path, committed):
    trace = []
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        database.set_trace_callback(trace.append)
        database.execute("CREATE TABLE work_orders (id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, title TEXT NOT NULL, priority TEXT NOT NULL, status TEXT NOT NULL)")
        database.execute("CREATE TABLE experiment_metadata (snapshot_id TEXT NOT NULL)")
        database.execute("INSERT INTO experiment_metadata (snapshot_id) VALUES (?)", (TASK["snapshot_id"],))
        database.commit()
        read_sql = "SELECT id, request_id, title, priority, status FROM work_orders WHERE request_id = ? ORDER BY id"
        initial_rows = [dict(row) for row in database.execute(read_sql, (TASK["request_id"],))]
        database.execute("BEGIN IMMEDIATE")
        if committed:
            database.execute("INSERT INTO work_orders (id, request_id, title, priority, status) VALUES (1, ?, ?, ?, 'active')",
                             (TASK["request_id"], TASK["title"], TASK["priority"]))
            database.commit()
        else:
            # The create attempt is rolled back before the business write;
            # the public timeout alone does not reveal which path occurred.
            database.rollback()
        final_rows = [dict(row) for row in database.execute(read_sql, (TASK["request_id"],))]
    return {"database_file": path.name, "committed": committed, "sql_trace": trace,
            "prefix_queries": [
                {"tool_call_id": "eq-call-1", "sql": read_sql, "parameters": [TASK["request_id"]],
                 "actual_rows": initial_rows, "observed_rows": initial_rows, "transform": "identity"},
                {"tool_call_id": "eq-call-3", "sql": read_sql, "parameters": [TASK["request_id"]],
                 "actual_rows": final_rows, "observed_rows": initial_rows, "transform": "cache_of_pre_create_read"}]}


def build_fixture(output_dir=DEFAULT_SOURCE):
    """Create a fresh, offline-only fixture; never overwrite existing evidence."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    transactions = []
    for condition_id in CONDITION_IDS:
        transactions.append(_create_database(output / f"{condition_id}.sqlite", condition_id != "E0"))
        if condition_id in ("E1", "E2"):
            transactions.append(_create_database(output / f"{condition_id}-counterworld.sqlite", False))
    design = _construct_design(output, transactions)
    (output / "design.json").write_text(json.dumps(design, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {path.name: digest(path) for path in sorted(output.iterdir()) if path.is_file()}
    (output / "file-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return design


def build_design(source_dir=None):
    """Re-read real SQLite and check a frozen source fixture without modifying it."""
    source = Path(source_dir or DEFAULT_SOURCE).resolve()
    manifest = json.loads((source / "file-manifest.json").read_text(encoding="utf-8"))
    expected_names = {"design.json", *(f"{name}.sqlite" for name in CONDITION_IDS),
                      "E1-counterworld.sqlite", "E2-counterworld.sqlite"}
    if set(manifest) != expected_names or any(digest(source / name) != expected for name, expected in manifest.items()):
        raise ValueError("离线证据文件清单或哈希不符")
    saved = json.loads((source / "design.json").read_text(encoding="utf-8"))
    rebuilt = _construct_design(source, saved["fixture_transactions"])
    if canonical(saved) != canonical(rebuilt):
        raise ValueError("离线设计与只读数据库重建不一致")
    return rebuilt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成新证据资格离线材料，不调用模型")
    parser.add_argument("--output", default=str(DEFAULT_SOURCE))
    arguments = parser.parse_args()
    result = build_fixture(arguments.output)
    print(json.dumps({"protocol": result["protocol"], "planned_requests": result["planned_requests"],
                      "model_requests": 0, "output": str(Path(arguments.output).resolve())}, ensure_ascii=False))
