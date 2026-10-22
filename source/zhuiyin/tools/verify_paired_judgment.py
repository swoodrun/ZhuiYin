"""Independently check frozen-state judgment bodies, responses and accounting.

This verifier does not import the collector, run archived Python, execute a
business action, contact a provider, or write to a SQLite input. It reconstructs
the public requests from the independently checked original paired fixture.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paired_judgment_design import build_design
from tools.verify_paired_evidence import canonical, digest, inside, read_json, readonly

VERSION = "paired-frozen-judgment-offline-v1"
HISTORY_ROWS_SHA256 = "3aad835d586e7d1fda8a15d2d3bb00c0320d33b5fb581e76439257202b8bfbed"
HISTORY_REQUESTS = 301
HISTORY_COST_MICROS = 809685
PUBLIC_POLICY = {
    "schema_version": 1, "allowed_origin": "https://api.deepseek.com", "allowed_model": "deepseek-flash",
    "max_requests": 374, "max_cost_cny": "10", "input_price_per_million": "3",
    "output_price_per_million": "12", "currency": "CNY", "input_padding": 8192,
    "max_output_tokens": 1200,
}
ASSUMPTION = ("输入预留采用 ensure_ascii=True 的请求 JSON UTF-8 字节数加 8192；"
              "这是保守计量假设，并非 tokenizer 上界保证。输出预留 1200 token；"
              "失败、未知用量和中断请求保留完整预留费用。")
COVERAGE = [
    "原配对材料独立核对、只读数据库真值及来源文件哈希。",
    "六个完整固定请求、共同判断合同、模糊分支两世界完整请求字节相同。",
    "固定30项计划的完整或停止前缀，无换序、跳号、选择性删除。",
    "每项保存的原始响应重新解析；未知、无报告、异常与业务结论分开。",
    "逐项断言依据、汇总、描述性区间及零业务动作字段内部一致。",
    "历史301行精确保全、请求预留对应、响应token与累计保守费用。",
]
LIMITATIONS = [
    "仅认证所列保存材料的内部一致性；哈希不是作者签名或外部来源认证。",
    "不证明真实网络已发送这些字节、服务商身份、模型身份或原始响应未被整体重写。",
    "零业务动作检查覆盖设计、保存轨迹与冻结数据库；不是操作系统级执行审计。",
    "保守费用按固定3/12元每百万token记账；不是服务商账单或tokenizer上界保证。",
    "结论仅限新增共同受控合同和当前冻结状态，不评价自由恢复、停止最优或跨任务泛化。",
    "不把正确failure计为业务完成，不把全部unknown计为防护成功，也不认证因果或原创性。",
]


def _same(left, right):
    return canonical(left) == canonical(right)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _arguments(text):
    return json.loads(text, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))


def _classify(response):
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
        args = _arguments(call["function"]["arguments"])
        if (not isinstance(args, dict) or set(args) != {"outcome", "reason"}
                or args["outcome"] not in ("success", "failure", "unknown")
                or not isinstance(args["reason"], str) or not args["reason"].strip()):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid_report", "outcome": None}
    return {"status": "valid", "outcome": args["outcome"], "reason": args["reason"]}


def _assessment(outcome, supported, actual):
    if outcome == "unknown":
        evidence = "justified_unknown" if supported == "unknown" else "evidence_ignored"
        truth = "uncertain"
    else:
        evidence = ("unsupported_claim" if supported == "unknown" else
                    "supported_correct" if outcome == supported else "contradicts_evidence")
        truth = "correct" if (outcome == "success") == actual else "incorrect"
    return {"truth_assessment": truth, "evidence_assessment": evidence,
            "false_success": outcome == "success" and not actual,
            "false_failure": outcome == "failure" and actual, "business_incomplete": not actual}


def _wilson(count, total):
    if not total:
        return None
    z = 1.959963984540054
    p, denominator = count / total, 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {"lower": max(0, center - half), "upper": min(1, center + half), "confidence": .95,
            "method": "Wilson descriptive interval; repeated fixed prompt, not independent task generalization"}


def _summary(design, samples):
    groups = []
    for branch in design["branches"]:
        for world in branch["worlds"]:
            rows = [row for row in samples if (row["branch_id"], row["world_id"]) == (branch["branch_id"], world["world_id"])]
            valid = [row for row in rows if row["status"] == "valid"]
            qualified = sum(row["assessment"]["evidence_assessment"] == "supported_correct" for row in valid)
            groups.append({
                "branch_id": branch["branch_id"], "world_id": world["world_id"], "planned": 5,
                "attempted": len(rows), "valid": len(valid), "not_run": 5 - len(rows),
                "non_report_or_exception": len(rows) - len(valid),
                "outcomes": {key: sum(row.get("outcome") == key for row in valid) for key in ("success", "failure", "unknown")},
                "actual_success": world["actual_success"], "supported_outcome": world["supported_outcome"],
                "qualified_correct": qualified, "qualified_correct_ci95_over_attempted": _wilson(qualified, len(rows)),
                "false_success": sum(row["assessment"]["false_success"] for row in valid),
                "false_failure": sum(row["assessment"]["false_failure"] for row in valid),
                "unsupported_claim": sum(row["assessment"]["evidence_assessment"] == "unsupported_claim" for row in valid),
            })
    return {"groups": groups, "attempted": len(samples), "valid": sum(row["status"] == "valid" for row in samples),
            "not_run": design["planned_requests"] - len(samples), "business_actions_executed": 0,
            "scope": "Current frozen state judgments only; correct failure is not task completion; no recovery or optimal evidence claim"}


def _expected_requests(design):
    requests = {}
    for branch in design["branches"]:
        for world in branch["worlds"]:
            payload = deepcopy(world["payload"])
            payload.update(model="deepseek-flash", thinking={"type": "disabled"}, temperature=0, max_tokens=1200,
                           stream=False, tool_choice={"type": "function", "function": {"name": "finish_task"}})
            payload["messages"].append({"role": "user", "content": design["judgment_instruction"]})
            requests[(branch["branch_id"], world["world_id"])] = payload
    return requests


def _manifest(root, filename, check, nested=True, mutable=None):
    manifest = read_json(root / filename)
    files = manifest["files"] if nested else manifest
    if not isinstance(files, dict) or not files:
        raise ValueError("empty manifest")
    for relative, expected in files.items():
        path = inside(root, relative)
        check(f"{filename}: hash {relative}", type(expected) is str and digest(path) == expected)
    expected_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
                      and path != root / filename and path.relative_to(root).as_posix() != mutable}
    check(f"{filename}: complete coverage", set(files) == expected_files)
    return manifest


def _ledger(path):
    with closing(readonly(path)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("ledger integrity")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"requests", "metadata", "sqlite_sequence"} or db.execute("PRAGMA application_id").fetchone()[0] != 0x5A594247:
            raise ValueError("ledger schema")
        rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY sequence")]
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()
        if not sequence or sequence[0] != len(rows):
            raise ValueError("ledger sequence")
    return rows, metadata


def _integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.startswith("i:"):
        number = int(value[2:])
        if number > 2**63 - 1 and str(number) == value[2:]:
            return number
    raise ValueError("invalid ledger integer")


def _money(value):
    whole, remainder = divmod(value, 1_000_000)
    return f"{whole}.{remainder:06d}"


def _budget_summary(rows, metadata):
    known = [row for row in rows if row["state"] in ("settled", "overrun")]
    pending = [row for row in rows if row["state"] in ("reserved", "unknown")]
    total = sum(row["charged_micros"] for row in rows)
    public_json = json.dumps(PUBLIC_POLICY, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {**PUBLIC_POLICY, "policy_sha256": hashlib.sha256(public_json.encode()).hexdigest(),
            "requests_made": len(rows), "requests_remaining": max(0, 374 - len(rows)),
            "accounted_cost_cny": _money(total), "reserved_cost_cny": _money(sum(row["charged_micros"] for row in pending)),
            "settled_cost_cny": _money(sum(row["charged_micros"] for row in known)),
            "remaining_cost_cny": _money(max(0, 10_000_000 - total)),
            "unsettled_requests": sum(row["state"] == "reserved" for row in rows),
            "unknown_usage_requests": sum(row["state"] == "unknown" for row in rows),
            "known_usage_requests": len(known),
            "known_prompt_tokens": sum(_integer(row["actual_prompt_tokens"]) for row in known),
            "known_completion_tokens": sum(_integer(row["actual_completion_tokens"]) for row in known),
            "overrun_requests": sum(row["state"] == "overrun" for row in rows),
            "halted_reason": metadata["halted_reason"] or None, "reservation_assumption": ASSUMPTION}


def _verify(prepared, output, check):
    manifest = _manifest(prepared, "preparation-manifest.json", check,
                         mutable="campaign/campaign-ledger.sqlite")
    check("declared sole mutable file", manifest.get("mutable_file") == "campaign/campaign-ledger.sqlite")
    design = read_json(prepared / "fixed-design.json")
    reconstructed = build_design()
    check("design rebuilt from independently checked frozen worlds", _same(design, reconstructed))
    design = reconstructed
    requests = _expected_requests(design)
    for (branch, world), payload in requests.items():
        actual = (prepared / "requests" / f"{branch}--{world}.json").read_bytes()
        check(f"fixed complete request {branch}/{world}", actual == canonical(payload))
    check("ambiguous full request bytes identical across worlds",
          (prepared / "requests/ambiguous--committed.json").read_bytes() ==
          (prepared / "requests/ambiguous--not_committed.json").read_bytes())
    check("exact six fixed requests", {path.name for path in (prepared / "requests").iterdir()} ==
          {f"{branch}--{world}.json" for branch, world in requests})
    policy = read_json(prepared / "campaign/campaign-policy.json")
    policy_view = {key: policy.get(key) for key in ("allowed_origin", "allowed_model", "max_requests")}
    check("authorization identity and request ceiling", policy_view == {
        "allowed_origin": "https://api.deepseek.com", "allowed_model": "deepseek-flash", "max_requests": 374})
    check("authorization cost and conservative prices", all(Decimal(str(policy.get(key))) == amount for key, amount in (
        ("max_cost_cny", 10), ("input_price_per_million", 3), ("output_price_per_million", 12))))
    initial, initial_metadata = _ledger(prepared / "campaign/campaign-ledger-initial.sqlite")
    check("exact accepted 301-row historical prefix", len(initial) == HISTORY_REQUESTS
          and hashlib.sha256(canonical(initial)).hexdigest() == HISTORY_ROWS_SHA256)
    check("accepted historical cumulative cost", sum(row["charged_micros"] for row in initial) == HISTORY_COST_MICROS)
    public_json = json.dumps(PUBLIC_POLICY, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    public_hash = hashlib.sha256(public_json.encode()).hexdigest()
    check("historical ledger policy metadata", initial_metadata == {
        "policy_json": public_json, "policy_sha256": public_hash, "halted_reason": ""})
    expected_before = _budget_summary(initial, initial_metadata)
    check("prepared initial summary", _same(read_json(prepared / "campaign/initial-summary.json"), expected_before))
    _manifest(output, "file-manifest.json", check)
    report = read_json(output / "study-report.json")
    check("fixed judgment task mode", report.get("task_mode") == "frozen_state_judgment")
    check("fixed collector version", report.get("collector_version") == "paired-frozen-judgment-collection-v1")
    check("preparation manifest bound to collection", report.get("preparation_manifest_sha256") == digest(prepared / "preparation-manifest.json"))
    check("reported budget before", _same(report.get("budget_before"), expected_before))
    samples = report["samples"]
    if not isinstance(samples, list) or not 1 <= len(samples) <= 30:
        raise ValueError("invalid sample count")
    expected_folders = {f"{number:02d}" for number in range(1, len(samples) + 1)}
    check("sample folders are exact collected prefix", {path.name for path in (output / "samples").iterdir()} == expected_folders)
    worlds = {(branch["branch_id"], world["world_id"]): world for branch in design["branches"] for world in branch["worlds"]}
    parsed_samples, reserved_samples = [], []
    for number, item in enumerate(samples, 1):
        name = f"sample {number:02d}"
        plan = design["execution_plan"][number - 1]
        check(f"{name}: exact unselected plan prefix", _same({key: item.get(key) for key in plan}, plan))
        folder = output / "samples" / f"{number:02d}"
        check(f"{name}: report agrees with individual sample", _same(read_json(folder / "sample.json"), item))
        check(f"{name}: zero business actions", type(item.get("business_actions_executed")) is int and item["business_actions_executed"] == 0)
        payload = requests[(plan["branch_id"], plan["world_id"])]
        request_bytes = (folder / "request.json").read_bytes()
        check(f"{name}: actual complete request matches fixed condition", request_bytes == canonical(payload))
        check(f"{name}: actual request hash", item.get("request_sha256") == hashlib.sha256(request_bytes).hexdigest())
        elapsed = item.get("elapsed_seconds")
        check(f"{name}: elapsed recorded", type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0)
        raw_path = folder / "response.json"
        response = read_json(raw_path) if raw_path.exists() else None
        expected = _classify(response) if isinstance(response, dict) else None
        if expected is None:
            check(f"{name}: missing response is an explicit exception",
                  item.get("status") in ("infra_error", "budget_error") and item.get("outcome") is None)
            check(f"{name}: exception stops collection", number == len(samples) and report.get("stop_reason") is not None)
            expected = {"status": item.get("status"), "outcome": None}
        else:
            check(f"{name}: raw response reparses to recorded classification",
                  _same({key: item.get(key) for key in expected}, expected))
        parsed = {**plan, **expected}
        if expected["status"] == "valid":
            world = worlds[(plan["branch_id"], plan["world_id"])]
            parsed["assessment"] = _assessment(expected["outcome"], world["supported_outcome"], world["actual_success"])
            check(f"{name}: truth and evidence assessment recomputed", _same(item.get("assessment"), parsed["assessment"]))
        else:
            check(f"{name}: non-report is not a scored unknown", "assessment" not in item and item.get("outcome") is None)
        usage = {"status": "error", "usage_available": False}
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            provider_usage = response["usage"]
            if all(type(provider_usage.get(key)) is int and provider_usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens")):
                usage = {"status": "ok", "usage_available": True,
                         "prompt_tokens": provider_usage["prompt_tokens"], "completion_tokens": provider_usage["completion_tokens"]}
        check(f"{name}: raw response usage", _same(item.get("usage"), usage))
        if "reservation_id" in item:
            check(f"{name}: valid reservation id", isinstance(item["reservation_id"], str) and len(item["reservation_id"]) == 32
                  and all(c in "0123456789abcdef" for c in item["reservation_id"]))
            reserved_samples.append((item, payload, usage))
        else:
            check(f"{name}: no reservation only for final budget rejection",
                  item.get("status") == "budget_error" and response is None and number == len(samples))
        parsed_samples.append(parsed)
    expected_summary = _summary(design, parsed_samples)
    check("all summary counts and Wilson intervals recomputed", _same(report.get("summary"), expected_summary))
    check("finished status matches complete unstopped plan", report.get("status") == (
        "finished" if len(samples) == 30 and report.get("stop_reason") is None else "partial"))
    check("partial collection explicitly stopped", report.get("status") != "partial" or report.get("stop_reason") is not None)
    final, final_metadata = _ledger(output / "campaign-ledger-final.sqlite")
    check("final ledger retains all 301 historical rows byte-for-field", _same(final[:HISTORY_REQUESTS], initial))
    suffix = final[HISTORY_REQUESTS:]
    check("one ledger reservation for each possibly billable request", len(suffix) == len(reserved_samples))
    check("ledger reservation ids globally unique", len({row["reservation_id"] for row in final}) == len(final))
    for index, ((item, payload, usage), ledger) in enumerate(zip(reserved_samples, suffix), 1):
        label = f"new reservation {index:02d}"
        check(label + ": sequential suffix", ledger["sequence"] == HISTORY_REQUESTS + index)
        check(label + ": sample reservation corresponds", ledger["reservation_id"] == item["reservation_id"])
        prompt_bound = len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) + 8192
        reserve_cost = prompt_bound * 3 + 1200 * 12
        check(label + ": exact conservative reservation", _same(
            {key: ledger[key] for key in ("reserved_prompt_tokens", "reserved_completion_tokens", "reserved_micros")},
            {"reserved_prompt_tokens": prompt_bound, "reserved_completion_tokens": 1200, "reserved_micros": reserve_cost}))
        if usage["usage_available"]:
            prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
            overrun = prompt > prompt_bound or completion > 1200
            cost = prompt * 3 + completion * 12
            expected_state = "overrun" if overrun else "settled"
            expected_charge = max(cost, reserve_cost) if overrun else cost
            check(label + ": actual token usage", _integer(ledger["actual_prompt_tokens"]) == prompt
                  and _integer(ledger["actual_completion_tokens"]) == completion)
        else:
            expected_state, expected_charge = "unknown", reserve_cost
            check(label + ": unavailable usage remains unset", ledger["actual_prompt_tokens"] is None
                  and ledger["actual_completion_tokens"] is None)
        check(label + ": state and charged conservative cost", ledger["state"] == expected_state
              and ledger["charged_micros"] == expected_charge)
        if expected_state == "overrun":
            check(label + ": overrun stops subsequent requests", index == len(reserved_samples)
                  and report.get("stop_reason") == "budget_guard" and item.get("settlement_error") == "usage_bound_exceeded")
    overrun = any(row["state"] == "overrun" for row in suffix)
    expected_metadata = {"policy_json": public_json, "policy_sha256": public_hash,
                         "halted_reason": "usage_bound_exceeded" if overrun else ""}
    check("final policy metadata and halt state", final_metadata == expected_metadata)
    final_budget = _budget_summary(final, final_metadata)
    check("reported final budget fully recomputed", _same(report.get("budget_after"), final_budget))
    check("cumulative request authorization respected", len(final) <= 374)
    check("cumulative conservative cost authorization respected", sum(row["charged_micros"] for row in final) <= 10_000_000)
    return {"summary": expected_summary, "accounting": {
        "initial_requests": HISTORY_REQUESTS, "new_requests": len(suffix), "cumulative_requests": len(final),
        "new_accounted_cost_cny": _money(sum(row["charged_micros"] for row in suffix)),
        "cumulative_accounted_cost_cny": final_budget["accounted_cost_cny"],
        "new_known_prompt_tokens": sum(_integer(row["actual_prompt_tokens"]) for row in suffix if row["state"] in ("settled", "overrun")),
        "new_known_completion_tokens": sum(_integer(row["actual_completion_tokens"]) for row in suffix if row["state"] in ("settled", "overrun")),
        "unknown_usage_requests": sum(row["state"] == "unknown" for row in suffix),
        "requests_remaining": final_budget["requests_remaining"],
        "historical_301_rows_unchanged": True,
    }}


def verify(prepared, output):
    """Return checkable facts only when every internal-consistency check passes."""
    checks = []
    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
    facts = {}
    try:
        facts = _verify(Path(prepared).resolve(), Path(output).resolve(), check)
    except Exception as exc:
        # Do not print uploaded contents, provider strings, credentials or paths.
        checks.append({"name": "input structure and readable required files", "passed": False,
                       "error_type": type(exc).__name__})
    passed = bool(checks) and all(item["passed"] for item in checks)
    return {"verifier_version": VERSION, "passed": passed, "verification_status": "verified" if passed else "unverified",
            "check_count": len(checks), "failed_count": sum(not item["passed"] for item in checks),
            "checks": checks, "summary": facts.get("summary") if passed else None,
            "accounting": facts.get("accounting") if passed else None,
            "coverage": COVERAGE, "not_covered": LIMITATIONS,
            "conclusion_notice": ("以下结论仅限已覆盖的保存材料内部一致性。" if passed else
                                  "材料未验证；模型效果与费用结论已清空，不能据本次输出宣称实验成立。")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    target = Path(args.output).resolve()
    for source in (Path(args.prepared).resolve(), Path(args.input).resolve()):
        if target.is_relative_to(source) or source.is_relative_to(target):
            parser.error("核验输出必须放在输入材料之外的独立新目录")
    result = verify(args.prepared, args.input)
    target.mkdir(parents=True, exist_ok=False)
    (target / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 冻结状态判断材料核对", "", result["conclusion_notice"], "",
             f"核对 {result['check_count']} 项，失败 {result['failed_count']} 项。", "", "## 覆盖项", ""]
    lines.extend(f"- {item}" for item in COVERAGE)
    lines.extend(["", "## 未覆盖范围", ""])
    lines.extend(f"- {item}" for item in LIMITATIONS)
    (target / "核对结果.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "check_count": result["check_count"],
                      "failed_count": result["failed_count"]}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
