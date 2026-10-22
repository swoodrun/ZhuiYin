"""Agent batch execution with one thread-local budget across all uploaded tasks."""
import copy

import engine
from run_billing import RunBudget, summarize_usage
from test_suite import execute_suite


def execute_agent_suite(plan, output, config, progress_callback=None, should_stop=None):
    billing = copy.deepcopy(getattr(config, "billing", {}))
    guard = RunBudget(billing) if billing else None
    sentinel = object()
    previous = {key: getattr(engine.MODEL_CONTEXT, key, sentinel) for key in engine.MODEL_CONTEXT_FIELDS}
    ctx = engine.MODEL_CONTEXT
    ctx.remaining = ctx.request_limit = config.max_requests
    ctx.cancel_check = should_stop
    ctx.usage_records = []
    ctx.budget_exhausted = False
    ctx.campaign_stopped_reason = None
    ctx.run_budget = guard
    ctx.cost_stopped_reason = None
    stop_reason = None

    def stop():
        nonlocal stop_reason
        if should_stop and should_stop():
            stop_reason = "cancelled"
        elif ctx.cost_stopped_reason:
            stop_reason = ctx.cost_stopped_reason
        elif ctx.campaign_stopped_reason:
            stop_reason = ctx.campaign_stopped_reason
        elif ctx.budget_exhausted or ctx.remaining <= 0:
            stop_reason = "request_budget_exhausted"
        return bool(stop_reason)

    def trial(cfg, faults, strategy, mode):
        if mode != "llm":
            raise ValueError("Agent batch cannot execute rules")
        before = len(ctx.usage_records)
        result = engine.run_trial(cfg, faults, strategy, "llm")
        result["agent_spec"] = engine.agent_spec(cfg, strategy)
        result["model_identity"] = engine.model_identity(cfg)
        records = copy.deepcopy(ctx.usage_records[before:])
        result["model_usage"] = {**(summarize_usage(records, billing) if billing else {}),
                                 "requests": len(records), "records": records}
        if billing:
            result["model_usage"]["billing"] = copy.deepcopy(billing)
        return result

    def publish(report):
        report["model_identity"] = engine.model_identity(config)
        report["prompt_policy"] = config.prompt_policy
        report["model_usage"] = {**(summarize_usage(ctx.usage_records, billing) if billing else {}),
                                 "requests": len(ctx.usage_records),
                                 "limit": config.max_requests, "remaining": ctx.remaining,
                                 "records": copy.deepcopy(ctx.usage_records)}
        if guard is not None:
            report["billing"] = copy.deepcopy(billing)
            report["run_budget"] = guard.summary()
            report["model_usage"].update(billing=copy.deepcopy(billing), run_budget=guard.summary())
        if (stop_reason == "request_budget_exhausted" and not ctx.budget_exhausted
                and all(case["status"] in {"completed", "duplicate", "conflict", "import_error"} for case in report["cases"])
                and report.get("status") != "running"):
            report.update(status="completed", completed=True, cancelled=False)
        elif stop_reason and report.get("status") != "running":
            # The shared executor treats its stop hook as cancellation; distinguish
            # a budget stop without pretending the remaining tasks were executed.
            report.update(status="cancelled" if stop_reason == "cancelled" else "partial",
                          completed=False, cancelled=stop_reason == "cancelled", stopped_reason=stop_reason)
            for case in report["cases"]:
                if case.get("stopped_reason") == "cancelled":
                    case["stopped_reason"] = stop_reason
                for value in case.get("strategies", {}).values():
                    if value.get("stopped_reason") == "cancelled":
                        value["stopped_reason"] = stop_reason

    try:
        return execute_suite({**plan, "profile": "agent"}, output, progress_callback=progress_callback,
                             should_stop=stop, quick_runner=trial, agent_config=config, report_transform=publish,
                             stop_reason=lambda: stop_reason or "cancelled")
    finally:
        for key, value in previous.items():
            if value is sentinel:
                if hasattr(ctx, key):
                    delattr(ctx, key)
            else:
                setattr(ctx, key, value)
