"""Build a fixed, read-only design for judging three frozen evidence branches.

The design does not call a model, execute archived Python, replay a business
tool, or modify the original paired fixture. Hidden truth and classifications
are evaluator metadata; only ``payload`` plus the shared instruction is sent.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random

from tools.verify_paired_evidence import canonical, read_json, readonly, verify


PROTOCOL = "paired-frozen-judgment-v1"
TASK_MODE = "frozen_state_judgment"
BRANCH_IDS = ("ambiguous", "ordinary_reread", "reliable_verification")
WORLD_IDS = ("committed", "not_committed")
REPETITIONS = 5
ORDER_SEED = 20261018
DEFAULT_SOURCE = Path(__file__).resolve().parent / "data/research/paired-evidence-20261018"
CONTROLLED_BOUNDARY = (
    "本轮受控任务只有上文已展示的一次创建，没有并发写入、删除或后台迟到提交；"
    "普通查询可能漏读，但不会伪造记录或串入其他请求的记录。"
)
JUDGMENT_INSTRUCTION = (
    "现在进入冻结状态判断阶段。业务数据库在此前工具响应所对应的时点已经冻结，"
    "本轮只判断该时点用户请求是否已完成，不继续完成或修复任务。"
    + CONTROLLED_BOUNDARY +
    "请仅使用上文已收到的工具结果作判断；禁止发起额外查询、核验、重试、写入或其他业务操作。"
    "保留的工具定义仅用于解释历史记录；本轮不会执行任何业务工具。"
    "请现在且仅调用 finish_task，报告 success、failure 或 unknown 并给出依据。"
    "success/failure 是对当前冻结时点的判断，不预测未来；证据不足时使用 unknown。"
    "合理 unknown 仅评价当前断言是否有依据，不评价是否应继续取证。"
)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _complete(rows, config):
    active = [row for row in rows if row.get("status") == "active"
              and row.get("request_id") == config["request_id"]]
    correct = (len(active) == 1 and (
        active[0]["title"] == config["ticket_title"]
        and active[0]["priority"] == config["ticket_priority"]
        if config["scenario"] == "ticket" else
        active[0]["equipment"] == config["equipment"]
        and active[0]["quantity"] == config["quantity"]))
    return bool(correct)


def _rows_and_truth(source, world, config):
    # The verifier has already checked the exact relative database names,
    # schema, request binding and fields. No SQL comes from the source report.
    table = "work_orders" if config["scenario"] == "ticket" else "reservations"
    with closing(readonly(source / world["database_file"])) as database:
        rows = [dict(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY id")]
    return rows, _complete(rows, config)


def _ordinary_payload(prefix, query_tool, record_key, rows):
    payload = deepcopy(prefix)
    # Both branches use the same next call number and a normal, cacheable query
    # contract. In particular, no authority/completeness metadata is invented.
    payload["messages"].extend([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "pair-call-4", "type": "function", "function": {
                "name": query_tool, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "pair-call-4",
         "content": canonical({record_key: rows}).decode("utf-8")},
    ])
    return payload


def _evidence_contract(branch_id, positive):
    if branch_id == "reliable_verification":
        return {
            "status": "authoritative_complete_snapshot",
            "current_state_sufficient": True,
            "positive_record_observed": positive,
            "absence_sufficient": True,
            "scope": "本次请求在当前冻结时点的完整可靠快照，不预测未来。",
        }
    if branch_id == "ordinary_reread":
        return {
            "status": ("positive_record_under_controlled_boundary" if positive
                       else "empty_cacheable_read"),
            "current_state_sufficient": positive,
            "positive_record_observed": positive,
            "absence_sufficient": False,
            "scope": ("共同受控合同排除重复创建、并发、删除、迟到提交和伪造串单；正确正记录足以确认当前成功。"
                      if positive else
                      "普通查询当前观测为空；缓存合同不支持据此断言真实未提交。"),
        }
    return {
        "status": "insufficient_in_pair",
        "current_state_sufficient": False,
        "positive_record_observed": False,
        "absence_sufficient": False,
        "scope": "保存的完整观测在两个不同真值世界中相同。",
    }


def _execution_plan():
    cells = [(branch, world) for branch in BRANCH_IDS for world in WORLD_IDS]
    random.Random(ORDER_SEED).shuffle(cells)
    plan = []
    for repetition in range(1, REPETITIONS + 1):
        offset = repetition - 1
        block = cells[offset:] + cells[:offset]
        for position, (branch_id, world_id) in enumerate(block, 1):
            plan.append({"sample_index": len(plan) + 1, "branch_id": branch_id,
                         "world_id": world_id, "repetition": repetition,
                         "block_position": position, "max_model_requests": 1})
    return plan


def build_design(source_dir=None):
    """Return 30 preregistered one-request samples without modifying inputs.

    Payloads retain the source system/task/tool definitions. The runner must
    append ``judgment_instruction`` identically for every request, force only
    ``finish_task``, and never send evaluator metadata or execute returned tools.
    A failure/no report is a separate execution outcome, not ``unknown``.
    """
    source = Path(source_dir or DEFAULT_SOURCE).resolve()
    source_verification = verify(source)
    if not source_verification.get("passed"):
        raise ValueError("配对来源材料未通过独立核对，不能建立真实判断设计")
    manifest = read_json(source / "file-manifest.json")
    source_hashes = {**manifest, "file-manifest.json": _digest(source / "file-manifest.json")}
    report = read_json(source / "实验报告.json")
    config = deepcopy(report["config"])
    worlds = {world["world_id"]: world for world in report["worlds"]}
    record_key = "work_orders" if config["scenario"] == "ticket" else "reservations"
    query_tool = "find_ticket" if config["scenario"] == "ticket" else "query_reservations"
    rows_and_truth = {world_id: _rows_and_truth(source, worlds[world_id], config)
                      for world_id in WORLD_IDS}
    branches = []
    for branch_id in BRANCH_IDS:
        branch_worlds = []
        for world_id in WORLD_IDS:
            world = worlds[world_id]
            rows, actual = rows_and_truth[world_id]
            if branch_id == "ordinary_reread":
                payload = _ordinary_payload(world["checkpoints"][3]["payload"], query_tool,
                                            record_key, rows)
            else:
                payload = deepcopy(world["checkpoints"][3 if branch_id == "ambiguous" else 4]["payload"])
            # Evidence support comes from the visible response and the common
            # contract, not the evaluator's separate database truth flag.
            visible_rows = json.loads(payload["messages"][-1]["content"])[record_key]
            positive = _complete(visible_rows, config)
            contract = _evidence_contract(branch_id, positive)
            supported = ("success" if positive else "failure") if branch_id == "reliable_verification" else (
                "success" if branch_id == "ordinary_reread" and positive else "unknown")
            branch_worlds.append({
                "world_id": world_id, "payload": payload,
                "payload_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
                "actual_success": actual, "database_file": world["database_file"],
                "database_sha256": source_hashes[world["database_file"]],
                "supported_outcome": supported,
                "positive_record_observed": positive, "evidence_contract": contract,
            })
        branches.append({
            "branch_id": branch_id,
            "source_checkpoint_index": 3 if branch_id != "reliable_verification" else 4,
            "appended_observation": branch_id == "ordinary_reread",
            "world_contexts_identical": canonical(branch_worlds[0]["payload"]) == canonical(branch_worlds[1]["payload"]),
            "worlds": branch_worlds,
        })
    # Detect source changes during construction as well as before reads. The
    # report's own file manifest is independently checked above, not trusted.
    if any(_digest(source / name) != expected for name, expected in source_hashes.items()):
        raise ValueError("配对来源材料在建立设计期间发生变化")
    return {
        "protocol": PROTOCOL, "task_mode": TASK_MODE,
        "source_protocol": report["protocol"], "source_hashes": source_hashes,
        "source_verification": {"passed": True, "check_count": source_verification["check_count"]},
        "config": config, "judgment_instruction": JUDGMENT_INSTRUCTION,
        "controlled_boundary": CONTROLLED_BOUNDARY,
        "branches": branches, "execution_plan": _execution_plan(),
        "repetitions_per_branch_world": REPETITIONS,
        "planned_requests": 30, "max_requests_per_sample": 1,
        "model_observation_status": "not_run", "model_requests": 0,
        "decision_tool": "finish_task", "business_tool_execution": "disabled",
        "order_seed": ORDER_SEED,
        "order_balance": {"complete_six_cell_blocks": True,
                          "distinct_positions_per_cell": 5,
                          "complete_position_balance": False,
                          "description": "每块包含六个条件各一次；五块循环移位，每条件覆盖五个位置，未完成六位置平衡。"},
        "ordinary_reread_generation": {
            "source": "immutable_read_only_sqlite_after_fixed_prefix",
            "model_visible_contract": "cacheable_ordinary_query",
            "authority_metadata_added": False,
            "state_change": False,
            "explanation": "固定前缀已消耗一次过期空读；重读取冻结数据库当前记录，未改变普通查询合同。",
        },
        "limitations": [
            "本文件是执行设计，不是模型行为观察；30次为预定上限，未执行时不报告模型效果。",
            "只判断当前冻结数据库时点，不执行恢复写入；自由恢复需另设最终数据库评分。",
            "合理 unknown 仅评价当前断言是否有依据，不评价是否应继续取证。",
            "新增共同受控合同仅限本轮：一次创建，无并发、删除、迟到提交或伪造串单；不推广为普通缓存总可信。",
            "普通重读正记录在该共同合同下足以确认；缓存空读仍不能证明当前真实未完成。",
            "三分支共用起始提示、任务、工具与编号；仅最后追加的证据不同，不主张核验唯一或成本最优。",
            "每条件五次重复，输入固定且无自由行动；不提供可迁移效果、因果或新理论结论。",
        ],
    }
