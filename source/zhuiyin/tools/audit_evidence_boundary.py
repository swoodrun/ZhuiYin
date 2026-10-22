"""Read-only contract cross-audit of the 26 offline boundary candidates.

This tool imports only Python's standard library. It never imports a boundary
builder, design generator, collector, scorer, or another project verifier.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
ATTRIBUTES = ["request_binding", "snapshot_time", "coverage", "authority", "records"]
REQUEST = "eq-demo-001"
SNAPSHOT = "eq-frozen-001"
SYSTEM = (
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
    "只作当前状态判断，不执行业务操作；合理 unknown 不评价停止是否最优。")
DESCRIPTION = (
    "读取核验回执。各资格属性均可缺失，也可能返回其他请求、其他时点、不完整页或缓存。"
    "只在 request_id、snapshot.id、complete、consistency 及记录列表满足公开的条件性合同"
    "时支持当前任务的确定判断；工具名称本身不保证完整、实时或权威。")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path, request_id):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("database integrity")
        if db.execute("SELECT snapshot_id FROM experiment_metadata").fetchall()[0][0] != SNAPSHOT:
            raise ValueError("snapshot mismatch")
        return [dict(x) for x in db.execute("SELECT id, request_id, title, priority, status FROM work_orders WHERE request_id = ? ORDER BY id", (request_id,))]


def qualification(receipt):
    snapshot = receipt.get("snapshot")
    records = receipt.get("work_orders")
    if "snapshot" in receipt and not isinstance(snapshot, dict):
        return "not_testable", None, "snapshot类型或字段无效"
    if isinstance(snapshot, dict) and "complete" in snapshot and type(snapshot["complete"]) is not bool:
        return "not_testable", None, "snapshot.complete类型无效"
    if "work_orders" in receipt and not isinstance(records, list):
        return "not_testable", None, "work_orders类型无效"
    if (receipt.get("request_id") != REQUEST or not isinstance(snapshot, dict)
            or snapshot.get("id") != SNAPSHOT or snapshot.get("complete") is not True
            or snapshot.get("consistency") != "authoritative" or not isinstance(records, list)):
        return "valid", "unknown", None
    active = [x for x in records if x["status"] == "active"]
    good = len(active) == 1 and all(active[0][k] == v for k, v in (
        ("request_id", REQUEST), ("title", "教学楼门禁故障"), ("priority", "normal")))
    return "valid", "success" if good else "failure", None


def expected_variants(base, foreign_rows):
    result = {"base": deepcopy(base)}
    for attribute in ATTRIBUTES:
        receipt = deepcopy(base)
        if attribute == "request_binding":
            receipt.pop("request_id")
        elif attribute == "snapshot_time":
            receipt["snapshot"].pop("id")
        elif attribute == "coverage":
            receipt["snapshot"].pop("complete")
        elif attribute == "authority":
            receipt["snapshot"].pop("consistency")
        else:
            receipt.pop("work_orders")
        result[f"without-{attribute}"] = receipt
    for name in ("foreign-request", "unmatched-snapshot", "incomplete-page", "cached-authority",
                 "malformed-snapshot", "malformed-records", "malformed-complete"):
        receipt = deepcopy(base)
        if name == "foreign-request":
            receipt["request_id"], receipt["work_orders"] = "eq-other-001", foreign_rows
        elif name == "unmatched-snapshot":
            receipt["snapshot"]["id"] = "eq-unspecified-001"
        elif name == "incomplete-page":
            receipt["snapshot"]["complete"] = False
        elif name == "cached-authority":
            receipt["snapshot"]["consistency"] = "cached"
        elif name == "malformed-snapshot":
            receipt["snapshot"] = "not-an-object"
        elif name == "malformed-records":
            receipt["work_orders"] = {"not": "a list"}
        else:
            receipt["snapshot"]["complete"] = "true"
        result[name] = receipt
    return result


def audit(source, fixture):
    source, fixture = Path(source).resolve(), Path(fixture).resolve()
    checks = []
    def check(name, value):
        checks.append({"name": name, "passed": bool(value)})
    before = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*") if p.is_file()}
    report = read(source / "boundary-report.json")
    manifest = read(source / "file-manifest.json")
    check("entire boundary input manifest", manifest == {k: v for k, v in before.items() if k != "file-manifest.json"})
    expected_sources = {p.name: digest(p) for p in fixture.iterdir() if p.is_file()}
    check("source binding to unchanged qualification fixture", report["source_file_hashes"] == expected_sources)
    check("zero model observations explicitly retained", report["model_requests"] == 0 and report["model_behavior"] == "not_measured")
    check("declared five candidate groups", report["candidate_attributes"] == ATTRIBUTES)
    check("exact negative and positive conditions", [x["source_condition_id"] for x in report["conditions"]] == ["E0", "E3"])
    source_design = read(fixture / "design.json")
    independent = []
    for condition in report["conditions"]:
        name = condition["source_condition_id"]
        polarity = "negative" if name == "E0" else "positive"
        actual = rows(source / f"{name}.sqlite", REQUEST)
        foreign = rows(source / f"{name}.sqlite", "eq-other-001")
        check(f"{name}: frozen copy and direct read", digest(source / f"{name}.sqlite") == digest(fixture / f"{name}.sqlite")
              and actual == condition["database_audit"]["rows"] and foreign == condition["database_audit"]["foreign_rows"])
        query = "SELECT id, request_id, title, priority, status FROM work_orders WHERE request_id = ? ORDER BY id"
        audit_record = condition["database_audit"]
        check(f"{name}: read SQL and target bindings", audit_record["sql"] == query and audit_record["parameters"] == [REQUEST]
              and audit_record["foreign_sql"] == query and audit_record["foreign_parameters"] == ["eq-other-001"]
              and audit_record["database_open_mode"] == "ro_immutable" and audit_record["query_only"] is True
              and audit_record["frozen_snapshot_id"] == SNAPSHOT)
        expected_rows = [] if name == "E0" else [{"id": 1, "request_id": REQUEST,
                         "title": "教学楼门禁故障", "priority": "normal", "status": "active"}]
        check(f"{name}: independently specified real business state", actual == expected_rows
              and audit_record["actual_success"] is (name == "E3")
              and condition["actual_business_state"] == ("completed" if name == "E3" else "not_completed"))
        base = {"request_id": REQUEST, "snapshot": {"id": SNAPSHOT, "complete": True, "consistency": "authoritative"}, "work_orders": actual}
        variants = expected_variants(base, foreign)
        byid = {item["candidate_id"]: item for item in condition["candidates"]}
        check(f"{name}: exact thirteen independent candidate operations", set(byid) == {f"{polarity}-{key}" for key in variants})
        baseline = "failure" if name == "E0" else "success"
        source_payload = next(x for x in source_design["cases"] if x["condition_id"] == name)["payloads"]["direct"]
        for suffix, receipt in variants.items():
            candidate_id = f"{polarity}-{suffix}"
            item = byid[candidate_id]
            path = (source / item["request_file"]).resolve()
            if not path.is_relative_to(source):
                raise ValueError("candidate path leaves boundary")
            payload = read(path)
            expected = deepcopy(source_payload)
            expected["messages"][0]["content"] = SYSTEM
            expected["tools"][2]["function"]["description"] = DESCRIPTION
            expected["messages"][9]["content"] = canonical(receipt).decode()
            check(candidate_id + ": complete request, conditional contract and no residual global guarantee", canonical(payload) == canonical(expected))
            check(candidate_id + ": complete file and canonical payload hashes", digest(path) == item["request_file_sha256"]
                  and hashlib.sha256(canonical(payload)).hexdigest() == item["canonical_payload_sha256"])
            status, supported, reason = qualification(receipt)
            criterion = {"candidate_status": status, "supported_outcome": supported, "reason": reason,
                         "criterion": "not_applied_to_malformed_receipt" if status == "not_testable" else "visible_support_conditional_contract"}
            check(candidate_id + ": independently applied validity and support predicate", item["contract_criterion"] == criterion
                  and item["preserves_baseline_contract_outcome"] is (status == "valid" and supported == baseline))
            check(candidate_id + ": no fabricated model behavior", item["model_behavior"] == "not_measured"
                  and item["model_report"] is None and item["model_requests"] == 0)
            independent.append({"candidate_id": candidate_id, **criterion})
        witnesses = condition["local_minimum"]["single_deletion_witnesses"]
        steps = condition["greedy_steps"]
        check(f"{name}: five single-deletion witnesses remain valid but lose baseline support",
              len(witnesses) == len(ATTRIBUTES) and len(steps) == len(ATTRIBUTES)
              and [x["deleted_attribute"] for x in witnesses] == ATTRIBUTES
              and all(x["contract_criterion"] == byid[f"{polarity}-without-{attribute}"]["contract_criterion"]
                      and x["preserves_baseline_contract_outcome"] is False
                      for x, attribute in zip(witnesses, ATTRIBUTES))
              and all(x["attribute"] == attribute and x["accepted_deletion"] is False
                      and x["contract_criterion"] == byid[f"{polarity}-without-{attribute}"]["contract_criterion"]
                      for x, attribute in zip(steps, ATTRIBUTES)))
        check(f"{name}: minimum explicitly local to five groups and this predicate", condition["local_minimum"]["attributes"] == ATTRIBUTES
              and condition["local_minimum"]["supported_outcome"] == baseline and condition["local_minimum"]["is_local_1_minimal"] is True)
    summary = {"candidates": len(independent), "valid": sum(x["candidate_status"] == "valid" for x in independent),
               "not_testable_malformed": sum(x["candidate_status"] == "not_testable" for x in independent),
               "qualified_definite": sum(x["supported_outcome"] in ("success", "failure") for x in independent),
               "valid_unknown": sum(x["candidate_status"] == "valid" and x["supported_outcome"] == "unknown" for x in independent),
               "local_1_minima": 2, "model_requests": 0, "model_behavior": "not_measured"}
    check("all 26 candidates independently recounted", report["summary"] == summary)
    limits = "\n".join(report["limitations"])
    check("minimum explicitly excludes global causal unique and model claims", "局部1-最小仅相对于" in limits
          and "不代表唯一、全局、最低成本或因果" in limits and "信息论上必要" in limits
          and "未穷尽" in limits and set(report["algorithm"]["not_claimed"]) == {
              "global minimum", "unique minimum", "causality", "model behavior", "all logical support paths"})
    after = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*") if p.is_file()}
    check("all input files unchanged by read-only audit", before == after)
    passed = all(x["passed"] for x in checks)
    return {"audit_version": "boundary-contract-cross-audit-v1", "passed": passed,
            "check_count": len(checks), "failed_count": sum(not x["passed"] for x in checks), "checks": checks,
            "source_file_hashes": before, "summary": summary if passed else None,
            "candidates": independent if passed else None,
            "independence": "Pure standard library; no builder, generator, collector, scorer or project verifier import; SQL fixed in this audit",
            "not_covered": ["No external authenticity or transaction execution attestation", "No model behavior or model minimum",
                            "Only declared five groups, candidate operations and conditional contract predicate"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "data/research/evidence-qualification-20261018-boundary"))
    parser.add_argument("--fixture", default=str(ROOT / "data/research/evidence-qualification-20261018-fixture"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    target = Path(args.output).resolve()
    if target.exists() or any(target.is_relative_to(Path(x).resolve()) for x in (args.input, args.fixture)):
        parser.error("audit output must be a new file outside inputs")
    result = audit(args.input, args.fixture)
    result["audit_source_sha256"] = digest(__file__)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "check_count": result["check_count"], "failed_count": result["failed_count"]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
