"""Preserve selected historical template rules across fresh batch retests."""
from __future__ import annotations

import copy
import json

from engine import Config
from task_templates import evaluate_contract, validate_snapshot


BUSINESS_FIELDS = ("scenario", "request_id", "equipment", "stock", "quantity", "ticket_title",
                   "ticket_priority", "seed", "faults")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def business_identity(config):
    """Ignore execution policy, model connection and batch budget overrides."""
    if isinstance(config, Config):
        config = config.public()
    if not isinstance(config, dict):
        raise ValueError("重测任务配置必须为对象")
    public = Config.from_dict({**config, "agent_mode": "rules", "model_config": {}}).public()
    identity = {key: public[key] for key in BUSINESS_FIELDS}
    identity["faults"] = sorted(identity["faults"])
    return _canonical(identity)


def _snapshot(candidate):
    config = candidate.get("config")
    if not isinstance(config, dict):
        raise ValueError("所选历史任务缺少业务配置")
    values = [value for value in (candidate.get("template_snapshot"), config.get("template_snapshot"))
              if value is not None]
    if not values:
        return None
    snapshots = [validate_snapshot(value) for value in values]
    if any(_canonical(value) != _canonical(snapshots[0]) for value in snapshots[1:]):
        raise ValueError("所选历史任务中的模板快照不一致，请分开重测")
    if business_identity(snapshots[0]["config"]) != business_identity(config):
        raise ValueError("所选历史任务与模板的业务参数不同，不能沿用该模板规则")
    return snapshots[0]


def attach_retest_templates(plan, selected_ids, candidates):
    """Attach immutable source versions without mutating preview or history."""
    if not isinstance(plan, dict) or not isinstance(plan.get("cases"), list):
        raise ValueError("重测预览缺少任务列表")
    if (not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= 500
            or any(not isinstance(value, str) for value in selected_ids)
            or len(set(selected_ids)) != len(selected_ids)):
        raise ValueError("请选择1—500个不同的历史任务")
    if not isinstance(candidates, list):
        raise ValueError("历史任务候选列表无效")
    available = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str):
            raise ValueError("历史任务候选格式无效")
        if candidate["candidate_id"] in available:
            raise ValueError("历史任务候选编号重复，无法确定模板来源")
        available[candidate["candidate_id"]] = candidate
    selections = {}
    for selected in selected_ids:
        candidate = available.get(selected)
        if not candidate or not candidate.get("eligible"):
            raise ValueError("所选历史任务不可重建")
        identity = business_identity(candidate.get("config"))
        snapshot = _snapshot(candidate)
        if identity in selections and _canonical(selections[identity]) != _canonical(snapshot):
            raise ValueError("同一任务选择了不同模板版本或业务规则，请分别重测")
        selections[identity] = snapshot
    result, matched = copy.deepcopy(plan), set()
    for case in result["cases"]:
        if not isinstance(case, dict) or case.get("kind") != "scenario" or not isinstance(case.get("config"), dict):
            continue
        identity = business_identity(case["config"])
        if identity not in selections:
            continue
        matched.add(identity)
        snapshot = selections[identity]
        existing = case.get("template_snapshot")
        if existing is not None and _canonical(validate_snapshot(existing)) != _canonical(snapshot):
            raise ValueError("重测预览已存在不同的模板业务规则")
        if snapshot is not None:
            case["template_snapshot"] = copy.deepcopy(snapshot)
    if set(selections) - matched:
        raise ValueError("部分历史任务未出现在重测预览中，拒绝丢失其模板规则")
    if "items" in result:
        result["items"] = result["cases"]
    return result


def decorate_retest_trial(trial, case, config):
    """Evaluate preserved source rules while retaining the original oracle."""
    if not isinstance(trial, dict) or not isinstance(case, dict):
        raise ValueError("重测执行记录和任务必须为对象")
    result = copy.deepcopy(trial)
    snapshot = case.get("template_snapshot")
    if snapshot is None:
        return result
    snapshot = validate_snapshot(snapshot)
    if business_identity(snapshot["config"]) != business_identity(case.get("config")):
        raise ValueError("重测模板与任务业务参数不一致")
    if business_identity(config) != business_identity(case["config"]):
        raise ValueError("当前执行任务与重测来源不同，不能套用历史模板规则")
    public = config.public() if isinstance(config, Config) else copy.deepcopy(config)
    result["template_contract"] = evaluate_contract(result, snapshot["contract"], config=public)
    return result
