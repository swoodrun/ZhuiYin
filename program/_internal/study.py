"""Budgeted, interleaved repeated trials with explicit statistical denominators."""
from __future__ import annotations

import copy
import math
import random
import time

import engine
from model_budget import CampaignBudgetError, public_summary
from run_billing import RunBudget, summarize_usage


STRATEGIES = {
    "baseline": "原始",
    "idempotency_only": "仅幂等",
    "verification_only": "仅核验",
    "verified": "幂等＋核验",
}
INFRA_CODES = {"MODEL_EXECUTION_ERROR", "AGENT_DID_NOT_FINISH"}
PROMPT_POLICIES = {"neutral": "通用提示", "recovery": "恢复指令"}
REPORT_OUTCOMES = {"success", "failure", "unknown"}


def _protocol():
    return getattr(engine, "EVALUATION_PROTOCOL", "business-report-v2")


def _integer(value, name, low, high):
    if isinstance(value, bool) or not (isinstance(value, int) or
            isinstance(value, str) and value.isdecimal()):
        raise ValueError(f"{name}必须是整数")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{name}需要在 {low}—{high} 之间")
    return number


def _price(value, name):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name}必须是非负数，或留空")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name}必须是非负数，或留空") from None
    if not math.isfinite(number) or not 0 <= number <= 1_000_000_000:
        raise ValueError(f"{name}必须是有限非负数，且不超过十亿")
    return number


def validate_study_options(raw):
    """Return only recognized, public study options; never retain credentials."""
    if not isinstance(raw, dict):
        raise ValueError("统计实验参数必须是一个对象")
    allowed = {"repetitions", "strategies", "max_requests", "input_price_per_million",
               "output_price_per_million", "currency", "order_mode", "prompt_policies"}
    if set(raw) - allowed:
        raise ValueError("统计实验参数含不支持的字段")
    strategies = raw.get("strategies", list(STRATEGIES))
    if not isinstance(strategies, list) or not strategies or any(
            not isinstance(item, str) or item not in STRATEGIES for item in strategies):
        raise ValueError("至少选择一个受支持的对照策略")
    prompt_policies = raw.get("prompt_policies", ["neutral"])
    if not isinstance(prompt_policies, list) or not prompt_policies or any(
            not isinstance(item, str) or item not in PROMPT_POLICIES for item in prompt_policies):
        raise ValueError("至少选择一种受支持的提示方案：neutral 或 recovery")
    currency = raw.get("currency", "CNY")
    order_mode = raw.get("order_mode", "balanced")
    if order_mode not in ("balanced", "fixed"):
        raise ValueError("策略顺序仅支持 balanced 或 fixed")
    if not isinstance(currency, str) or not 1 <= len(currency.strip()) <= 12 or any(
            ord(char) < 32 or ord(char) == 127 for char in currency):
        raise ValueError("币种需要填写 1—12 个可见字符")
    return {
        "repetitions": _integer(raw.get("repetitions", 5), "每组重复次数", 2, 20),
        "strategies": list(dict.fromkeys(strategies)),
        "prompt_policies": list(dict.fromkeys(prompt_policies)),
        "max_requests": _integer(raw.get("max_requests", 80), "整个实验的模型请求上限", 1, engine.MAX_MODEL_REQUESTS),
        "input_price_per_million": _price(raw.get("input_price_per_million"), "每百万输入 token 价格"),
        "output_price_per_million": _price(raw.get("output_price_per_million"), "每百万输出 token 价格"),
        "currency": currency.strip(),
        "order_mode": order_mode,
    }


def wilson_interval(numerator, denominator):
    """95% Wilson score interval; no interval exists without valid observations."""
    if denominator == 0:
        return None
    z = 1.959963984540054
    proportion = numerator / denominator
    scale = 1 + z * z / denominator
    midpoint = (proportion + z * z / (2 * denominator)) / scale
    radius = z * math.sqrt(proportion * (1 - proportion) / denominator +
                           z * z / (4 * denominator * denominator)) / scale
    return [max(0.0, midpoint - radius), min(1.0, midpoint + radius)]


