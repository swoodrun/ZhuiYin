"""Offline weakening of a bounded, explicit evidence-qualification predicate.

This tool does not measure model behavior. It reads frozen SQLite itself,
constructs semantically allowed missing/weaker receipts, and records the exact
model-shaped inputs. Local 1-minimality is relative to the five declared
attribute groups and ``visible_support``; it is not a general sufficiency or
causality claim. Malformed candidates are retained separately as not testable.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qualification_scoring import visible_support


VERSION = "evidence-boundary-offline-v1"
DEFAULT_FIXTURE = ROOT / "data/research/evidence-qualification-20261018-fixture"
DEFAULT_OUTPUT = ROOT / "data/research/evidence-qualification-20261018-boundary"
ATTRIBUTE_GROUPS = ("request_binding", "snapshot_time", "coverage", "authority", "records")
PATHS = {
    "request_binding": ("request_id",),
    "snapshot_time": ("snapshot", "id"),
    "coverage": ("snapshot", "complete"),
    "authority": ("snapshot", "consistency"),
    "records": ("work_orders",),
}
TASK = {"request_id": "eq-demo-001", "title": "教学楼门禁故障",
        "priority": "normal", "snapshot_id": "eq-frozen-001"}
BOUNDARY_SYSTEM = (
    "你负责判断工单创建任务在冻结时点 eq-frozen-001 的状态。success 表示当前已完成，"
    "failure 表示当前未完成，unknown 表示可见证据不满足本轮资格判据。"
    "本轮只有历史中展示的一次创建，没有并发写入、删除、后台迟到提交或后续恢复，"
    "数据库冻结后不再变化。普通读取可漏读；不能凭普通空列表否认任务。"
    "核验返回的 request_id、snapshot.id、snapshot.complete、snapshot.consistency、work_orders"
    "均可能未提供；返回也可能来自另一请求、其他时点、不完整页或缓存。"
    "缺属性不等于 false 或空列表；不得从工具名称推定其完整、实时或权威。"
    "本轮采用一个明确且保守的合同资格判据：只有返回 request_id 匹配 eq-demo-001、"
    "snapshot.id 匹配 eq-frozen-001、snapshot.complete 严格为 true、"
    "snapshot.consistency 为 authoritative，并且 work_orders 是有效记录列表时，"
    "才依据该回执作确定判断。匹配回执中有唯一 active 且 request_id、title、priority"
    "符合用户任务的记录时支持 success；匹配的完整权威空列表支持 failure。"
    "任一资格属性缺失或削弱时，本轮判据输出 unknown；该规则不主张穷尽所有可能的证据推导。"
    "只作当前状态判断，不执行业务操作；合理 unknown 不评价停止是否最优。"
)
BOUNDARY_VERIFY_DESCRIPTION = (
    "读取核验回执。各资格属性均可缺失，也可能返回其他请求、其他时点、不完整页或缓存。"
    "只在 request_id、snapshot.id、complete、consistency 及记录列表满足公开的条件性合同"
    "时支持当前任务的确定判断；工具名称本身不保证完整、实时或权威。"
)
LIMITATIONS = [
    "合同判据与模型行为是两个问题：本轮 model_behavior=not_measured，模型请求数为0。",
    "局部1-最小仅相对于固定的五组候选属性、删除操作与visible_support判据；不代表唯一、全局、最低成本或因果结论。",
    "资格判据有意保守；按该谓词必要不等于信息论上必要。正记录在无删除等额外假设下可能有其他充分支持路径，本原型未穷尽。",
    "这是新的离线条件性合同，不覆盖或改写原30条、24次计划或其他已封存请求的评分。",
    "无效格式候选单列为不可检验，不与有效但证据不足的unknown合并。",
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_fixture(source):
    manifest = json.loads((source / "file-manifest.json").read_text(encoding="utf-8"))
    names = {"design.json", "E0.sqlite", "E1.sqlite", "E2.sqlite", "E3.sqlite",
             "E1-counterworld.sqlite", "E2-counterworld.sqlite"}
    if (not isinstance(manifest, dict) or set(manifest) != names
            or any(digest(source / name) != expected for name, expected in manifest.items())):
        raise ValueError("来源清单或哈希不符")
    design = json.loads((source / "design.json").read_text(encoding="utf-8"))
    if design.get("task") != TASK:
        raise ValueError("来源任务不符")
    return design, {**manifest, "file-manifest.json": digest(source / "file-manifest.json")}


def _database_audit(path):
    # No statement is loaded from the archived design or its SQL trace.
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"work_orders", "experiment_metadata"}:
            raise ValueError("来源数据库表结构不符")
        columns = [row[1] for row in database.execute("PRAGMA table_info(work_orders)")]
        if columns != ["id", "request_id", "title", "priority", "status"]:
            raise ValueError("来源工单字段不符")
        snapshots = [row[0] for row in database.execute("SELECT snapshot_id FROM experiment_metadata")]
        if snapshots != [TASK["snapshot_id"]]:
            raise ValueError("来源冻结时点不符")
        query = "SELECT id, request_id, title, priority, status FROM work_orders WHERE request_id = ? ORDER BY id"
        rows = [dict(row) for row in database.execute(query, (TASK["request_id"],))]
        foreign_rows = [dict(row) for row in database.execute(query, ("eq-other-001",))]
        total = database.execute("SELECT count(*) FROM work_orders").fetchone()[0]
        query_only = database.execute("PRAGMA query_only").fetchone()[0] == 1
    if total != len(rows):
        raise ValueError("边界来源含额外请求记录")
    expected_row = {"id": 1, "request_id": TASK["request_id"], "title": TASK["title"],
                    "priority": TASK["priority"], "status": "active"}
    actual = rows == [expected_row]
    if rows and not actual:
        raise ValueError("边界来源包含非设计记录")
    return {"database_sha256": digest(path), "database_open_mode": "ro_immutable",
            "query_only": query_only, "sql": query, "parameters": [TASK["request_id"]],
            "rows": rows, "actual_success": actual, "frozen_snapshot_id": snapshots[0],
            "foreign_sql": query, "foreign_parameters": ["eq-other-001"], "foreign_rows": foreign_rows}


def _base_payload(source_case, rows):
    payload = deepcopy(source_case["payloads"]["direct"])
    if len(payload.get("messages", [])) != 11 or payload["messages"][9].get("tool_call_id") != "eq-call-4":
        raise ValueError("来源历史结构不符")
    receipt = {"request_id": TASK["request_id"],
               "snapshot": {"id": TASK["snapshot_id"], "complete": True, "consistency": "authoritative"},
               "work_orders": rows}
    if json.loads(payload["messages"][9]["content"]) != receipt:
        raise ValueError("来源请求与只读SQL不符")
    # The same explicit optional-attribute contract is used for every candidate.
    # No candidate inherits a guarantee that the removed field was always true.
    payload["messages"][0]["content"] = BOUNDARY_SYSTEM
    verify = [tool for tool in payload["tools"] if tool["function"]["name"] == "verify_ticket"]
    if len(verify) != 1:
        raise ValueError("来源核验工具结构不符")
    verify[0]["function"]["description"] = BOUNDARY_VERIFY_DESCRIPTION
    return payload, receipt


def _remove(receipt, attribute):
    result = deepcopy(receipt)
    path = PATHS[attribute]
    parent = result
    for part in path[:-1]:
        parent = parent[part]
    parent.pop(path[-1], None)
    return result


def receipt_validity(receipt):
    """Allow omission and weaker values; reject type/shape corruption separately."""
    if not isinstance(receipt, dict) or not set(receipt) <= {"request_id", "snapshot", "work_orders"}:
        return False, "回执必须为约定对象"
    if "request_id" in receipt and not isinstance(receipt["request_id"], str):
        return False, "request_id类型无效"
    if "snapshot" in receipt:
        snapshot = receipt["snapshot"]
        if not isinstance(snapshot, dict) or not set(snapshot) <= {"id", "complete", "consistency"}:
            return False, "snapshot类型或字段无效"
        if "id" in snapshot and not isinstance(snapshot["id"], str):
            return False, "snapshot.id类型无效"
        if "complete" in snapshot and type(snapshot["complete"]) is not bool:
            return False, "snapshot.complete类型无效"
        if "consistency" in snapshot and not isinstance(snapshot["consistency"], str):
            return False, "snapshot.consistency类型无效"
    if "work_orders" in receipt:
        rows = receipt["work_orders"]
        if not isinstance(rows, list):
            return False, "work_orders类型无效"
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"id", "request_id", "title", "priority", "status"}
                    or type(row["id"]) is not int or row["id"] < 1
                    or any(not isinstance(row[key], str) for key in ("request_id", "title", "priority", "status"))):
                return False, "工单记录类型或字段无效"
    return True, None


def contract_criterion(receipt):
    valid, reason = receipt_validity(receipt)
    if not valid:
        return {"candidate_status": "not_testable", "supported_outcome": None,
                "reason": reason, "criterion": "not_applied_to_malformed_receipt"}
    return {"candidate_status": "valid", "supported_outcome": visible_support(receipt, TASK),
            "reason": None, "criterion": "visible_support_conditional_contract"}


def _variants(base, foreign_rows):
    yield "base", "baseline", [], deepcopy(base)
    for attribute in ATTRIBUTE_GROUPS:
        yield f"without-{attribute}", "remove_attribute", [attribute], _remove(base, attribute)
    changes = (
        ("foreign-request", "request_binding", ("request_id",), "eq-other-001"),
        ("unmatched-snapshot", "snapshot_time", ("snapshot", "id"), "eq-unspecified-001"),
        ("incomplete-page", "coverage", ("snapshot", "complete"), False),
        ("cached-authority", "authority", ("snapshot", "consistency"), "cached"),
        ("malformed-snapshot", None, ("snapshot",), "not-an-object"),
        ("malformed-records", None, ("work_orders",), {"not": "a list"}),
        ("malformed-complete", None, ("snapshot", "complete"), "true"),
    )
    for name, attribute, path, value in changes:
        receipt = deepcopy(base)
        target = receipt
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        if name == "foreign-request":
            # Use the real read for the changed target. Retaining current-task
            # positive rows under a foreign envelope would invent a conflicting
            # snapshot rather than a valid other-object candidate.
            receipt["work_orders"] = deepcopy(foreign_rows)
            attributes = [attribute, "records"] if base["work_orders"] != foreign_rows else [attribute]
        else:
            attributes = [attribute] if attribute else []
        yield name, "weaken_attribute" if attribute else "malformed_negative_control", attributes, receipt


def run(output_dir=DEFAULT_OUTPUT, source_dir=DEFAULT_FIXTURE):
    """Create a fresh offline archive and retain all candidate outcomes."""
    source, output = Path(source_dir).resolve(), Path(output_dir).resolve()
    if output == source or source in output.parents:
        raise ValueError("输出不能写入来源材料")
    source_design, source_hashes = _read_fixture(source)
    source_cases = {case["condition_id"]: case for case in source_design["cases"]}
    audits = {name: _database_audit(source / f"{name}.sqlite") for name in ("E0", "E3")}
    if audits["E0"]["actual_success"] or not audits["E3"]["actual_success"]:
        raise ValueError("正负来源业务真值不符")
    output.mkdir(parents=True, exist_ok=False)
    report = {"version": VERSION, "model_requests": 0, "model_behavior": "not_measured",
              "task": deepcopy(TASK), "candidate_attributes": list(ATTRIBUTE_GROUPS),
              "criterion": "visible_support_conditional_contract",
              "source_file_hashes": source_hashes, "conditions": [], "limitations": LIMITATIONS,
              "algorithm": {"name": "fixed_order_greedy_deletion_then_single_deletion_audit",
                            "attribute_order": list(ATTRIBUTE_GROUPS),
                            "keep_deletion_only_if": "valid candidate preserves baseline supported_outcome",
                            "local_minimum_scope": "five declared attribute groups and this explicit contract predicate",
                            "not_claimed": ["global minimum", "unique minimum", "causality", "model behavior", "all logical support paths"]}}
    for condition, polarity in (("E0", "negative"), ("E3", "positive")):
        audit = audits[condition]
        payload, base = _base_payload(source_cases[condition], audit["rows"])
        shutil.copyfile(source / f"{condition}.sqlite", output / f"{condition}.sqlite")
        baseline = contract_criterion(base)["supported_outcome"]
        candidates = []
        for name, operation, attributes, receipt in _variants(base, audit["foreign_rows"]):
            request = deepcopy(payload)
            request["messages"][9]["content"] = canonical(receipt).decode("utf-8")
            candidate_id = f"{polarity}-{name}"
            request_file = f"candidates/{candidate_id}/request.json"
            _write_json(output / request_file, request)
            criterion = contract_criterion(receipt)
            candidates.append({"candidate_id": candidate_id, "operation": operation,
                               "removed_or_weakened_attributes": attributes,
                               "request_file": request_file, "request_file_sha256": digest(output / request_file),
                               "canonical_payload_sha256": hashlib.sha256(canonical(request)).hexdigest(),
                               "contract_criterion": criterion, "model_behavior": "not_measured",
                               "model_report": None, "model_requests": 0,
                               "preserves_baseline_contract_outcome": criterion["candidate_status"] == "valid" and criterion["supported_outcome"] == baseline})
        remaining = list(ATTRIBUTE_GROUPS)
        steps = []
        current = deepcopy(base)
        # The current predicate needs all five attributes, so none is removed.
        # The generic acceptance rule is recorded rather than inferred from a
        # favorable model output or database-truth guess.
        for attribute in ATTRIBUTE_GROUPS:
            proposed = _remove(current, attribute)
            result = contract_criterion(proposed)
            accepted = result["candidate_status"] == "valid" and result["supported_outcome"] == baseline
            if accepted:
                current = proposed
                remaining.remove(attribute)
            steps.append({"attribute": attribute, "candidate_id": f"{polarity}-without-{attribute}",
                          "accepted_deletion": accepted, "contract_criterion": result,
                          "model_behavior": "not_measured"})
        witnesses = []
        for attribute in remaining:
            result = contract_criterion(_remove(current, attribute))
            witnesses.append({"deleted_attribute": attribute,
                              "candidate_id": f"{polarity}-without-{attribute}",
                              "contract_criterion": result,
                              "preserves_baseline_contract_outcome": result["candidate_status"] == "valid" and result["supported_outcome"] == baseline})
        report["conditions"].append({"source_condition_id": condition, "polarity": polarity,
                                     "database_file": f"{condition}.sqlite", "database_audit": audit,
                                     "actual_business_state": "completed" if audit["actual_success"] else "not_completed",
                                     "baseline_supported_outcome": baseline, "model_behavior": "not_measured",
                                     "candidates": candidates, "greedy_steps": steps,
                                     "local_minimum": {"attributes": remaining,
                                                       "supported_outcome": contract_criterion(current)["supported_outcome"],
                                                       "is_local_1_minimal": all(not item["preserves_baseline_contract_outcome"] for item in witnesses),
                                                       "single_deletion_witnesses": witnesses}})
    if any(digest(source / name) != expected for name, expected in source_hashes.items()):
        raise ValueError("来源材料在生成期间改变")
    candidates = [candidate for condition in report["conditions"] for candidate in condition["candidates"]]
    report["summary"] = {"candidates": len(candidates),
                         "valid": sum(c["contract_criterion"]["candidate_status"] == "valid" for c in candidates),
                         "not_testable_malformed": sum(c["contract_criterion"]["candidate_status"] == "not_testable" for c in candidates),
                         "qualified_definite": sum(c["contract_criterion"]["supported_outcome"] in ("success", "failure") for c in candidates),
                         "valid_unknown": sum(c["contract_criterion"]["supported_outcome"] == "unknown" for c in candidates),
                         "local_1_minima": sum(c["local_minimum"]["is_local_1_minimal"] for c in report["conditions"]),
                         "model_requests": 0, "model_behavior": "not_measured"}
    _write_json(output / "boundary-report.json", report)
    manifest = {path.relative_to(output).as_posix(): digest(path) for path in sorted(output.rglob("*")) if path.is_file()}
    _write_json(output / "file-manifest.json", manifest)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="离线合同证据边界删减，不调用模型")
    parser.add_argument("--input", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    result = run(args.output, args.input)
    print(json.dumps(result["summary"], ensure_ascii=False))
