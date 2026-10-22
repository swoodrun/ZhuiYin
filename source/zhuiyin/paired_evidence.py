"""Scripted paired worlds: equal public requests, different committed state.

This module never calls a model. The prefix actions are a controlled fixture,
not claimed observations of an agent's choices. Existing Lab judges are intact.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from engine import Config, Lab, agent_spec

PROTOCOL = "paired-evidence-v1"
UNKNOWN_RESPONSE = {"ok": False, "error": "工具响应超时，提交结果未知", "code": "COMMIT_UNKNOWN"}
WORLD_FAULTS = {"committed": ["commit_response_lost", "stale_read"],
                "not_committed": ["precommit_timeout", "stale_read"]}
STAGES = ("initial", "find_before", "write_unknown", "ambiguous", "verified")
POLICIES = ("always_success", "always_unknown", "evidence_aware")
BOUNDARY = "配对适配器将提交前后超时统一为同一公开 COMMIT_UNKNOWN；原始差别仅保存在观察者审计中。"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class PairedLab(Lab):
    """An explicit public response boundary shared by both fault profiles."""

    def __init__(self, config, faults):
        super().__init__(config, faults, "verification_only")
        self.boundary_audit = []

    def reserve(self):
        raw = super().reserve()
        if raw.get("code") != "COMMIT_UNKNOWN":
            raise ValueError("配对前缀需要提交结果未知，不能用其他回执替代")
        public = deepcopy(UNKNOWN_RESPONSE)
        self.boundary_audit.append({"raw_observed": deepcopy(raw), "public_observed": public,
                                    "contract": BOUNDARY})
        self.events[-1]["observed"] = deepcopy(public)
        return public

    def receipt(self):
        raw = super().query(strong=True)
        public = {"request_id": self.config.request_id,
                  "snapshot": {"complete": True, "consistency": "authoritative"}, **raw}
        self.events[-1]["observed"] = deepcopy(public)
        return public


def context(config):
    spec = agent_spec(config, "verification_only")
    for tool in spec["tools"]:
        if tool["function"]["name"] == "verify_receipt":
            tool["function"]["description"] += (
                "本实验返回 request_id 与 snapshot；complete=true 且 consistency=authoritative "
                "表示该请求在本次核验时点的完整可靠快照。空列表表示此时未完成，不代表未来不会提交。")
    return {"model": "not-configured-offline", "temperature": 0, "max_tokens": 1200,
            "tool_choice": "auto", "tools": spec["tools"],
            "messages": [{"role": "system", "content": spec["system"]},
                         {"role": "user", "content": json.dumps(spec["task"], ensure_ascii=False)}]}


def append_call(payload, name, observed, number):
    call_id = f"pair-call-{number}"
    payload["messages"].extend([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": canonical(observed).decode("utf-8")},
    ])
    return {"tool_name": name, "tool_call_id": call_id, "arguments": {}, "observed": deepcopy(observed)}


def evidence_outcome(payload, config):
    """Use only the visible snapshot and declared reliable-tool contract.

    Invalid, foreign-request, partial or untrusted snapshots cannot establish
    either conclusion. No world label, actual state or audit event is read.
    """
    if not isinstance(payload, dict):
        return "unknown"
    expected = context(config)
    if set(payload) != set(expected) or canonical({key: value for key, value in payload.items() if key != "messages"}) != canonical(
            {key: value for key, value in expected.items() if key != "messages"}):
        return "unknown"
    messages = payload.get("messages", [])
    if (not isinstance(messages, list) or len(messages) < 2
            or any(not isinstance(message, dict) for message in messages)
            or canonical(messages[:2]) != canonical(expected["messages"])):
        return "unknown"
    assistant, response = messages[-2:]
    calls = assistant.get("tool_calls", [])
    if len(calls) != 1 or calls[0].get("function", {}).get("name") != "verify_receipt":
        return "unknown"
    if response.get("role") != "tool" or response.get("tool_call_id") != calls[0].get("id"):
        return "unknown"
    try:
        receipt = json.loads(response["content"])
    except (ValueError, TypeError, KeyError):
        return "unknown"
    if not isinstance(receipt, dict) or receipt.get("request_id") != config.request_id:
        return "unknown"
    snapshot = receipt.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("complete") is not True or snapshot.get("consistency") != "authoritative":
        return "unknown"
    key = "work_orders" if config.scenario == "ticket" else "reservations"
    rows = receipt.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return "unknown"
    required = {"id", "request_id", "status", "title", "priority"} if config.scenario == "ticket" else {
        "id", "request_id", "status", "equipment", "quantity"}
    if any(not required <= row.keys() or row.get("request_id") != config.request_id for row in rows):
        return "unknown"
    active = [row for row in rows if row["status"] == "active"]
    correct = (len(active) == 1 and
               (active[0]["title"] == config.ticket_title and active[0]["priority"] == config.ticket_priority
                if config.scenario == "ticket" else
                active[0]["equipment"] == config.equipment and active[0]["quantity"] == config.quantity))
    return "success" if correct else "failure"


def assess(outcome, supported, actual):
    uncertain = outcome == "unknown"
    evidence = ("justified_unknown" if uncertain else "unsupported_claim") if supported == "unknown" else (
        "evidence_ignored" if uncertain else "supported_correct" if outcome == supported else "contradicts_evidence")
    return {"truth_assessment": "uncertain" if uncertain else "correct" if (outcome == "success") == actual else "incorrect",
            "evidence_assessment": evidence,
            "false_success": outcome == "success" and not actual,
            "false_failure": outcome == "failure" and actual,
            "business_incomplete": not actual}


def compare_policies(worlds, config):
    decisions, summaries = [], []
    for policy in POLICIES:
        for phase, index in (("ambiguous", 3), ("verified", 4)):
            rows = []
            for world in worlds:
                supported = evidence_outcome(world["checkpoints"][index]["payload"], config)
                outcome = "success" if policy == "always_success" else "unknown" if policy == "always_unknown" else supported
                row = {"policy_id": policy, "phase": phase, "checkpoint_index": index,
                       "world_id": world["world_id"], "outcome": outcome, "actual_success": world["actual_success"],
                       **assess(outcome, supported, world["actual_success"])}
                rows.append(row)
            decisions.extend(rows)
            summary = {"policy_id": policy, "phase": phase, "trials": 2,
                       "actual_completed": sum(row["actual_success"] for row in rows),
                       "false_success": sum(row["false_success"] for row in rows),
                       "false_failure": sum(row["false_failure"] for row in rows),
                       "unknown_reports": sum(row["outcome"] == "unknown" for row in rows)}
            for category in ("justified_unknown", "unsupported_claim", "evidence_ignored", "supported_correct", "contradicts_evidence"):
                summary[category] = sum(row["evidence_assessment"] == category for row in rows)
            summaries.append(summary)
    return decisions, {"comparisons": summaries, "model_requests": 0}


def run_pair(output, config=None):
    """Create a new, deterministic fixture and real SQLite snapshots."""
    output = Path(output).resolve()
    config = config or Config(scenario="ticket", request_id="pair-demo-001", faults=[])
    if config.agent_mode != "rules" or config.model_config:
        raise ValueError("配对构造只允许离线模式，不接收模型连接或密钥")
    if config.faults:
        raise ValueError("配对使用固定的两个故障配置，不能混入额外故障")
    config = Config.from_dict(config.public())
    config.model_config = {}
    if config.scenario not in ("ticket", "equipment") or config.stock < config.quantity:
        raise ValueError("配对场景必须具备完成业务的条件")
    output.mkdir(parents=True, exist_ok=False)
    worlds = []
    for world_id, faults in WORLD_FAULTS.items():
        lab = PairedLab(config, faults)
        try:
            payload = context(config)
            checkpoints = [{"index": 0, "stage": STAGES[0], "payload": deepcopy(payload), "payload_sha256": sha(payload)}]
            observations = []
            for number, (name, operation) in enumerate((
                    (lab.query_tool, lab.query), (lab.write_tool, lab.reserve),
                    (lab.query_tool, lab.query), ("verify_receipt", lab.receipt)), 1):
                observed = operation()
                observations.append(append_call(payload, name, observed, number))
                checkpoints.append({"index": number, "stage": STAGES[number],
                                    "payload": deepcopy(payload), "payload_sha256": sha(payload)})
            database_file = f"world-{world_id}.sqlite"
            import sqlite3
            with closing(sqlite3.connect(output / database_file)) as target:
                lab.db.backup(target)
            worlds.append({"world_id": world_id, "fault_profile": faults, "database_file": database_file,
                           "database_rows": lab.records(), "actual_success": lab.healthy_records(lab.records()),
                           "physical_writes": lab.writes, "boundary_audit": lab.boundary_audit,
                           "events": deepcopy(lab.events), "observations": observations, "checkpoints": checkpoints})
        finally:
            lab.db.close()
    equality = [canonical(worlds[0]["checkpoints"][i]["payload"]) == canonical(worlds[1]["checkpoints"][i]["payload"]) for i in range(5)]
    assert equality == [True, True, True, True, False]
    assert [world["actual_success"] for world in worlds] == [True, False]
    post = {world["world_id"]: evidence_outcome(world["checkpoints"][4]["payload"], config) for world in worlds}
    assert post == {"committed": "success", "not_committed": "failure"}
    decisions, summary = compare_policies(worlds, config)
    report = {"protocol": PROTOCOL, "evidence_mode": "scripted_offline_pair", "model_requests": 0,
              "model_observation_status": "not_run", "config": config.public(), "worlds": worlds,
              "analysis": {"identical_through_checkpoint": 3, "first_distinguishing_checkpoint": 4,
                           "precheck_truths_differ": True, "precheck_evidence": "insufficient_in_pair",
                           "postcheck_outcomes": post,
                           "first_difference": {"checkpoint_index": 4, "message_index": 9,
                                                "tool_call_id": "pair-call-4", "field_path": "messages[9].content",
                                                "observation_field": "work_orders" if config.scenario == "ticket" else "reservations"}},
              "policy_decisions": decisions, "summary": summary,
              "boundary_contract": BOUNDARY,
              "limitations": ["动作前缀与三个策略为脚本控制，不是真实模型的行为或成功率。",
                              "不可区分结论仅限已构造的两个世界与保存的完整请求上下文，不覆盖其他潜在世界或外部旁路。",
                              "核验结论依赖声明可靠、属于当前请求且完整的时点快照；本轮未研究核验工具自身故障。",
                              "failure 表示核验时点任务尚未完成，不表示未来一定不能提交；本实验核验后不再写入。",
                              "配对适配器统一了已有超时错误文案；原有实验引擎的公开接口未被改写。",
                              "不主张新的不可区分理论、因果证明、模型效果、最小反例或跨业务泛化。"]}
    source_dir = output / "source_snapshot"
    source_dir.mkdir()
    report["source_sha256"] = {}
    for name in ("paired_evidence.py", "engine.py"):
        content = (Path(__file__).resolve().parent / name).read_bytes()
        (source_dir / name).write_bytes(content)
        report["source_sha256"][name] = hashlib.sha256(content).hexdigest()
    (output / "实验报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(output.rglob("*")) if path.is_file()}
    (output / "file-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
