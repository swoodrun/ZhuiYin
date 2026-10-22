"""Continue the existing approved budget; freeze a single exploratory case."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
OLD = ROOT / "data/evaluations/v06-protocol-20261017"
OUT = ROOT / "data/evaluations/v061-review-20261017"


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    ledger = OUT / "campaign-ledger.sqlite"
    if not ledger.exists():
        with sqlite3.connect(f"file:{(OLD / 'campaign-ledger.sqlite').as_posix()}?mode=ro", uri=True) as source:
            with sqlite3.connect(ledger) as target:
                source.backup(target)
    policy = json.loads((OLD / "campaign-policy.json").read_text(encoding="utf-8"))
    policy["ledger_path"] = ledger.as_posix()
    save("campaign-policy.json", policy)
    os.environ["ZHUIYIN_CAMPAIGN_POLICY"] = str(OUT / "campaign-policy.json")
    from model_budget import public_summary
    summary = public_summary()
    if not (OUT / "campaign-initial.json").exists():
        if summary["requests_made"] != 222:
            raise RuntimeError("累计消耗已变化，请先重新核对计划，不能重置台账")
        save("campaign-initial.json", summary)
        save("experiment-plan.json", {
            "declared_at_utc": datetime.now(timezone.utc).isoformat(),
            "app_version": "0.6.1", "evaluation_protocol": "business-report-v2",
            "purpose": "One exploratory interaction case, not a population or causal claim.",
            "model": "deepseek-flash", "origin": "https://api.deepseek.com", "thinking": "disabled",
            "config": {"scenario": "ticket", "ticket_title": "教学楼门禁故障", "ticket_priority": "normal",
                       "request_id": "v061-interaction-001", "seed": 42, "agent_mode": "llm",
                       "prompt_policy": "neutral", "faults": ["commit_response_lost", "stale_read"],
                       "max_requests": 108},
            "max_requests_per_trial": 9, "repeats": 2,
            "request_bound": "2 faults permit at most 3 deletion groups: baseline1+initial2+candidates6+final2+combined1=12 trials, at most108 requests. If both conditions are retained, 10 trials at most90 requests.",
            "target": "The first completed baseline's exact sorted full violation-code fingerprint. Never replace the target with a later error.",
            "deletion": "Accept only 2/2 complete same-fingerprint reruns, restart after acceptance, then independently revalidate final set twice. Preserve every attempted event, database state, usage and deletion boundary.",
            "activation_evidence": ["commit_response_lost actually emitted COMMIT_UNKNOWN after commit",
                "stale_read actually emitted stale empty query", "write retried after stale empty query",
                "physical writes and single-deletion outcome fingerprints"],
            "stop": "Exactly one predeclared case. No fallback, no rerun to hunt for a successful reduction. No baseline error, drift, unfinished baseline, infrastructure failure, cancellation and budget stops all remain in the record. Service/cost failures can prevent completion despite the request bound.",
            "budget": {"already_used_requests": 222, "remaining_before_case": 138,
                       "approved_total_requests": 360, "approved_total_cost_cny": "10",
                       "case_request_limit": 108, "campaign_history_reset": False}
        })
        save("eight-arm-preregistration.json", {
            "declared_at_utc": datetime.now(timezone.utc).isoformat(), "status": "not_run_budget_insufficient",
            "model": "deepseek-flash", "thinking": "disabled", "evaluation_protocol": "business-report-v2",
            "scenario": "ticket", "ticket_title": "教学楼门禁故障", "ticket_priority": "normal",
            "faults": ["commit_response_lost", "stale_read"], "seed": 42,
            "strategies": ["baseline", "idempotency_only", "verification_only", "verified"],
            "prompt_policies": ["neutral", "recovery"], "repetitions": 2, "order_mode": "balanced",
            "primary_metric": "confirmed_completion_rate: actual DB completion and explicitly reported success / valid trials",
            "secondary_metrics": ["task_completion_rate", "business_effect_failure_rate", "report_error_rate",
                                  "uncertain_report_rate", "attempted_completion_rate", "infrastructure_error_rate"],
            "planned_business_trials": 16, "requests_per_trial_upper_bound": 9,
            "worst_case_requests": 144, "available_before_interaction_case": 138,
            "decision": "Pre-register only in this round: even before the interaction case the current allowance cannot guarantee144 requests. Do not lower the per-trial bound to fit or mix old-protocol samples. No calls are authorized above the existing total360 limit.",
            "interpretation": "Capability descriptions are visible to the model. This compares capability-informed tool schemes, not a blind change of the database. Separate policy/strategy denominators; Wilson intervals descriptive only. Two repetitions do not establish superiority, interactions or independent observations.",
            "retention": "Retain all attempted trial traces, actual order and failures; unrun samples remain unrun."
        })
    return summary


if __name__ == "__main__":
    summary = prepare()
    if "--start" in sys.argv:
        log = (OUT / "server.log").open("ab")
        process = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", "8766"],
            cwd=ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        print(json.dumps({"pid": process.pid, "campaign": summary}, ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False))
