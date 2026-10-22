"""Continue the approved campaign without changing the frozen v0.5 evidence."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
OLD = ROOT / "data/evaluations/v05-deepseek-20261017"
OUT = ROOT / "data/evaluations/v06-protocol-20261017"


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
        save("campaign-initial.json", summary)
        save("experiment-plan.json", {
            "declared_at_utc": datetime.now(timezone.utc).isoformat(),
            "evaluation_protocol": "business-report-v2", "version": "0.6.0",
            "purpose": "Exploratory real-model same-full-fingerprint fault minimization with full trial evidence; not a population efficacy estimate.",
            "previous_requests": 184, "approved_total_requests": 360, "approved_total_cost_cny": 10,
            "prior_runs": ["18ae71b49129", "04f677e8b0dd"],
            "model": "deepseek-flash", "origin": "https://api.deepseek.com", "thinking": "disabled",
            "prompt_policy": "neutral", "scenario": "ticket", "seed": 42,
            "cases_in_order": [
                {"case_id": "v06-same-error-001", "faults": ["latency", "corrupt_payload"], "max_requests": 120,
                 "rationale": "A damaged committed priority is a business error; latency is a potentially removable condition. Detection is not assumed to repair the row."},
                {"case_id": "v06-same-error-002", "faults": ["latency", "ack_without_commit"], "max_requests": 120,
                 "rationale": "Fallback exploratory case, only if case 1 has no complete stable counterexample and at least 54 total requests remain."}],
            "repeats": 2,
            "target": "Exact full sorted violation-code fingerprint from the first completed baseline; never replace target with another error.",
            "protocol": "Each deletion group repeats twice. Accept only 2/2 same fingerprint. Restart deletions after acceptance; retain every candidate event and actual database state. Independently revalidate final combination twice. Separately run combined capabilities on original faults under the same neutral prompt.",
            "stop": "Stop after first complete stable reduction; otherwise preserve all attempts. No repeated search after the two declared cases. Infrastructure errors, unstable initial/final fingerprints, unknown reports, and budget stops remain explicit. Never claim a global minimum or deterministic model guarantee.",
            "factorial_scope": "Local verified 2x2 tool-capability design layered by neutral/recovery instructions; no new paid 8-arm efficacy study is claimed in this run.",
            "billing": "Continue all previous reservations and charges; no reset. Conservative rates are accounting assumptions, not a provider invoice."
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
