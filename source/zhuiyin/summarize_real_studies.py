"""Summarize existing model-mode study reports without sending any requests.

This command reads only explicitly supplied, public JSON evidence files. A model
name or a service URL in a report is a recorded identity, not independent proof
that a particular provider produced its responses. Explicit synthetic markers
are rejected, including markers nested within evaluation_context or samples.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlsplit


STRATEGIES = {
    "baseline": "原始", "idempotency_only": "仅幂等",
    "verification_only": "仅核验", "verified": "幂等＋核验",
}
SCENARIOS = {"equipment": "设备预约", "ticket": "工单创建"}
SAMPLE_FIELDS = {
    "strategy", "trial_index", "status", "execution_index", "order_position",
    "elapsed_ms", "failed", "actual_success", "reported_success", "business_records",
    "events", "stock_initial", "stock_remaining", "failure_fingerprint", "violations",
    "stats", "model_usage",
}
USAGE_FIELDS = {
    "requests", "successful_requests", "failed_requests", "prompt_tokens",
    "completion_tokens", "total_tokens", "known_prompt_tokens", "known_completion_tokens",
    "usage_unknown_requests", "estimated_cost", "currency", "cost_note", "limit",
    "input_price_per_million", "output_price_per_million", "external_model_verified",
}
PROVENANCE_FIELDS = {
    "app_version", "prompt_version", "prompt_definition_sha256", "source_sha256",
    "source_tree_sha256", "public_config_sha256",
}
CONFIG_FIELDS = {
    "scenario", "equipment", "stock", "quantity", "request_id", "seed", "faults",
    "agent_mode", "ticket_title", "ticket_priority", "max_requests",
}


def _timestamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _reject_synthetic(value):
    if isinstance(value, dict):
        if value.get("synthetic_protocol") is True or value.get("real_model_evidence") is False:
            raise ValueError("输入含明确的合成协议或非真实模型标记，不能用于真实模型实测汇总")
        for child in value.values():
            _reject_synthetic(child)
    elif isinstance(value, list):
        for child in value:
            _reject_synthetic(child)


def _read_json(path):
    data = path.read_bytes()
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise ValueError("输入不是有效的 UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("输入 JSON 顶层必须为对象")
    _reject_synthetic(value)
    return value, {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def _whitelist(value, allowed):
    return {key: copy.deepcopy(child) for key, child in value.items() if key in allowed} if isinstance(value, dict) else {}


def _origin(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return None
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return None


def _rate(numerator, denominator):
    if not denominator:
        return {"numerator": numerator, "denominator": denominator, "rate": None, "ci95": None}
    z = 1.959963984540054
    fraction = numerator / denominator
    scale = 1 + z * z / denominator
    midpoint = (fraction + z * z / (2 * denominator)) / scale
    radius = z * math.sqrt(fraction * (1 - fraction) / denominator + z * z / (4 * denominator * denominator)) / scale
    return {"numerator": numerator, "denominator": denominator, "rate": fraction,
            "ci95": [max(0.0, midpoint - radius), min(1.0, midpoint + radius)]}


def _usage_totals(samples):
    """Keep missing usage unknown, exposing known partial totals separately."""
    usages = [sample.get("model_usage") if isinstance(sample.get("model_usage"), dict) else {} for sample in samples]
    result = {}
    for key in ("requests", "successful_requests", "failed_requests", "prompt_tokens", "completion_tokens", "total_tokens", "usage_unknown_requests"):
        values = [_integer(usage.get(key)) for usage in usages]
        result[key] = sum(values) if all(value is not None for value in values) else None
    for key, fallback in (("known_prompt_tokens", "prompt_tokens"), ("known_completion_tokens", "completion_tokens")):
        result[key] = sum(_integer(usage.get(key)) or _integer(usage.get(fallback)) or 0 for usage in usages)
    costs = [_number(usage.get("estimated_cost")) for usage in usages]
    currencies = {usage.get("currency") for usage in usages if isinstance(usage.get("currency"), str)}
    result["estimated_cost"] = sum(costs) if all(value is not None for value in costs) and len(currencies) == 1 else None
    result["currency"] = next(iter(currencies)) if len(currencies) == 1 else None
    result["cost_note"] = "仅为报告单价下的估算；缺失用量不按 0 处理，最终以服务商账单为准。"
    return result


def summarize_report(report, source):
    _reject_synthetic(report)
    if report.get("kind") != "study" or report.get("mode") != "llm":
        raise ValueError("只接受 kind=study 且 mode=llm 的多轮报告；规则报告不能作为真实模型证据")
    config = report.get("config") if isinstance(report.get("config"), dict) else {}
    options = report.get("options") if isinstance(report.get("options"), dict) else {}
    samples = report.get("samples")
    strategies = options.get("strategies", list(STRATEGIES))
    repetitions = _integer(options.get("repetitions"))
    if repetitions is None or repetitions < 1 or not isinstance(strategies, list) or not strategies or len(set(strategies)) != len(strategies) or any(strategy not in STRATEGIES for strategy in strategies):
        raise ValueError("报告缺少有效的每组次数或策略计划")
    if not isinstance(samples, list):
        raise ValueError("报告 samples 必须为列表")
    seen = set()
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("strategy") not in strategies or sample.get("status") not in ("valid", "infra_error", "cancelled"):
            raise ValueError("样本策略或执行状态无效")
        trial_index = _integer(sample.get("trial_index"))
        if trial_index is None or not 1 <= trial_index <= repetitions:
            raise ValueError("样本轮次超出计划范围")
        key = (sample["strategy"], trial_index)
        if key in seen:
            raise ValueError("报告含重复的策略轮次，拒绝重复计数")
        seen.add(key)
        if sample["status"] == "valid" and (not isinstance(sample.get("failed"), bool) or not isinstance(sample.get("actual_success"), bool)):
            raise ValueError("有效样本必须包含明确的业务失败及实际完成布尔值")
    identity = report.get("model_identity") if isinstance(report.get("model_identity"), dict) else {}
    model = identity.get("model") if isinstance(identity.get("model"), str) and identity.get("model").strip() else None
    origin = _origin(identity.get("api_origin"))
    public_identity = {"model": model, "api_origin": origin}
    if identity.get("thinking") in ("disabled", "enabled"):
        public_identity["thinking"] = identity["thinking"]
    usage = _whitelist(report.get("model_usage"), USAGE_FIELDS)
    requests = _integer(usage.get("requests"))
    evidence_fields = {"mode_llm": True, "model_recorded": bool(model), "api_origin_recorded": bool(origin),
                       "samples_present": bool(samples), "positive_request_count_recorded": requests is not None and requests > 0,
                       "successful_request_count_recorded": (_integer(usage.get("successful_requests")) or 0) > 0}
    evidence_complete = all(evidence_fields.values())
    evidence = {"assessment": "reported_external_model_execution" if evidence_complete else "insufficient_recorded_evidence",
                "checks": evidence_fields,
                "identity_note": "模型名、API 来源和请求用量均为输入报告的记录；本汇总未独立认证服务方或响应来源。"}
    arms = []
    for strategy in strategies:
        group = [sample for sample in samples if sample["strategy"] == strategy]
        valid = [sample for sample in group if sample["status"] == "valid"]
        failures = [sample for sample in valid if sample["failed"]]
        completions = sum(sample["actual_success"] for sample in valid)
        pilot = valid[0] if valid else None
        anchor = pilot if pilot and pilot["failed"] and pilot.get("failure_fingerprint") else None
        followups = valid[1:]
        matched = sum(sample.get("failure_fingerprint") == anchor["failure_fingerprint"] for sample in followups) if anchor else 0
        reproduction = _rate(matched, len(followups)) if anchor else {"numerator": 0, "denominator": len(followups), "rate": None, "ci95": None}
        reproduction.update({"anchor_fingerprint": anchor["failure_fingerprint"] if anchor else None,
                             "anchor_trial_index": anchor["trial_index"] if anchor else None,
                             "definition": "以首个有效轮次为锚点：它失败时，仅统计后续有效轮次的同一指纹复现；首轮成功或没有后续轮次时未知。"})
        arms.append({"strategy": strategy, "label": STRATEGIES[strategy], "planned": repetitions,
                     "attempted": len(group), "valid_trials": len(valid), "unrun": repetitions - len(group),
                     "business_failures": len(failures), "infra_errors": sum(sample["status"] == "infra_error" for sample in group),
                     "cancelled": sum(sample["status"] == "cancelled" for sample in group), "actual_completions": completions,
                     "business_failure_rate": _rate(len(failures), len(valid)),
                     "task_completion_rate": _rate(completions, len(valid)),
                     "attempted_completion_rate": _rate(completions, len(group)), "reproduction": reproduction,
                     "model_usage": _usage_totals(group)})
    failure_samples = [_whitelist(sample, SAMPLE_FIELDS) for sample in samples if sample["status"] != "valid" or sample.get("failed")]
    valid_failures = sum(sample["status"] == "valid" and sample["failed"] for sample in samples)
    return {"source": source, "scenario": config.get("scenario"), "scenario_label": SCENARIOS.get(config.get("scenario"), "其他场景"),
            "status": report.get("status"), "stopped_reason": report.get("stopped_reason"),
            "mode": "llm", "model_identity": public_identity, "evidence": evidence,
            "config": _whitelist(config, CONFIG_FIELDS), "options": _whitelist(options, {"repetitions", "strategies", "max_requests", "order_mode", "input_price_per_million", "output_price_per_million", "currency"}),
            "provenance": _whitelist(report.get("provenance"), PROVENANCE_FIELDS), "arms": arms,
            "execution_plan": copy.deepcopy(report.get("execution_plan", [])),
            "execution_order": copy.deepcopy(report.get("execution_order", [])), "model_usage": usage,
            "sample_usage_totals": _usage_totals(samples), "sample_count": len(samples),
            "business_failure_sample_count": valid_failures, "preserved_failure_sample_count": len(failure_samples),
            "failure_samples": failure_samples,
            "failure_preservation_note": "完整保留每个失败或非有效样本的现有业务记录、执行事件、违规、指纹、统计及用量；成功样本仍在原始报告中。",
            "limitations": copy.deepcopy(report.get("limitations", []))}


def _cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("`", "'")


def _percent(value):
    return "未知" if value is None else f"{value * 100:.1f}%"


def _known(value):
    return str(value) if value is not None else "未知"


def _rate_text(value):
    interval = value.get("ci95")
    ci = f"；95%区间 {_percent(interval[0])}—{_percent(interval[1])}" if interval else "；95%区间未知"
    return f"{value['numerator']}/{value['denominator']}（{_percent(value.get('rate'))}{ci}）"


def render_markdown(summary):
    lines = ["# 追因：真实模型实验记录汇总", "", f"生成时间：{summary['created_at']}（北京时间）。", "",
             "这是一轮探索性实验。每组计划次数及实际执行数分别列出；不足的轮次保持未运行。两个业务分别分析，不合并推断普遍优势。", "",
             "本文件只汇总已有报告，没有发出任何模型请求。模型名、API 来源和用量是报告记录，不能单凭字段或名称独立证明服务方真实性。", "",
             "## 预算与费用口径", "",
             "费用保护台账用于执行前预留、控制调用预算；报告 token 单价用于费用估算。两者均不等于实际账单，缓存、推理及平台其他计费以服务商账单为准。缺失 token 用量与费用显示未知，不按 0 处理。", ""]
    if summary.get("campaign"):
        lines += ["公开执行台账摘要：", "", "```json", json.dumps(summary["campaign"]["public_summary"], ensure_ascii=False, indent=2), "```", ""]
    else:
        lines += ["未提供公开执行台账；无法从这份汇总独立核对跨场景预算保护。", ""]
    for index, report in enumerate(summary["reports"], 1):
        identity, usage, evidence = report["model_identity"], report["model_usage"], report["evidence"]
        cost = usage.get("estimated_cost")
        lines += [f"## {index}. {_cell(report['scenario_label'])}", "",
                  f"状态：{_cell(report['status'])}；停止原因：{_cell(report.get('stopped_reason') or '无记录')}。模式：llm。",
                  f"记录的模型：{_cell(identity.get('model') or '未知')}；API 来源：{_cell(identity.get('api_origin') or '未知')}。",
                  f"思考模式记录：{_cell(identity.get('thinking') or '未记录')}。",
                  f"证据完整性：{'报告记录支持已产生外部模型执行' if evidence['assessment'] == 'reported_external_model_execution' else '不足，不能据此声称已有完整真实模型实测证据'}；检查项：{_cell(json.dumps(evidence['checks'], ensure_ascii=False))}。",
                  f"请求：{_known(usage.get('requests'))}；成功响应：{_known(usage.get('successful_requests'))}；失败请求：{_known(usage.get('failed_requests'))}。",
                  f"输入 token：{_known(usage.get('prompt_tokens'))}；输出 token：{_known(usage.get('completion_tokens'))}；用量未知请求：{_known(usage.get('usage_unknown_requests'))}。",
                  f"报告单价口径估算：{str(cost) + ' ' + str(usage.get('currency', '')) if cost is not None else '未知'}。{_cell(usage.get('cost_note') or '')}", "",
                  "| 策略 | 有效/尝试/计划 | 未运行 | 运行错误/取消 | 业务错误率（有效） | 完成率（有效） | 完成率（尝试） | 同错复现率 | 请求 | 已知输入/输出 token |", "|---|---|---|---|---|---|---|---|---|---|"]
        for arm in report["arms"]:
            group_usage = arm["model_usage"]
            lines.append("| " + " | ".join([
                arm["label"], f"{arm['valid_trials']}/{arm['attempted']}/{arm['planned']}", str(arm["unrun"]),
                f"{arm['infra_errors']}/{arm['cancelled']}", _rate_text(arm["business_failure_rate"]),
                _rate_text(arm["task_completion_rate"]), _rate_text(arm["attempted_completion_rate"]),
                _rate_text(arm["reproduction"]), str(group_usage["requests"]) if group_usage["requests"] is not None else "未知",
                f"{group_usage['known_prompt_tokens']}/{group_usage['known_completion_tokens']}",
            ]) + " |")
        lines += ["", "各比例区间均为 Wilson 95% 区间，未做显著性检验。业务错误率只使用有效轮次；完成率另列已尝试分母，运行错误和取消按未完成计，未运行不计入尝试分母。", "",
                  "“实际完成”来自数据库裁判；完成后报告不一致仍可出现业务违规，因此完成与错误并不互为补数。", "",
                  "同错复现以本组首个有效轮次为锚点：它失败时统计后续有效轮次再次出现同一错误指纹的比例，首轮自身不计；首轮成功或没有后续有效轮次时显示未知。", "",
                  f"完整失败与未完成样本：{report['preserved_failure_sample_count']} 条（其中有效业务失败 {report['business_failure_sample_count']} 条），保存在 summary.json 的 reports[{index - 1}].failure_samples。原始成功及失败全部轮次仍保留于输入报告。", "",
                  f"原始报告：`{_cell(report['source']['path'])}`；SHA-256：`{report['source']['sha256']}`。", "",
                  f"程序/提示版本：{_cell(report['provenance'].get('app_version', '未知'))} / {_cell(report['provenance'].get('prompt_version', '未知'))}；执行顺序及源码散列保存在 summary.json。", ""]
    lines += ["## 解释边界", "", "每组 5 轮只能作为探索性材料，区间通常很宽。同一场景、故障组合和服务时段下的轮次可能相关，模型服务也可能变化。本汇总不把数值差异称作显著改进，不把两个场景合并推断普遍可靠性，不将本地故障注入等同于真实系统故障迁移。", "",
              "失败请求、运行错误、取消及因预算停止的未运行轮次均单列保留。未观察到策略差异表示这些有限条件下未观察到差异，不构成等效或优越性证明。", ""]
    return "\n".join(lines)


def _public_campaign(raw):
    """Select only budget/accounting fields, never copy request headers/configs."""
    allowed = {
        "schema_version", "allowed_origin", "allowed_model", "policy_sha256", "input_padding",
        "requests_made", "requests_remaining", "accounted_cost_cny", "settled_cost_cny", "remaining_cost_cny",
        "unsettled_requests", "known_usage_requests", "overrun_requests", "halted_reason", "reservation_assumption",
        "kind", "status", "created_at", "updated_at", "currency", "request_limit", "max_requests", "money_limit", "max_cost", "max_cost_cny", "budget_cny",
        "requests", "request_count", "requests_attempted", "successful_requests", "failed_requests", "reserved_cost", "reserved_cost_cny", "known_cost", "known_cost_cny", "estimated_cost", "estimated_cost_cny",
        "known_prompt_tokens", "known_completion_tokens", "prompt_tokens", "completion_tokens", "unknown_usage_requests", "usage_unknown_requests", "stopped_reason", "cost_note", "cost_policy", "pricing", "policy",
        "limits", "totals", "budget", "usage", "ledger", "records", "request_records", "warnings", "note", "notes",
        "sequence", "status", "elapsed_ms", "reserved_cny", "known_estimated_cny", "input_price_per_million", "output_price_per_million", "input_cny_per_million", "output_cny_per_million", "input_price_cny_per_million", "output_price_cny_per_million",
        "max_input_tokens", "max_output_tokens", "max_prompt_tokens", "max_completion_tokens", "response_status", "usage_available", "http_status", "scenario", "strategy", "trial_index", "reservation", "reservation_cny", "actual_cost_cny", "billing_note", "protection_note", "finalized", "count", "limit", "spent", "reserved",
    }
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(child) for key, child in value.items() if key in allowed}
        if isinstance(value, list):
            return [clean(child) for child in value]
        return value
    return clean(raw)


def build_summary(report_inputs, campaign_input=None):
    reports = [summarize_report(report, source) for report, source in report_inputs]
    summary = {"kind": "model_study_evidence_summary", "created_at": _timestamp(),
               "network_requests_sent_by_summarizer": 0, "report_count": len(reports), "reports": reports,
               "combined_usage_only": _usage_totals(reports),
               "inference_scope": "各场景分别呈现；探索性，不汇总推断跨场景普遍优势。"}
    if campaign_input:
        campaign, source = campaign_input
        _reject_synthetic(campaign)
        summary["campaign"] = {"source": source, "public_summary": _public_campaign(campaign),
                               "cost_note": "费用保护上界/估计，不是服务商实际账单。"}
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+", help="已有的模型模式 study 报告 JSON（至少一份）")
    parser.add_argument("--campaign", type=Path, help="可选，只读的公开调用与费用保护台账 JSON")
    parser.add_argument("--output-dir", type=Path, required=True, help="必须尚不存在的新输出目录")
    args = parser.parse_args(argv)
    try:
        report_inputs = [_read_json(path) for path in args.reports]
        campaign_input = _read_json(args.campaign) if args.campaign else None
        summary = build_summary(report_inputs, campaign_input)
        markdown = render_markdown(summary)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        with (args.output_dir / "summary.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        with (args.output_dir / "真实模型实测报告.md").open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown)
    except (OSError, ValueError, TypeError) as error:
        print(f"无法生成汇总：{type(error).__name__}；{str(error) if isinstance(error, ValueError) else '请检查输入结构、文件路径与新目录要求。'}", file=sys.stderr)
        return 2
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), "report_count": len(summary["reports"]),
                      "network_requests_sent": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
