"""Exercise diagnoses against real transactions and malformed saved evidence."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import diagnostics
import engine


ROOT = Path(__file__).resolve().parent
BUNDLED_NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
NODE = shutil.which("node") or (str(BUNDLED_NODE) if BUNDLED_NODE.is_file() else None)


class DiagnosticsTests(unittest.TestCase):
    def test_duplicate_and_false_report_link_to_saved_evidence(self):
        cfg = engine.Config(faults=["commit_response_lost", "stale_read", "latency"])
        trial = engine.run_trial(cfg, cfg.faults)
        before = copy.deepcopy(trial)
        result = diagnostics.diagnose_trial(trial, cfg.public())
        self.assertEqual(result["actual_success"], False)
        duplicate = next(row for row in result["findings"] if row["code"] == "DUPLICATE_RESERVATION")
        self.assertEqual(len(duplicate["record_indexes"]), 2)
        self.assertTrue(duplicate["event_indexes"])
        for index in duplicate["event_indexes"]:
            self.assertGreater(len(result["events"][index]["actual"]["reservations"]), 1)
        mismatch = next(row for row in result["findings"] if row["code"] == "REPORT_MISMATCH")
        self.assertEqual(result["events"][mismatch["event_indexes"][0]]["tool"], "finish_task")
        self.assertEqual({row["id"] for row in result["activated_faults"]}, set(cfg.faults))
        self.assertEqual(result["events"][result["first_anomaly_index"]]["kind"], "fault")
        self.assertEqual(trial, before)

    def test_configured_fault_without_activation_is_not_called_triggered(self):
        cfg = engine.Config(faults=["commit_response_lost", "stale_read"])
        trial = engine.run_trial(cfg, cfg.faults, "verified")
        result = diagnostics.diagnose_trial(trial, cfg.public())
        self.assertEqual({row["id"] for row in result["configured_faults"]}, set(cfg.faults))
        self.assertEqual([row["id"] for row in result["activated_faults"]], ["commit_response_lost"])

    def test_all_fault_kinds_have_executed_evidence(self):
        observed = set()
        for fault in engine.FAULTS:
            trial = engine.run_trial(engine.Config(scenario="ticket"), [fault])
            result = diagnostics.diagnose_trial(trial)
            observed.update(row["id"] for row in result["activated_faults"])
        pair = engine.run_trial(engine.Config(scenario="ticket"), ["commit_response_lost", "stale_read"])
        observed.update(row["id"] for row in diagnostics.diagnose_trial(pair)["activated_faults"])
        self.assertEqual(observed, set(engine.FAULTS))

    def test_not_run_errors_and_unknown_never_become_a_pass(self):
        not_run = diagnostics.diagnose_trial({"status": "not_run", "actual_success": True, "skipped": True})
        self.assertEqual(not_run["execution_status"], "not_run")
        self.assertIsNone(not_run["actual_success"])
        with patch.object(engine, "llm_agent", side_effect=RuntimeError("synthetic offline failure")):
            interrupted = engine.run_trial(engine.Config(), [], agent_mode="llm")
        error = diagnostics.diagnose_trial(interrupted)
        self.assertEqual(error["execution_status"], "incomplete")
        self.assertIsNone(error["actual_success"])
        unknown = diagnostics.diagnose_trial({"completed": True, "actual_success": True, "reported_outcome": "unknown", "report_assessment": "uncertain"})
        self.assertEqual(unknown["execution_status"], "completed")
        self.assertEqual(unknown["reported_outcome"], "unknown")
        self.assertEqual(unknown["findings"][0]["code"], "UNCERTAIN_REPORT")

    def test_static_snapshot_is_not_independently_measured_business_truth(self):
        raw = {"analysis": {"actual_success": True, "reported_outcome": "success", "static_evaluation": {"evidence_complete": False}}}
        result = diagnostics.analyze_report(raw)["trials"][0]
        self.assertEqual(result["scope"], "static_import_only")
        self.assertIsNone(result["actual_success"])
        self.assertIn("静态", result["status"]["business"])

    def test_primary_collection_avoids_pooled_reduction_and_control_trials(self):
        good = engine.run_trial(engine.Config(), [])
        raw = {"result": {"config": {"faults": []}, "baseline": good, "fixed": good,
                          "minimization": {"initial_runs": [good] * 3}, "regression": {"cases": [good] * 16}}}
        result = diagnostics.analyze_report(raw)
        self.assertEqual(result["counts"]["total"], 2)
        self.assertEqual([row["path"] for row in result["trials"]], ["report.result.baseline", "report.result.fixed"])
        suite = {"cases": [{"name": "a", "config": {"faults": []}, "strategies": {"baseline": good, "verified": good}},
                           {"name": "b", "status": "not_run"}]}
        result = diagnostics.analyze_report(suite)
        self.assertEqual(result["counts"], {"total": 3, "completed": 2, "incomplete": 0, "not_run": 1, "unknown": 0})
        study = diagnostics.analyze_report({"samples": [{**good, "trial_index": 2, "strategy": "baseline"}]})
        self.assertEqual(len(study["trials"]), 1)
        self.assertIn("第2轮", study["trials"][0]["label"])

    def test_structured_secrets_and_sql_are_not_in_diagnostic_evidence(self):
        raw = {"completed": True, "events": [{"observed": {"api_key": "SECRET_DIRECT", "headers": [{"name": "Authorization", "value": "SECRET_HEADER"}], "request_id": "business-id", "total_tokens": 3, "sqlite_dump": "SECRET_SQL"}}]}
        result = diagnostics.diagnose_trial(raw)
        encoded = json.dumps(result)
        self.assertNotIn("SECRET", encoded)
        self.assertIn("business-id", encoded)
        self.assertIn("total_tokens", encoded)

    def test_legacy_batch_result_descends_to_primary_trials_with_evidence(self):
        cfg = engine.Config(faults=["commit_response_lost", "stale_read"])
        baseline = engine.run_trial(cfg, cfg.faults)
        fixed = engine.run_trial(cfg, cfg.faults, "verified")
        raw = {"kind": "batch", "cases": [{"name": "历史借用场景", "config": cfg.public(),
                "result": {"baseline": baseline, "fixed": fixed, "minimization": {"initial_runs": [baseline] * 4}}}]}
        before = copy.deepcopy(raw)
        result = diagnostics.analyze_report(raw)
        self.assertEqual(result["counts"], {"total": 2, "completed": 2, "incomplete": 0, "not_run": 0, "unknown": 0})
        first, second = result["trials"]
        self.assertEqual(first["path"], "report.cases[0].result.baseline")
        self.assertEqual(first["label"], "历史借用场景 · 原始执行")
        self.assertEqual(second["path"], "report.cases[0].result.fixed")
        self.assertFalse(first["actual_success"])
        self.assertTrue(second["actual_success"])
        self.assertTrue(first["events"])
        self.assertEqual(len(first["records"]), 2)
        self.assertEqual({row["id"] for row in first["activated_faults"]}, set(cfg.faults))
        self.assertEqual(raw, before)

    def test_legacy_batch_partial_result_remains_incomplete(self):
        raw = {"cases": [{"name": "历史中断场景", "partial_result": {"baseline": {
            "completed": False, "actual_success": True, "reported_outcome": None,
            "events": [{"kind": "fault", "observed": {"delay_ms": 15}}]}}}]}
        result = diagnostics.analyze_report(raw)
        self.assertEqual(result["counts"]["incomplete"], 1)
        self.assertEqual(result["trials"][0]["path"], "report.cases[0].partial_result.baseline")
        self.assertIsNone(result["trials"][0]["actual_success"])
        self.assertEqual(len(result["trials"][0]["events"]), 1)

    def test_empty_and_legacy_reports_keep_unknown_fields(self):
        result = diagnostics.analyze_report({})["trials"][0]
        self.assertEqual(result["execution_status"], "unknown")
        self.assertIsNone(result["configured_faults"])
        self.assertIsNone(result["actual_success"])
        legacy = diagnostics.diagnose_trial({"reported_success": False, "actual_success": False})
        self.assertEqual(legacy["reported_outcome"], "failure")
        self.assertIn("未记录", legacy["status"]["assessment"])

    @unittest.skipUnless(NODE, "Node is optional for frontend contracts")
    def test_real_reports_match_frontend_analyzer(self):
        cfg = engine.Config(faults=["commit_response_lost", "stale_read"])
        baseline = engine.run_trial(cfg, cfg.faults)
        fixed = engine.run_trial(cfg, cfg.faults, "verified")
        fixtures = [{"config": cfg.public(), "baseline": baseline, "fixed": fixed},
                    {"samples": [{**baseline, "trial_index": 1, "prompt_policy": "neutral"}, {**fixed, "trial_index": 1, "prompt_policy": "neutral"}]},
                    {"cases": [{"name": "合成批次", "strategies": {"baseline": baseline}}, {"name": "未运行", "status": "not_run"}, {"analysis": {"actual_success": True, "reported_outcome": "success"}}]},
                    {"cases": [{"name": "旧批量场景", "config": cfg.public(), "result": {"baseline": baseline, "fixed": fixed}},
                               {"name": "旧批量报告", "report": {"baseline": fixed}},
                               {"name": "旧批量中断", "partial_result": {"baseline": {"completed": False, "actual_success": None, "events": []}}}]},
                    {}, {"actual_success": False, "reported_success": False}]
        script = "const a=require(process.argv[1]);const f=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(f.map(a.analyzeReport)));"
        completed = subprocess.run([NODE, "-e", script, str(ROOT / "static/diagnostics.js")], input=json.dumps(fixtures),
                                   text=True, encoding="utf-8", capture_output=True, check=True, timeout=15)
        self.assertEqual(json.loads(completed.stdout), [diagnostics.analyze_report(item) for item in fixtures])

    @unittest.skipUnless(NODE, "Node is optional for frontend contracts")
    def test_frontend_navigation_and_text_only_rendering(self):
        completed = subprocess.run([NODE, str(ROOT / "tools/check_diagnostics.js")], text=True, encoding="utf-8",
                                   capture_output=True, check=True, timeout=15)
        self.assertIn("passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
