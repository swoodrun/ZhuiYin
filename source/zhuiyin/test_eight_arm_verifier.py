"""Offline negative probes of the fixed-study verifier, using copied fixtures."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools import verify_eight_arm as verifier


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "data/research/eight-arm-20261018"
OLD_POLICY = ROOT / "data/evaluations/v061-review-20261017/campaign-policy.json"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class EightArmVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eight-arm-verifier-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prepared, self.run = self.root / "prepared", self.root / "run"
        shutil.copytree(FIXTURE / "prepared", self.prepared)
        shutil.copytree(FIXTURE / "rehearsal", self.run)
        self.policy = self.root / "campaign-policy.json"
        self.ledger = self.root / "campaign-ledger.sqlite"
        shutil.copyfile(verifier.HISTORY, self.ledger)
        raw = load(OLD_POLICY)
        raw["ledger_path"] = self.ledger.as_posix()
        save(self.policy, raw)

    def rehash(self):
        manifest = {p.relative_to(self.run).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.run.rglob("*") if p.is_file() and p.name != "file-manifest.json"}
        save(self.run / "file-manifest.json", manifest)

    def read(self):
        return verifier.verify(self.run, self.prepared, self.policy)

    def assertRejected(self, match):
        result = self.read()
        self.assertFalse(result["passed"])
        self.assertEqual(result["conclusion_status"], "unverified")
        self.assertIsNone(result["arms"])
        self.assertIsNone(result["trials"])
        self.assertIsNone(result["accounting"])
        self.assertTrue(any(match in item for item in result["failed_checks"]), result["failed_checks"])

    def make_synthetic_real(self):
        """Offline-only fixture: no provider or model executor is involved."""
        policy = load(self.policy)
        policy["max_requests"] = 374
        save(self.policy, policy)
        public = {"schema_version": 1, "allowed_origin": "https://api.deepseek.com", "allowed_model": "deepseek-flash",
                  "max_requests": 374, "max_cost_cny": "10", "input_price_per_million": "3",
                  "output_price_per_million": "12", "currency": "CNY", "input_padding": 8192, "max_output_tokens": 1200}
        canonical = json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        design = load(self.prepared / "fixed-design.json")
        design["expected_campaign_policy_sha256"] = fingerprint
        save(self.prepared / "fixed-design.json", design)
        save(self.prepared / "design-sha256.json", {"sha256": hashlib.sha256(verifier.canonical(design)).hexdigest()})
        save(self.run / "fixed-design.json", design)
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("UPDATE metadata SET value=? WHERE key='policy_json'", (canonical,))
            db.execute("UPDATE metadata SET value=? WHERE key='policy_sha256'", (fingerprint,))
            for number in range(1, 17):
                db.execute("INSERT INTO requests(reservation_id,state,reserved_prompt_tokens,reserved_completion_tokens,"
                           "reserved_micros,charged_micros,actual_prompt_tokens,actual_completion_tokens) VALUES (?,?,?,?,?,?,?,?)",
                           (f"synthetic-{number:02d}", "settled", 8192, 1200, 38976, 420, 100, 10))
            db.commit()
        report = load(self.run / "study-report.json")
        report["mode"] = "llm"
        report["config"]["agent_mode"] = "llm"
        report["model_identity"] = design["model_identity"]
        index = load(self.run / "trial-index.json")
        index["evidence_mode"] = "real_model_attempts"
        save(self.run / "trial-index.json", index)
        for number, sample in enumerate(report["samples"], 1):
            path = self.run / f"trials/trial-{number:02d}.json"
            trial = load(path)
            factor = design["factor_design"]["manifests_by_prompt_policy"][sample["prompt_policy"]][sample["strategy"]]
            sample["factor_manifest"] = factor
            sample["model_usage"].update({"requests": 1, "successful_requests": 1, "failed_requests": 0,
                "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                "known_prompt_tokens": 100, "known_completion_tokens": 10,
                "usage_unknown_requests": 0, "estimated_cost": 0.00042})
            trial["sample"] = sample
            trial["result"]["agent_mode"] = "llm"
            trial["result"]["factor_manifest"] = factor
            trial["evidence_mode"] = "real_model_attempts"
            trial["request_records"] = [{"status": "ok", "prompt_tokens": 100, "completion_tokens": 10,
                                         "total_tokens": 110, "usage_available": True, "http_status": 200, "elapsed_ms": 1}]
            save(path, trial)
        report["campaign_budget"] = {"requests_made": 246, "requests_remaining": 128,
            "accounted_cost_cny": "0.619302", "policy_sha256": fingerprint}
        report["model_usage"].update({"requests": 16, "successful_requests": 16, "failed_requests": 0,
            "prompt_tokens": 1600, "completion_tokens": 160, "total_tokens": 1760,
            "known_prompt_tokens": 1600, "known_completion_tokens": 160,
            "usage_unknown_requests": 0, "estimated_cost": 0.00672, "external_model_verified": True})
        save(self.run / "study-report.json", report)
        state = load(self.run / "run-state.json")
        state.update({"mode": "llm", "new_model_requests": 16})
        save(self.run / "run-state.json", state)
        self.rehash()

    def test_rules_fixture_passes_without_network_or_input_changes(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for base in (self.run, self.prepared)
                  for p in base.rglob("*") if p.is_file()}
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network permitted")):
            result = self.read()
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(len(result["trials"]), 16)
        self.assertIn("不是真实模型", result["evidence_scope"])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_synthetic_real_ledger_matches_all_16_trials(self):
        self.make_synthetic_real()
        result = self.read()
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(result["accounting"]["new_requests"], 16)
        self.assertEqual(result["accounting"]["cumulative_requests"], 246)
        self.assertEqual(result["accounting"]["new_accounted_cost_cny"], "0.006720")
        self.assertEqual(len(result["arms"]), 8)
        self.assertEqual(result["ledger_input"]["kind"], "active_policy_path")

    def test_portable_frozen_snapshot_works_without_old_absolute_path_and_rejects_tampering(self):
        self.make_synthetic_real()
        snapshot = self.policy.parent / "campaign-ledger-final.sqlite"
        shutil.copyfile(self.ledger, snapshot)
        policy = load(self.policy)
        policy["ledger_path"] = (self.root / "unavailable-original-workspace/campaign-ledger.sqlite").as_posix()
        save(self.policy, policy)
        self.assertFalse(Path(policy["ledger_path"]).exists())
        result = self.read()
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(result["ledger_input"], {"kind": "frozen_snapshot", "file": "campaign-ledger-final.sqlite"})
        with closing(sqlite3.connect(snapshot)) as db:
            db.execute("UPDATE requests SET reservation_id='snapshot-tampered' WHERE sequence=1")
            db.commit()
        self.assertRejected("原230条逐字段保全")

    def test_failed_flag_contradiction_is_rejected_after_rehash(self):
        path = self.run / "trials/trial-01.json"
        trial = load(path)
        trial["result"]["failed"] = False
        save(path, trial)
        self.rehash()
        self.assertRejected("failed 汇总")

    def test_group_summary_contradiction_is_rejected_after_rehash(self):
        path = self.run / "study-report.json"
        report = load(path)
        report["arms"][0]["confirmed_completions"] = 2
        report["arms"][0]["confirmed_completion_rate"] = {"numerator": 2, "denominator": 2, "rate": 1, "ci95": [0, 1]}
        save(path, report)
        self.rehash()
        self.assertRejected("confirmed_completions")

    def test_database_change_is_rejected_even_with_valid_hash(self):
        with closing(sqlite3.connect(self.run / "trials/trial-01.sqlite")) as db:
            db.execute("UPDATE work_orders SET priority='urgent' WHERE id=1")
            db.commit()
        self.rehash()
        self.assertRejected("SQLite业务行")

    def test_original_230_rows_cannot_be_replaced(self):
        self.make_synthetic_real()
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("UPDATE requests SET reservation_id='rewritten' WHERE sequence=1")
            db.commit()
        self.assertRejected("原230条逐字段保全")

    def test_new_usage_must_match_ordered_ledger_suffix(self):
        self.make_synthetic_real()
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("UPDATE requests SET actual_prompt_tokens=101, charged_micros=423 WHERE sequence=231")
            db.commit()
        self.assertRejected("新请求231用量")

    def test_policy_metadata_contradiction_is_rejected(self):
        self.make_synthetic_real()
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("UPDATE metadata SET value='wrong' WHERE key='policy_sha256'")
            db.commit()
        self.assertRejected("累计台账政策")

    def test_late_reordered_trial_is_not_a_fixed_plan_prefix(self):
        path = self.run / "study-report.json"
        report = load(path)
        report["samples"][15]["trial_index"] = 1
        save(path, report)
        self.rehash()
        self.assertRejected("第16轮计划:trial_index")

    def test_partial_prefix_retains_missing_groups_without_replacement(self):
        self.make_synthetic_real()
        report = load(self.run / "study-report.json")
        report.update({"status": "partial", "completed": 8, "stopped_reason": "cancelled"})
        report["samples"] = report["samples"][:8]
        report["execution_order"] = report["execution_order"][:8]
        for arm in report["arms"]:
            for key in ("attempted", "valid_trials", "business_failures", "successes", "business_effect_failures",
                        "report_errors", "uncertain_reports", "confirmed_completions"):
                arm[key] //= 2
            arm["unrun"] = 1
            arm["report_outcomes"] = {key: value // 2 for key, value in arm["report_outcomes"].items()}
            for key in ("business_failure_rate", "task_completion_rate", "business_effect_failure_rate", "report_error_rate",
                        "uncertain_report_rate", "confirmed_completion_rate", "attempted_completion_rate"):
                arm[key] = verifier.rate(arm[key]["numerator"] // 2, 1)
        for key in ("requests", "successful_requests", "prompt_tokens", "completion_tokens", "total_tokens",
                    "known_prompt_tokens", "known_completion_tokens"):
            report["model_usage"][key] //= 2
        report["model_usage"]["estimated_cost"] = 0.00336
        report["campaign_budget"].update({"requests_made": 238, "requests_remaining": 136, "accounted_cost_cny": "0.615942"})
        save(self.run / "study-report.json", report)
        for number in range(9, 17):
            (self.run / f"trials/trial-{number:02d}.json").unlink()
            (self.run / f"trials/trial-{number:02d}.sqlite").unlink()
        index = load(self.run / "trial-index.json")
        index["trials"] = index["trials"][:8]
        save(self.run / "trial-index.json", index)
        state = load(self.run / "run-state.json")
        state.update({"status": "partial", "new_model_requests": 8, "trials_indexed": 8,
                      "trial_json_files_retained": 8, "trial_databases_retained": 8})
        save(self.run / "run-state.json", state)
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("DELETE FROM requests WHERE sequence>238")
            db.execute("UPDATE sqlite_sequence SET seq=238 WHERE name='requests'")
            db.commit()
        self.rehash()
        result = self.read()
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(len(result["trials"]), 8)
        self.assertTrue(all(arm["unrun"] == 1 and arm["attempted"] == 1 for arm in result["arms"]))

    def test_unknown_usage_preserves_partial_token_totals(self):
        options = {"input_price_per_million": 3, "output_price_per_million": 12}
        actual = verifier.usage_facts([{"status": "invalid_response", "prompt_tokens": 12,
                                      "completion_tokens": None, "usage_available": False}], options)
        self.assertIsNone(actual["estimated_cost"])
        self.assertIsNone(actual["prompt_tokens"])
        self.assertEqual(actual["known_prompt_tokens"], 12)
        self.assertEqual(actual["usage_unknown_requests"], 1)

    def test_network_error_retains_database_without_becoming_a_valid_success(self):
        self.make_synthetic_real()
        report = load(self.run / "study-report.json")
        path = self.run / "trials/trial-04.json"
        trial = load(path)
        sample, result = trial["sample"], trial["result"]
        violations = [{"code": "AGENT_DID_NOT_FINISH"}, {"code": "MODEL_EXECUTION_ERROR"}]
        changes = {"completed": False, "reported_outcome": None, "reported_success": None,
                   "report_assessment": "unreported", "failed": True,
                   "failure_fingerprint": "AGENT_DID_NOT_FINISH|MODEL_EXECUTION_ERROR",
                   "violations": violations, "execution_violations": violations,
                   "business_failed": False, "report_failed": False, "business_violations": [], "reporting_violations": []}
        for source in (sample, result):
            source.update(deepcopy(changes))
            source["events"] = [event for event in source["events"] if event["tool"] != "finish_task"]
        sample["status"] = "infra_error"
        sample["model_usage"].update({"requests": 1, "successful_requests": 0, "failed_requests": 1,
            "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            "known_prompt_tokens": 0, "known_completion_tokens": 0, "usage_unknown_requests": 1, "estimated_cost": None})
        trial["request_records"] = [{"status": "network_error", "prompt_tokens": None, "completion_tokens": None,
                                     "total_tokens": None, "usage_available": False, "http_status": None, "elapsed_ms": 1}]
        report["samples"][3] = sample
        report["execution_order"][3]["status"] = "infra_error"
        arm = next(a for a in report["arms"] if a["arm_id"] == "neutral:verified")
        arm.update({"valid_trials": 1, "successes": 1, "confirmed_completions": 1, "infra_errors": 1,
                    "report_outcomes": {"success": 1, "failure": 0, "unknown": 0}})
        for key in ("business_failure_rate", "business_effect_failure_rate", "report_error_rate", "uncertain_report_rate"):
            arm[key] = verifier.rate(0, 1)
        for key in ("task_completion_rate", "confirmed_completion_rate"):
            arm[key] = verifier.rate(1, 1)
        arm["attempted_completion_rate"] = verifier.rate(1, 2)
        report["model_usage"].update({"successful_requests": 15, "failed_requests": 1,
            "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            "known_prompt_tokens": 1500, "known_completion_tokens": 150,
            "usage_unknown_requests": 1, "estimated_cost": None})
        report["campaign_budget"]["accounted_cost_cny"] = "0.657858"
        save(path, trial)
        save(self.run / "study-report.json", report)
        index = load(self.run / "trial-index.json")
        index["trials"][3]["status"] = "infra_error"
        save(self.run / "trial-index.json", index)
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("UPDATE requests SET state='unknown',actual_prompt_tokens=NULL,actual_completion_tokens=NULL,"
                       "charged_micros=38976 WHERE sequence=234")
            db.commit()
        self.rehash()
        checked = self.read()
        self.assertTrue(checked["passed"], checked["failed_checks"])
        checked_arm = next(a for a in checked["arms"] if a["arm_id"] == "neutral:verified")
        self.assertEqual(checked_arm["valid_trials"], 1)
        self.assertEqual(checked_arm["infrastructure_error_rate"]["rate"], 0.5)
        self.assertTrue(checked["trials"][3]["actual_success"])
        self.assertEqual(checked["trials"][3]["status"], "infra_error")

    def test_configured_fault_does_not_imply_activation(self):
        original = load(self.run / "trials/trial-04.json")
        result = verifier.activations(original["result"]["events"])
        self.assertTrue(result["commit_response_lost"])
        self.assertFalse(result["stale_read"])

    def test_untrusted_sql_text_is_never_executed(self):
        path = self.run / "trials/trial-01.json"
        trial = load(path)
        trial["result"]["sqlite_dump"] = "ATTACH DATABASE 'must-not-be-created.sqlite' AS evil;"
        save(path, trial)
        self.rehash()
        result = self.read()
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertFalse((self.root / "must-not-be-created.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
