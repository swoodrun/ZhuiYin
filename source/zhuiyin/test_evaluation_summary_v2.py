"""Exercise real evaluation results through compact reports and page helpers."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import engine


ROOT = Path(__file__).resolve().parent
BUNDLED_NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
NODE = shutil.which("node") or (str(BUNDLED_NODE) if BUNDLED_NODE.is_file() else None)


class EvaluationSummaryTests(unittest.TestCase):
    def test_unknown_and_violations_keep_original_values_without_private_payloads(self):
        lab = engine.Lab(engine.Config(), ["corrupt_payload"], "baseline")
        try:
            lab.reserve()
            lab.finish(outcome="unknown", reason="no assertion")
            trial = lab.result("llm")
        finally:
            lab.db.close()
        trial["model_config"] = {"api_key": "not-public"}
        summary = engine.evaluation_summary(trial)
        for key in ("evaluation_protocol", "prompt_policy", "completed", "reported_outcome",
                    "report_assessment", "reported_success", "actual_success", "business_failed",
                    "report_failed", "business_violations", "reporting_violations", "execution_violations"):
            self.assertEqual(summary[key], trial[key], key)
        self.assertTrue(summary["completed"])
        self.assertTrue(summary["business_failed"])
        self.assertFalse(summary["report_failed"])
        self.assertIsNone(summary["reported_success"])
        self.assertEqual(summary["report_assessment"], "uncertain")
        self.assertTrue({"model_config", "events", "sqlite_dump", "business_records"}.isdisjoint(summary))
        summary["business_violations"][0]["code"] = "mutated-copy"
        self.assertNotEqual(summary["business_violations"], trial["business_violations"])

    def test_legacy_summary_does_not_acquire_v2_fields(self):
        legacy = {"failed": False, "failure_fingerprint": "", "actual_success": True,
                  "reported_success": True, "stats": {"tool_calls": 2}, "violations": []}
        self.assertEqual(engine.evaluation_summary(legacy), legacy)
        for key in ("evaluation_protocol", "reported_outcome", "report_assessment", "business_failed"):
            self.assertNotIn(key, engine.evaluation_summary(legacy))

    def test_unfinished_baseline_retains_evidence_and_usage_without_downstream_runs(self):
        def unfinished(lab, model_config):
            lab.reserve()
            engine.MODEL_CONTEXT.remaining -= 1
            engine.MODEL_CONTEXT.usage_records.append({"usage": {"prompt_tokens": 12, "completion_tokens": 4}})
            raise RuntimeError("offline interruption fixture")

        cfg = engine.Config(faults=[], agent_mode="llm", model_config={"api_key": "private-fixture"})
        original_trial = engine.run_trial
        with patch.object(engine, "llm_agent", new=unfinished), \
                patch.object(engine, "run_trial", wraps=original_trial) as calls, \
                self.assertRaises(engine.IncompleteBaselineError) as caught:
            engine.run_experiment(cfg)
        self.assertEqual(calls.call_count, 1)
        error = caught.exception
        self.assertIsInstance(error, RuntimeError)
        partial = error.partial_result
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["stopped_reason"], "model_execution_error")
        self.assertFalse(partial["completed"])
        self.assertIsNone(partial["baseline"]["failed"])
        self.assertTrue(partial["baseline"]["actual_success"])
        self.assertTrue(partial["baseline"]["events"])
        self.assertTrue(partial["baseline"]["sqlite_dump"])
        self.assertEqual(len(partial["baseline"]["business_records"]), 1)
        self.assertEqual(partial["fixed"]["status"], "not_run")
        self.assertEqual(partial["minimization"]["trials"], 0)
        self.assertEqual(partial["model_usage"]["requests"], 1)
        self.assertEqual(partial["model_usage"]["requests_by_phase"], {"baseline": 1, "minimization": 0, "fixed": 0})
        self.assertNotIn("private-fixture", json.dumps(partial))
        self.assertNotIn("api_key", partial["config"])
        for panel in ("regression", "ablation", "expanded_regression"):
            self.assertFalse(partial[panel]["completed"])


@unittest.skipUnless(NODE, "Node is needed for the actual frontend helper regression")
class EvaluationPagePipelineTests(unittest.TestCase):
    def views(self, values, helper="trial"):
        script = ("const api=require(process.argv[1]);"
                  "const data=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "process.stdout.write(JSON.stringify(data.map(x=>api[process.argv[2]](x))));")
        completed = subprocess.run([NODE, "-e", script, str(ROOT / "static/outcome.js"), helper],
            input=json.dumps(values), text=True, encoding="utf-8", capture_output=True, check=True, timeout=20)
        return json.loads(completed.stdout)

    def test_all_32_real_expanded_results_reach_page_as_modern(self):
        report = engine.run_experiment(engine.Config(faults=[]))
        summaries = []
        for case in report["expanded_regression"]["cases"]:
            fixture = engine.Config.from_dict(case["config"])
            for strategy, summary in case["strategies"].items():
                rerun = engine.run_trial(fixture, case["faults"], strategy, "rules")
                self.assertEqual(summary, engine.evaluation_summary(rerun))
                summaries.append(summary)
        self.assertEqual(len(summaries), 32)
        views = self.views(summaries)
        self.assertTrue(all(view["modern"] for view in views))
        self.assertTrue(all("原口径" not in json.dumps(view, ensure_ascii=False) for view in views))
        for result, view in zip(summaries, views):
            if result["business_failed"]:
                self.assertEqual(view["badgeLabel"], "业务违规")
            elif result["report_failed"]:
                self.assertEqual(view["badgeLabel"], "报告不一致")
        old = {"failed": False, "actual_success": True, "reported_success": True}
        old_view = self.views([engine.evaluation_summary(old)])[0]
        self.assertFalse(old_view["modern"])
        self.assertIn("原口径", old_view["badgeLabel"])

    def test_import_source_claims_cannot_replace_static_conclusion_or_unknown(self):
        source = {"evaluation_protocol": engine.EVALUATION_PROTOCOL, "actual_success": True,
                  "business_failed": False, "report_failed": False,
                  "reported_outcome": "success", "report_assessment": "consistent"}
        static = {"actual_success": False, "business_failed": True, "report_failed": True,
                  "report_assessment": "inconsistent", "scope": "quantity_and_expected_fields",
                  "evidence_complete": True}
        analysis = {**source, "source_evaluation": source, "static_evaluation": static,
                    "import_verification": "static-business-v2.1"}
        view = self.views([analysis], "imported")[0]
        self.assertEqual(view["staticView"]["actualLabel"], "否")
        self.assertEqual(view["staticView"]["badgeLabel"], "业务违规")
        self.assertEqual(view["sourceView"]["actualLabel"], "是")
        self.assertEqual(view["sourceView"]["badgeLabel"], "未记录")
        static.update(actual_success=None, business_failed=None, report_failed=None,
                      report_assessment="unverifiable", scope="quantity_only", evidence_complete=False)
        analysis["final_state"] = {"actual_success": True}
        view = self.views([analysis], "imported")[0]
        self.assertEqual(view["staticView"]["actualLabel"], "未知")
        self.assertEqual(view["staticView"]["assessmentLabel"], "证据不足，无法核对")
        self.assertEqual(view["staticView"]["badgeKind"], "warn")
        self.assertEqual(view["sourceView"]["actualLabel"], "是")
        self.assertEqual(view["evidenceLabel"], "不足")

    def test_real_damaged_honest_failure_import_reaches_page_without_report_error(self):
        from ingest import parse_upload

        cfg = engine.Config(scenario="ticket", request_id="summary-damage", ticket_title="损坏导入贯通",
                            ticket_priority="normal", faults=["corrupt_payload"])
        lab = engine.Lab(cfg, cfg.faults, "baseline")
        try:
            lab.reserve()
            lab.finish(outcome="failure", reason="wrong priority")
            trial = lab.result("llm")
        finally:
            lab.db.close()
        trial.pop("actual_success")
        report = {"evaluation_protocol": engine.EVALUATION_PROTOCOL, "mode": "llm", "config": cfg.public(),
                  "status": "finished", "completed": True, "baseline": trial, "fixed": trial}
        analyses = parse_upload("damaged-report.json", json.dumps(report))["analyses"]
        views = self.views(analyses, "imported")
        for analysis, view in zip(analyses, views):
            self.assertEqual(analysis["static_evaluation"]["report_assessment"], "consistent")
            self.assertFalse(analysis["static_evaluation"]["report_failed"])
            self.assertNotIn("REPORT_MISMATCH", {finding["code"] for finding in analysis["findings"]})
            self.assertEqual(view["staticView"]["actualLabel"], "否")
            self.assertEqual(view["staticView"]["reportLabel"], "失败")
            self.assertEqual(view["staticView"]["assessmentLabel"], "与证据一致")
            self.assertEqual(view["staticView"]["badgeLabel"], "业务违规")


if __name__ == "__main__":
    unittest.main()
