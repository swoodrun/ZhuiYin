"""Export/import contracts for full, partial and unrun single experiments."""
import copy
import json
import unittest
from unittest.mock import patch

import engine
from ingest import parse_upload
from test_experiment_budget import SyntheticModel, config


class ReportRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.complete = engine.run_experiment(engine.Config())
        cls.budget_reports = {}
        for budget in (7, 20):
            transport = SyntheticModel()
            with patch("engine.urllib.request.build_opener", return_value=transport):
                cls.budget_reports[budget] = engine.run_experiment(config(max_requests=budget))
            if len(transport.requests) > budget:
                raise AssertionError("Synthetic transport exceeded its budget")

    def import_report(self, report):
        # Re-import is a static analysis. Neither exported SQL nor an uploaded
        # model connection may be used, even for a model-mode source report.
        with patch("engine.sqlite3.connect", side_effect=AssertionError("No database during import")), \
                patch("engine.model_request", side_effect=AssertionError("No model during import")):
            return parse_upload("report.json", json.dumps(report, ensure_ascii=False))

    def test_complete_export_reimports_both_business_results(self):
        parsed = self.import_report(self.complete)
        baseline, fixed = parsed["analyses"]
        self.assertEqual(baseline["verdict"], "violation")
        self.assertEqual(fixed["verdict"], "no_violation_found")
        for analysis, key in ((baseline, "baseline"), (fixed, "fixed")):
            self.assertEqual(analysis["execution_status"], "completed")
            self.assertEqual(analysis["final_state"]["actual_success"], self.complete[key]["actual_success"])
            self.assertIsNone(analysis["stopped_reason"])
        self.assertNotIn("sqlite_dump", json.dumps(parsed))
        self.assertEqual(parsed["source_report"], {"status": "finished", "completed": True, "stopped_reason": None,
                                                "evaluation_protocol": engine.EVALUATION_PROTOCOL, "prompt_policy": "neutral"})

    def test_partial_export_reimports_finished_fixed_group(self):
        report = self.budget_reports[20]
        self.assertEqual(report["status"], "partial")
        parsed = self.import_report(report)
        fixed = parsed["analyses"][1]
        self.assertEqual(fixed["execution_status"], "completed")
        self.assertEqual(fixed["verdict"], "no_violation_found")
        self.assertIs(fixed["final_state"]["actual_success"], True)
        self.assertIsNone(fixed["stopped_reason"])
        self.assertEqual(parsed["scenario"]["agent_mode"], "rules")
        self.assertEqual(fixed["agent_mode"], "llm")
        self.assertEqual(parsed["source_report"], {"status": "partial", "completed": False,
                                                "stopped_reason": "request_budget_exhausted",
                                                "evaluation_protocol": engine.EVALUATION_PROTOCOL, "prompt_policy": "neutral"})

    def test_unrun_fixed_group_preserves_unknown_and_stop_reason(self):
        report = self.budget_reports[7]
        self.assertIsNone(report["fixed"]["actual_success"])
        parsed = self.import_report(report)
        fixed = parsed["analyses"][1]
        self.assertEqual(fixed["execution_status"], "not_run")
        self.assertEqual(fixed["verdict"], "inconclusive")
        self.assertIsNone(fixed["final_state"]["actual_success"])
        self.assertEqual(fixed["stopped_reason"], "request_budget_exhausted")
        self.assertEqual(fixed["events"], [])
        self.assertEqual(fixed["findings"], [])
        self.assertIn("未运行", fixed["summary"])
        self.assertEqual(parsed["source_report"]["stopped_reason"], "request_budget_exhausted")

    def test_unknown_final_success_is_accepted_without_becoming_false(self):
        report = copy.deepcopy(self.complete)
        report["fixed"].update(completed=False, status="infra_error", actual_success=None,
                               reported_success=None, reported_outcome=None, report_assessment="unreported", failed=None,
                               stopped_reason="model_execution_error")
        report["fixed"]["events"] = [event for event in report["fixed"]["events"] if event["tool"] != "finish_task"]
        fixed = self.import_report(report)["analyses"][1]
        self.assertEqual(fixed["execution_status"], "incomplete")
        self.assertEqual(fixed["verdict"], "inconclusive")
        self.assertIsNone(fixed["final_state"]["actual_success"])
        self.assertEqual(fixed["stopped_reason"], "model_execution_error")

    def test_unrun_claim_with_business_results_is_rejected(self):
        for changed in ({"completed": True}, {"actual_success": False},
                        {"reported_success": True}, {"failed": False}):
            report = copy.deepcopy(self.budget_reports[7])
            report["fixed"].update(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "未运行组"):
                self.import_report(report)

    def test_report_state_types_fail_with_a_source_line(self):
        for changed in ({"skipped": "true"}, {"completed": None}, {"status": []}, {"agent_mode": {}}):
            report = copy.deepcopy(self.complete)
            report["fixed"].update(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "第 [0-9]+ 行"):
                self.import_report(report)

    def test_safe_identity_and_version_metadata_survive_without_unknown_fields(self):
        report = copy.deepcopy(self.complete)
        report.update(mode="llm", model_identity={"model": "synthetic-only", "api_origin": "https://model.invalid:443",
                      "api_key": "SYNTHETIC_METADATA_SECRET"},
                      provenance={"app_version": "0.5.0", "prompt_version": "tool-assistant-v1",
                                  "prompt_definition_sha256": "a" * 64,
                                  "source_tree_sha256": "b" * 64, "public_config_sha256": "c" * 64,
                                  "source_sha256": {"engine.py": "d" * 64, "../private.py": "e" * 64,
                                                    "bad.py": "not-a-hash"},
                                  "api_key": "SYNTHETIC_METADATA_SECRET", "unknown": "ignored"})
        parsed = self.import_report(report)
        self.assertEqual(parsed["mode"], "llm")
        self.assertEqual(parsed["model_identity"], {"model": "synthetic-only", "api_origin": "https://model.invalid:443"})
        self.assertEqual(parsed["provenance"]["source_sha256"], {"engine.py": "d" * 64})
        self.assertEqual(parsed["provenance"]["prompt_definition_sha256"], "a" * 64)
        self.assertNotIn("SYNTHETIC_METADATA_SECRET", json.dumps(parsed))
        self.assertNotIn("unknown", parsed["provenance"])

    def test_identity_origin_never_keeps_userinfo_paths_queries_or_fragments(self):
        for origin in ("https://user:fakepass@model.invalid", "https://model.invalid/private-path",
                       "https://model.invalid?api_key=SYNTHETIC_QUERY_SECRET", "https://model.invalid#secret",
                       "file:///private", "https://model.invalid:bad-port"):
            report = copy.deepcopy(self.complete)
            report["model_identity"] = {"model": "synthetic-only", "api_origin": origin}
            with self.subTest(origin=origin):
                parsed = self.import_report(report)
                self.assertIsNone(parsed["model_identity"]["api_origin"])
                self.assertNotIn(origin, json.dumps(parsed))


if __name__ == "__main__":
    unittest.main()