def _rate(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None,
            "ci95": wilson_interval(numerator, denominator)}


def _token(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(records, options, billing=None):
    """Unknown usage remains unknown, with known partial totals exposed separately."""
    if billing:
        summary = summarize_usage(records, billing)
        return {**summary,
                "successful_requests": sum(isinstance(record, dict) and record.get("status") == "ok"
                                           for record in records),
                "failed_requests": sum(not isinstance(record, dict) or record.get("status") != "ok"
                                       for record in records),
                "billing": copy.deepcopy(billing), "records": copy.deepcopy(records)}
    safe = []
    for record in records:
        if not isinstance(record, dict):
            record = {}
        prompt = _token(record.get("prompt_tokens"))
        completion = _token(record.get("completion_tokens"))
        total = _token(record.get("total_tokens"))
        known = bool(record.get("usage_available")) and prompt is not None and completion is not None
        safe.append({"status": record.get("status") if record.get("status") in
                     ("ok", "http_error", "network_error", "invalid_response") else "invalid_response",
                     "prompt_tokens": prompt, "completion_tokens": completion,
                     "total_tokens": total, "usage_available": known})
    unknown = sum(not record["usage_available"] for record in safe)
    prompt_complete = all(record["prompt_tokens"] is not None and record["usage_available"] for record in safe)
    completion_complete = all(record["completion_tokens"] is not None and record["usage_available"] for record in safe)
    known_prompt = sum(record["prompt_tokens"] or 0 for record in safe)
    known_completion = sum(record["completion_tokens"] or 0 for record in safe)
    prompt = known_prompt if prompt_complete else None
    completion = known_completion if completion_complete else None
    input_price, output_price = options["input_price_per_million"], options["output_price_per_million"]
    estimate = None
    if input_price is None or output_price is None:
        reason = "请补全输入和输出单价，再估算费用。"
    elif unknown:
        reason = "部分请求用量缺失，费用按未知处理。"
    else:
        estimate = (prompt * input_price + completion * output_price) / 1_000_000
        reason = "按你填写的单价估算；缓存、推理 token 及服务商其他计费规则需另行计算。"
    return {"requests": len(safe), "successful_requests": sum(r["status"] == "ok" for r in safe),
            "failed_requests": sum(r["status"] != "ok" for r in safe),
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion if prompt is not None and completion is not None else None,
            "known_prompt_tokens": known_prompt, "known_completion_tokens": known_completion,
            "usage_unknown_requests": unknown,
            "estimated_cost": estimate, "currency": options["currency"], "cost_note": reason,
            "input_price_per_million": input_price, "output_price_per_million": output_price}


def _arms(options, samples):
    arms = []
    for policy, strategy in ((policy, strategy) for policy in options.get("prompt_policies", ["neutral"])
                             for strategy in options["strategies"]):
        group = [sample for sample in samples if sample["strategy"] == strategy
                 and sample.get("prompt_policy", "neutral") == policy]
        valid = [sample for sample in group if sample["status"] == "valid"]
        failures = [sample for sample in valid if sample["failed"]]
        completions = sum(sample["actual_success"] for sample in valid)
        business_effect_failures = sum(sample["business_failed"] for sample in valid)
        report_errors = sum(sample["report_failed"] for sample in valid)
        uncertain = sum(sample["reported_outcome"] == "unknown" for sample in valid)
        confirmed = sum(sample["actual_success"] and sample["reported_outcome"] == "success"
                        for sample in valid)
        pilot = valid[0] if valid else None
        anchor = pilot if pilot and pilot["failed"] and pilot["failure_fingerprint"] else None
        followups = valid[1:]
        matched = sum(sample["failure_fingerprint"] == anchor["failure_fingerprint"]
                      for sample in followups) if anchor else 0
        arms.append({"strategy": strategy, "prompt_policy": policy,
                     "arm_id": f"{policy}:{strategy}",
                     "label": f"{PROMPT_POLICIES[policy]} · {STRATEGIES[strategy]}",
                     "evaluation_protocol": _protocol(),
                     "planned": options["repetitions"], "attempted": len(group),
                     "valid_trials": len(valid), "business_failures": len(failures),
                     "successes": completions,
                     "business_effect_failures": business_effect_failures,
                     "report_errors": report_errors, "uncertain_reports": uncertain,
                     "confirmed_completions": confirmed,
                     "report_outcomes": {outcome: sum(sample["reported_outcome"] == outcome
                                                        for sample in valid)
                                         for outcome in ("success", "failure", "unknown")},
                     "infra_errors": sum(sample["status"] == "infra_error" for sample in group),
                     "cancelled": sum(sample["status"] == "cancelled" for sample in group),
                     "unrun": options["repetitions"] - len(group),
                     "business_failure_rate": _rate(len(failures), len(valid)),
                     "business_failure_rate_definition": "兼容旧字段：有效轮次中任意裁判违规的比例，含报告不一致；不等于业务副作用失败率。",
                     "business_effect_failure_rate": _rate(business_effect_failures, len(valid)),
                     "report_error_rate": _rate(report_errors, len(valid)),
                     "uncertain_report_rate": _rate(uncertain, len(valid)),
                     "confirmed_completion_rate": _rate(confirmed, len(valid)),
                     "task_completion_rate": _rate(completions, len(valid)),
                     "attempted_completion_rate": _rate(completions, len(group)),
                     "reproduction": {
                         "anchor_fingerprint": anchor["failure_fingerprint"] if anchor else None,
                         "anchor_trial_index": anchor["trial_index"] if anchor else None,
                         "pilot_trial_index": pilot["trial_index"] if pilot else None,
                         "pilot_failed": bool(pilot["failed"]) if pilot else None,
                         "matched": matched, "denominator": len(followups),
                         "rate": matched / len(followups) if anchor and followups else None,
                         "ci95": wilson_interval(matched, len(followups)) if anchor else None,
                         "definition": "以本组第一个有效轮次为准。它失败时，统计后续有效轮次再次出现同一失败的比例，第一轮自身不计入。它成功或没有后续有效轮次时，比例显示为未知；不会改选后来的失败。"}})
    return arms


_model_identity = engine.model_identity  # Compatibility for existing callers.


def execution_plan(options, seed):
    """Balance tool strategies separately within each prompt-policy layer.

    A partial block need not be position-balanced; the plan and actual order are
    both reported so budget truncation remains visible rather than corrected.
    """
    strategies = options["strategies"]
    policies = options.get("prompt_policies", ["neutral"])
    offset = random.Random(seed).randrange(len(strategies)) if options["order_mode"] == "balanced" else 0
    policy_offset = random.Random(seed).randrange(len(policies)) if options["order_mode"] == "balanced" else 0
    plan = []
    for index in range(options["repetitions"]):
        shift = (offset + index) % len(strategies) if options["order_mode"] == "balanced" else 0
        policy_shift = (policy_offset + index) % len(policies) if options["order_mode"] == "balanced" else 0
        policy_order = policies[policy_shift:] + policies[:policy_shift]
        trials = []
        for policy_position, policy in enumerate(policy_order, 1):
            within_shift = (shift + policies.index(policy)) % len(strategies) if options["order_mode"] == "balanced" else 0
            ordered = strategies[within_shift:] + strategies[:within_shift]
            trials.extend({"prompt_policy": policy, "strategy": strategy,
                           "order_position": position, "policy_order_position": policy_position}
                          for position, strategy in enumerate(ordered, 1))
        plan.append({"trial_index": index + 1,
                     "prompt_policy": policies[0] if len(policies) == 1 else None,
                     # Keep the original single-policy field for older report readers.
                     "strategies": strategies[shift:] + strategies[:shift],
                     "prompt_policies": policy_order, "trials": trials})
    return plan


def _safe_factor_manifest(value):
    """Copy the public capability/prompt identity contract, never arbitrary metadata."""
    if not isinstance(value, dict):
        return None
    manifest = {}
    if isinstance(value.get("prompt_policy"), str) and value["prompt_policy"] in PROMPT_POLICIES:
        manifest["prompt_policy"] = value["prompt_policy"]
    for key in ("idempotency", "verification"):
        if isinstance(value.get(key), bool):
            manifest[key] = value[key]
    for key in ("system_prompt_sha256", "tool_schema_sha256"):
        digest = value.get(key)
        if isinstance(digest, str) and len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
            manifest[key] = digest
    if value.get("parameter_scope") == "server_supplied_business_fields":
        manifest["parameter_scope"] = value["parameter_scope"]
    if value.get("verification_enforcement") == "agent_choice":
        manifest["verification_enforcement"] = value["verification_enforcement"]
    return manifest


def _factor_design(config, options):
    """Public design contract: tools and instructions are separate factors."""
    policy_manifests = {}
    for policy in options["prompt_policies"]:
        policy_config = copy.copy(config)
        policy_config.prompt_policy = policy
        policy_manifests[policy] = {strategy: _safe_factor_manifest(
            engine.agent_spec(policy_config, strategy)["manifest"]) for strategy in options["strategies"]}
    return {
        "design": "tool-capabilities-by-prompt-policy",
        "group_by": ["prompt_policy", "strategy"],
        "factors": {
            "prompt_policy": options["prompt_policies"][:],
            "idempotency": sorted({engine.STRATEGIES[name]["idempotency"] for name in options["strategies"]}),
            "verification": sorted({engine.STRATEGIES[name]["verification"] for name in options["strategies"]}),
        },
        "strategy_capabilities": {name: {key: engine.STRATEGIES[name][key]
                                         for key in ("idempotency", "verification")}
                                  for name in options["strategies"]},
        "manifests_by_prompt_policy": policy_manifests,
        "prompt_policy_definitions": {
            "neutral": "四组使用相同通用系统提示；仅工具能力不同。",
            "recovery": "四组使用相同恢复系统提示；恢复指令不随核验能力增减。",
        },
        "fixed_within_policy": ["scenario", "business_input", "faults", "seed", "model_identity", "system_prompt"],
        "evaluation_protocol": _protocol(),
        "denominator_note": "每个提示方案与工具策略组合单独统计；不把不同提示方案合并为同一组。",
        "rules_note": "规则模式不会执行自然语言提示；只有模型模式可以检验提示方案的作用。",
    }


def _trial_evaluation(result):
    """Read v2 outcomes without treating a deliberate unknown as an outage.

    Legacy bool reports are accepted for protocol-test fixtures and old executors.
    Absence of a report is still incomplete; unknown requires an explicit outcome.
    """
    raw_violations = [value for value in result.get("violations", []) if isinstance(value, dict)]
    codes = {value.get("code", "") for value in raw_violations}
    outcome = result.get("reported_outcome")
    if not isinstance(outcome, str) or outcome not in REPORT_OUTCOMES:
        legacy = result.get("reported_success")
        outcome = ("success" if legacy else "failure") if isinstance(legacy, bool) else None
    completed = result.get("completed")
    if not isinstance(completed, bool):
        completed = outcome is not None
    infra = bool(codes & INFRA_CODES) or not completed or outcome is None
    business = result.get("business_violations")
    if not isinstance(business, list):
        business = [value for value in raw_violations
                    if value.get("code") not in INFRA_CODES | {"REPORT_MISMATCH"}]
    reporting = result.get("reporting_violations")
    if not isinstance(reporting, list):
        reporting = [value for value in raw_violations if value.get("code") == "REPORT_MISMATCH"]
    execution = result.get("execution_violations")
    if not isinstance(execution, list):
        execution = [value for value in raw_violations if value.get("code") in INFRA_CODES]
    business_failed = result.get("business_failed")
    if not isinstance(business_failed, bool):
        business_failed = bool(business) or (not infra and not result.get("actual_success"))
    report_failed = result.get("report_failed")
    if not isinstance(report_failed, bool):
        report_failed = bool(reporting)
    assessment = result.get("report_assessment")
    if assessment not in ("consistent", "inconsistent", "uncertain", "unreported"):
        assessment = ("unreported" if outcome is None else "uncertain" if outcome == "unknown"
                      else "inconsistent" if report_failed else "consistent")
    return {"infra": infra, "completed": completed, "reported_outcome": outcome,
            "report_assessment": assessment,
            "business_failed": business_failed, "report_failed": report_failed,
            "business_violations": _public_violations(business),
            "reporting_violations": _public_violations(reporting),
            "execution_violations": _public_violations(execution)}


def _public_violations(violations):
    return [{"code": code,
             "message": "模型调用或任务报告未完成；本轮不纳入业务比例。" if code in INFRA_CODES else
                        str(violation.get("message", ""))[:400]}
            for violation in violations if isinstance(violation, dict)
            for code in [str(violation.get("code", ""))[:80]]]


def run_study(config, options, on_progress=None, cancel_check=None, on_checkpoint=None,
              on_trial=None):
    """Run fresh agents under one shared request budget, restoring thread-local state.

    An optional observer receives isolated copies of each attempted trial's public
    sample and full engine result before its checkpoint. Observer failures abort
    the study, so a collector cannot silently lose evidence and continue running.
    """
    billing = copy.deepcopy(getattr(config, "billing", {}))
    # Old API callers may still supply their own study request limit. The new
    # workbench sends one shared setting, and its config is also the fallback.
    raw_options = options
    if billing and isinstance(options, dict) and "max_requests" not in options:
        raw_options = {**options, "max_requests": config.max_requests}
    options = validate_study_options(raw_options)
    mode = config.agent_mode
    if mode not in ("rules", "llm"):
        raise ValueError("统计实验只支持规则模式或模型模式")
    samples, usage_records = [], []
    started = time.perf_counter()
    planned = options["repetitions"] * len(options["strategies"]) * len(options["prompt_policies"])
    plan = execution_plan(options, config.seed)
    provenance = engine.report_provenance(config)
    factor_design = _factor_design(config, options)
    stopped_reason = None
    guard = RunBudget(billing) if mode == "llm" and billing else None
    sentinel = object()
    previous = {key: getattr(engine.MODEL_CONTEXT, key, sentinel)
                for key in engine.MODEL_CONTEXT_FIELDS}
    engine.MODEL_CONTEXT.remaining = options["max_requests"]
    engine.MODEL_CONTEXT.cancel_check = cancel_check
    engine.MODEL_CONTEXT.usage_records = usage_records
    engine.MODEL_CONTEXT.budget_exhausted = False
    engine.MODEL_CONTEXT.request_limit = options["max_requests"]
    engine.MODEL_CONTEXT.campaign_stopped_reason = None
    engine.MODEL_CONTEXT.run_budget = guard
    engine.MODEL_CONTEXT.cost_stopped_reason = None

    limitations = [
        "规则模式是本地模拟，只验证工具本身的机制。真实模型的效果需另测。" if mode == "rules" else
        "模型模式每轮重新调用模型。结果限于本次配置、模型服务和样本。",
        ("各策略每轮轮换位置，种子决定起始位置。完整的轮换周期内，各策略占据每个位置的次数相同；预算截断仍可能导致样本数不同。"
         if options["order_mode"] == "balanced" else "各策略每轮按固定顺序运行，预算截断可能导致后面的策略样本较少。"),
        "各策略场景、种子、故障相同。种子相同，模型输出仍可能不同。",
        "提示方案是独立因素：同一提示方案内四组系统提示相同，工具能力单独变化。两种提示方案分层统计；规则模式不能检验自然语言指令作用。",
        "比例按有效轮次计算。运行错误、未完成和取消的轮次单独列出，不计入分母。",
        "另列按已尝试轮次计算的实际完成率。运行错误、未完成和取消计为未完成；尚未运行的轮次不计入分母。",
        "实际完成率依据数据库；已确认完成率另要求明确报告成功。明确报告无法确认是有效轮次，单列无法确认占比；它不等于服务运行错误。",
        "业务副作用失败率与报告不一致率分列。旧 business_failure_rate 保留任意裁判违规的兼容含义，所以实际完成与汇总违规两个比例可能同时不为 0。",
        "区间按 Wilson 95% 方法估算，未做显著性检验。样本少、同场景轮次相关、模型服务变化都会影响比较。",
        "以每组第一个有效轮次为准：它失败时，统计后续有效轮次是否出现同一失败，第一轮自身不计入。它成功则不统计，也不会改选后来的失败。",
        "所有轮次共享请求上限。失败也占用预算；已发出的请求无法立即取消。",
        ("token 用量以服务商返回为准；按工作台的缓存命中输入、缓存未命中输入、输出三项单价估算。缺失用量保留预留金额，不能算作零费用。"
         if billing else "token 用量以服务商返回为准，缺失按未知处理。费用按你填写的单价估算；缓存、推理 token 及其他计费规则需另算。"),
        "检查范围是本机内置规则，以及 7 类工具故障中本次选择的故障。真实系统的可靠性需另外验证。",
    ]

    def snapshot(final=False):
        complete = len(samples) == planned and not stopped_reason
        report = {"kind": "study", "status": "finished" if final and complete else "partial",
                "mode": mode, "config": copy.deepcopy(config.public()), "options": copy.deepcopy(options),
                "evaluation_protocol": _protocol(), "factor_design": copy.deepcopy(factor_design),
                "model_identity": engine.model_identity(config), "provenance": copy.deepcopy(provenance),
                "execution_plan": copy.deepcopy(plan),
                "execution_order": [{"sequence": index + 1, "trial_index": sample["trial_index"],
                                     "prompt_policy": sample["prompt_policy"],
                                     "policy_order_position": sample["policy_order_position"],
                                     "order_position": sample["order_position"],
                                     "strategy": sample["strategy"], "status": sample["status"]}
                                    for index, sample in enumerate(samples)],
                "arms": _arms(options, samples), "samples": copy.deepcopy(samples),
                "model_usage": {**_usage(usage_records, options, billing), "limit": options["max_requests"],
                                "external_model_verified": mode == "llm" and bool(usage_records)},
                "completed": len(samples), "planned": planned,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "limitations": limitations[:], "stopped_reason": stopped_reason}
        if guard is not None:
            report["run_budget"] = guard.summary()
            report["model_usage"]["run_budget"] = guard.summary()
        try:
            campaign = public_summary()
        except CampaignBudgetError:
            campaign = {"status": "unavailable", "note": "累计预算台账无法安全读取；新请求将被保护门闸拒绝。"}
        if campaign is not None:
            report["campaign_budget"] = campaign
        return report

    def checkpoint(final=False):
        report = snapshot(final)
        if on_checkpoint:
            on_checkpoint(copy.deepcopy(report))
        return report

    def is_cancelled():
        return bool(cancel_check and cancel_check())

    try:
        for round_plan in plan:
            trial_index = round_plan["trial_index"]
            for planned_trial in round_plan["trials"]:
                strategy = planned_trial["strategy"]
                prompt_policy = planned_trial["prompt_policy"]
                order_position = planned_trial["order_position"]
                if is_cancelled():
                    stopped_reason = "cancelled"
                    break
                if mode == "llm" and engine.MODEL_CONTEXT.cost_stopped_reason:
                    stopped_reason = engine.MODEL_CONTEXT.cost_stopped_reason
                    break
                if mode == "llm" and engine.MODEL_CONTEXT.remaining <= 0:
                    stopped_reason = "request_budget_exhausted"
                    break
                if on_progress:
                    on_progress("study", int(len(samples) / planned * 100),
                                f"第 {trial_index}/{options['repetitions']} 轮 · {PROMPT_POLICIES[prompt_policy]} · {STRATEGIES[strategy]}，已完成 {len(samples)}/{planned}")
                before = len(usage_records)
                trial_started = time.perf_counter()
                try:
                    trial_config = copy.deepcopy(config)
                    trial_config.prompt_policy = prompt_policy
                    result = engine.run_trial(trial_config, trial_config.faults, strategy,
                                              agent_mode=mode, model_config=config.model_config)
                except Exception:
                    # Defense in depth: run_trial currently converts execution
                    # failures into violations, but Lab initialization before
                    # its try block, tests, or future executors can still raise.
                    # Provider exception text and arbitrary payloads must not enter reports.
                    result = {"failed": True, "actual_success": False,
                              "failure_fingerprint": "MODEL_EXECUTION_ERROR",
                              "violations": [{"code": "MODEL_EXECUTION_ERROR"}], "stats": {}}
                evaluation = _trial_evaluation(result)
                infra = evaluation.pop("infra")
                status = "cancelled" if infra and is_cancelled() else "infra_error" if infra else "valid"
                violations = _public_violations(result.get("violations", []))
                stats = {key: result.get("stats", {}).get(key, 0)
                         for key in ("tool_calls", "physical_writes", "idempotency_hits", "verification_calls")}
                sample = {"strategy": strategy, "prompt_policy": prompt_policy,
                          "arm_id": f"{prompt_policy}:{strategy}",
                          "evaluation_protocol": result.get("evaluation_protocol", _protocol()),
                          "factor_manifest": _safe_factor_manifest(result.get("factor_manifest")),
                          **evaluation,
                          "trial_index": trial_index, "status": status,
                          "execution_index": len(samples) + 1, "order_position": order_position,
                          "policy_order_position": planned_trial["policy_order_position"],
                          "elapsed_ms": round((time.perf_counter() - trial_started) * 1000, 3),
                          "failed": bool(result.get("failed")),
                          "actual_success": bool(result.get("actual_success")),
                          "reported_success": result.get("reported_success"),
                          "business_records": copy.deepcopy(result.get("business_records", result.get("reservations", []))),
                          "events": copy.deepcopy(result.get("events", [])),
                          "stock_initial": result.get("stock_initial"),
                          "stock_remaining": result.get("stock_remaining"),
                          "failure_fingerprint": str(result.get("failure_fingerprint", ""))[:500],
                          "violations": violations, "stats": stats,
                          "model_usage": _usage(usage_records[before:], options, billing)}
                samples.append(sample)
                if is_cancelled():
                    stopped_reason = "cancelled"
                elif mode == "llm" and engine.MODEL_CONTEXT.cost_stopped_reason:
                    stopped_reason = engine.MODEL_CONTEXT.cost_stopped_reason
                elif mode == "llm" and engine.MODEL_CONTEXT.campaign_stopped_reason:
                    stopped_reason = engine.MODEL_CONTEXT.campaign_stopped_reason
                elif mode == "llm" and (engine.MODEL_CONTEXT.budget_exhausted or
                        engine.MODEL_CONTEXT.remaining <= 0 and len(samples) < planned):
                    stopped_reason = "request_budget_exhausted"
                if on_trial:
                    on_trial(copy.deepcopy(sample), copy.deepcopy(result))
                checkpoint()
                if stopped_reason:
                    break
            if stopped_reason:
                break
        report = checkpoint(final=True)
        if on_progress:
            on_progress("done" if report["status"] == "finished" else "partial", 100,
                        "多轮统计已完成" if report["status"] == "finished" else "多轮统计已停止，已保留部分结果")
        return report
    finally:
        for key, value in previous.items():
            if value is sentinel:
                if hasattr(engine.MODEL_CONTEXT, key):
                    delattr(engine.MODEL_CONTEXT, key)
            else:
                setattr(engine.MODEL_CONTEXT, key, value)
