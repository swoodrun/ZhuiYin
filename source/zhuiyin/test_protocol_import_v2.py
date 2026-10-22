"""Tri-state v2 imports remain distinct from legacy, incomplete and unrun data."""
import copy
import json
import unittest
from unittest.mock import patch

import engine
from ingest import EVALUATION_PROTOCOL, parse_upload


def report_fixture():
    row = {"id": 1, "request_id": "protocol-import", "status": "active",
           "title": "核验工单", "priority": "normal"}
    trial = {
        "evaluation_protocol": EVALUATION_PROTOCOL, "agent_mode": "llm",
        "completed": True, "status": "finished", "failed": False,
        "actual_success": True, "reported_success": None,
        "reported_outcome": "unknown", "report_assessment": "uncertain",
        "business_failed": False, "report_failed": False,
        "business_violations": [], "reporting_violations": [], "execution_violations": [],
        "business_records": [row], "events": [
            {"step": 1, "tool": "create_ticket", "observed": {"ok": True},
             "actual": {"committed": True, "ticket_id": 1}},
            {"step": 2, "tool": "finish_task", "kind": "report",
             "observed": {"outcome": "unknown", "success": None, "reason": "无法确认"}},
        ],
    }
    return {
        "evaluation_protocol": EVALUATION_PROTOCOL, "status": "finished", "completed": True,
        "mode": "llm", "config": {"scenario": "ticket", "request_id": "protocol-import",
                                   "ticket_title": "核验工单", "agent_mode": "llm", "faults": []},
        "baseline": copy.deepcopy(trial), "fixed": copy.deepcopy(trial),
    }


class ProtocolImportV2Tests(unittest.TestCase):
    def parse(self, data):
        with patch("engine.sqlite3.connect", side_effect=AssertionError("Import must not execute SQL")), \
                patch("engine.model_request", side_effect=AssertionError("Import must not call a model")):
            return parse_upload("protocol-report.json", json.dumps(data, ensure_ascii=False))

    def test_explicit_unknown_is_completed_not_a_mismatch_or_false(self):
        parsed = self.parse(report_fixture())
        self.assertEqual(parsed["evaluation_protocol"], EVALUATION_PROTOCOL)
        self.assertEqual(parsed["source_report"]["evaluation_protocol"], EVALUATION_PROTOCOL)
        for analysis in parsed["analyses"]:
            self.assertEqual(analysis["execution_status"], "completed")
            self.assertEqual(analysis["reported_outcome"], "unknown")
            self.assertIsNone(analysis["reported_success"])
            self.assertIs(analysis["final_state"]["actual_success"], True)
            self.assertEqual(analysis["report_assessment"], "uncertain")
            self.assertFalse(analysis["business_failed"])
            self.assertFalse(analysis["report_failed"])
            self.assertEqual(analysis["verdict"], "no_violation_found")
            self.assertEqual(analysis["findings"], [])

    def test_unknown_report_does_not_hide_duplicate_business_effect(self):
        report = report_fixture()
        trial = report["baseline"]
        trial["business_records"].append({**trial["business_records"][0], "id": 2})
        trial.update(actual_success=False, failed=True, business_failed=True,
                     business_violations=[{"code": "DUPLICATE_TICKET", "message": "两条有效工单"}])
        analysis = self.parse(report)["analyses"][0]
        self.assertEqual(analysis["verdict"], "violation")
        self.assertEqual({f["code"] for f in analysis["findings"]}, {"DUPLICATE_EFFECT"})
        self.assertTrue(analysis["business_failed"])
        self.assertFalse(analysis["report_failed"])
        self.assertEqual(analysis["source_evaluation"]["business_violations"], trial["business_violations"])
        self.assertEqual({item["code"] for item in analysis["business_violations"]}, {"DUPLICATE_EFFECT"})
        self.assertEqual(analysis["report_assessment"], "uncertain")

    def test_unfinished_has_unreported_outcome_and_keeps_execution_evidence(self):
        report = report_fixture()
        trial = report["fixed"]
        trial["events"].pop()
        trial.update(completed=False, status="infra_error", reported_outcome=None,
                     report_assessment="unreported", stopped_reason="model_execution_error",
                     execution_violations=[{"code": "AGENT_DID_NOT_FINISH", "message": "未提交结果"}])
        analysis = self.parse(report)["analyses"][1]
        self.assertEqual(analysis["execution_status"], "incomplete")
        self.assertEqual(analysis["verdict"], "inconclusive")
        self.assertIsNone(analysis["reported_outcome"])
        self.assertEqual(analysis["report_assessment"], "unreported")
        self.assertEqual(analysis["execution_violations"], trial["execution_violations"])

    def test_unrun_preserves_null_results_and_unknown_cannot_claim_not_run(self):
        report = report_fixture()
        report["fixed"].update(status="not_run", skipped=True, completed=False, failed=None,
                               actual_success=None, reported_outcome=None, report_assessment="unreported",
                               business_records=[], events=[], business_failed=None, report_failed=None)
        analysis = self.parse(report)["analyses"][1]
        self.assertEqual(analysis["execution_status"], "not_run")
        self.assertIsNone(analysis["final_state"]["actual_success"])
        self.assertIsNone(analysis["reported_success"])
        self.assertIsNone(analysis["reported_outcome"])
        report["fixed"]["reported_outcome"] = "unknown"
        with self.assertRaisesRegex(ValueError, "未运行组"):
            self.parse(report)

    def test_legacy_failure_claim_keeps_its_original_mismatch(self):
        report = report_fixture()
        report.pop("evaluation_protocol")
        for trial in (report["baseline"], report["fixed"]):
            for key in ("evaluation_protocol", "reported_outcome", "report_assessment", "business_failed",
                        "report_failed", "business_violations", "reporting_violations", "execution_violations"):
                trial.pop(key)
            trial["reported_success"] = False
            trial["events"][-1]["observed"] = {"success": False, "reason": "无法确认（旧布尔协议）"}
        parsed = self.parse(report)
        self.assertNotIn("evaluation_protocol", parsed)
        for analysis in parsed["analyses"]:
            self.assertEqual(analysis["execution_status"], "completed")
            self.assertEqual(analysis["verdict"], "violation")
            self.assertIn("REPORT_MISMATCH", {f["code"] for f in analysis["findings"]})
            self.assertNotIn("reported_outcome", analysis)
            self.assertNotIn("report_assessment", analysis)

    def test_null_success_is_allowed_only_for_an_explicit_v2_unknown_report(self):
        for changed in ("legacy", "ordinary_tool", "success_outcome", "missing_outcome"):
            report = report_fixture()
            observed = report["fixed"]["events"][-1]["observed"]
            if changed == "legacy":
                report.pop("evaluation_protocol")
                report["fixed"].pop("evaluation_protocol")
            elif changed == "ordinary_tool":
                report["fixed"]["events"][-1].update(tool="query_ticket", kind="tool")
            elif changed == "success_outcome":
                observed["outcome"] = "success"
            else:
                observed.pop("outcome")
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "第 [0-9]+ 行"):
                self.parse(report)

    def test_v2_outcome_cannot_contradict_the_compatibility_bool(self):
        for value in (True, False):
            report = report_fixture()
            report["fixed"]["reported_success"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "reported_outcome"):
                self.parse(report)

    def test_event_only_unknown_is_retained_without_a_compatibility_field(self):
        report = report_fixture()
        for trial in (report["baseline"], report["fixed"]):
            trial.pop("reported_success")
            trial.pop("reported_outcome")
            trial.pop("report_assessment")
        analysis = self.parse(report)["analyses"][0]
        self.assertEqual(analysis["reported_outcome"], "unknown")
        self.assertEqual(analysis["report_assessment"], "uncertain")
        self.assertEqual(analysis["findings"], [])

    def test_current_engine_unknown_result_roundtrips_from_real_database_state(self):
        config = engine.Config(scenario="ticket", request_id="actual-engine-v2", faults=[])

        def uncertain_agent(lab, connection):
            lab.reserve()
            lab.finish(outcome="unknown", reason="核验信息不足")

        with patch("engine.llm_agent", side_effect=uncertain_agent), \
                patch("engine.model_request", side_effect=AssertionError("Synthetic engine test is offline")):
            trial = engine.run_trial(config, [], agent_mode="llm")
        self.assertTrue(trial["completed"])
        self.assertTrue(trial["actual_success"])
        report = {"config": config.public(), "evaluation_protocol": engine.EVALUATION_PROTOCOL,
                  "baseline": trial, "fixed": copy.deepcopy(trial)}
        analysis = self.parse(report)["analyses"][0]
        self.assertEqual(analysis["execution_status"], "completed")
        self.assertEqual(analysis["reported_outcome"], "unknown")
        self.assertIsNone(analysis["reported_success"])
        self.assertEqual(analysis["report_assessment"], "uncertain")
        self.assertEqual(analysis["findings"], [])
        self.assertEqual(analysis["business_violations"], [])
        self.assertEqual(analysis["reporting_violations"], [])
        self.assertEqual(analysis["execution_violations"], [])

    def test_safe_model_settings_and_protocol_metadata_are_whitelisted(self):
        report = report_fixture()
        report.update(model_identity={"model": "synthetic", "api_origin": "https://model.invalid",
                                      "thinking": "disabled", "api_key": "SYNTHETIC_NOT_FOR_STORAGE"},
                      provenance={"app_version": "0.6.0", "prompt_version": "tool-assistant-v2",
                                  "evaluation_protocol": EVALUATION_PROTOCOL, "prompt_policy": "neutral",
                                  "model_request_settings": {"thinking": "disabled",
                                                             "api_key": "SYNTHETIC_NOT_FOR_STORAGE",
                                                             "arbitrary": "ignored"}})
        parsed = self.parse(report)
        self.assertEqual(parsed["model_identity"], {"model": "synthetic", "api_origin": "https://model.invalid",
                                                    "thinking": "disabled"})
        self.assertEqual(parsed["provenance"]["model_request_settings"], {"thinking": "disabled"})
        self.assertEqual(parsed["provenance"]["evaluation_protocol"], EVALUATION_PROTOCOL)
        self.assertEqual(parsed["provenance"]["prompt_policy"], "neutral")
        self.assertNotIn("SYNTHETIC_NOT_FOR_STORAGE", json.dumps(parsed))

    def test_malformed_optional_audit_labels_do_not_raise_untyped_errors(self):
        report = report_fixture()
        report.update(model_identity={"model": "synthetic", "thinking": {}},
                      provenance={"prompt_policy": [], "model_request_settings": {"thinking": {}}})
        parsed = self.parse(report)
        self.assertNotIn("thinking", parsed["model_identity"])
        self.assertNotIn("model_request_settings", parsed["provenance"])

    def test_factor_audit_keeps_switches_and_hashes_without_prompt_text(self):
        report = report_fixture()
        manifest = {"prompt_policy": "neutral", "idempotency": True, "verification": False,
                    "system_prompt_sha256": "a" * 64, "tool_schema_sha256": "b" * 64,
                    "parameter_scope": "server_supplied_business_fields", "verification_enforcement": "agent_choice",
                    "system_prompt": "private prompt not retained", "api_key": "SYNTHETIC_FACTOR_SECRET"}
        report["baseline"].update(prompt_policy="neutral", factor_manifest=manifest)
        report["provenance"] = {"factor_design": {"instruction_policy": "neutral",
            "system_prompt_shared_within_policy": True, "capability_factors": ["idempotency", "verification"],
            "manifests": {"baseline": manifest, "unknown_strategy": manifest}, "extra": "ignored"}}
        parsed = self.parse(report)
        self.assertEqual(parsed["analyses"][0]["factor_manifest"]["system_prompt_sha256"], "a" * 64)
        self.assertEqual(parsed["analyses"][0]["prompt_policy"], "neutral")
        design = parsed["provenance"]["factor_design"]
        self.assertEqual(set(design["manifests"]), {"baseline"})
        self.assertEqual(design["capability_factors"], ["idempotency", "verification"])
        self.assertNotIn("extra", design)
        self.assertNotIn("private prompt not retained", json.dumps(parsed))
        self.assertNotIn("SYNTHETIC_FACTOR_SECRET", json.dumps(parsed))

    def test_invalid_new_protocol_fields_fail_with_a_source_line(self):
        for field, value in (("reported_outcome", {}), ("reported_outcome", "maybe"),
                             ("report_assessment", []), ("business_failed", 0),
                             ("report_failed", "false"), ("business_violations", {}),
                             ("execution_violations", [{"code": []}])):
            report = report_fixture()
            report["fixed"][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "第 [0-9]+ 行"):
                self.parse(report)

    def test_static_jsonl_unknown_keeps_business_fields_and_masks_headers(self):
        rows = [
            {"type": "meta", "evaluation_protocol": EVALUATION_PROTOCOL,
             "task": {"request_id": "jsonl-v2"}},
            {"type": "event", "event": {"tool": "create_ticket", "actual": {"committed": True},
             "observed": {"headers": [{"name": "Authorization", "value": "SYNTHETIC_HEADER"}],
                          "fields": [{"name": "Authorization", "value": "business-value"}]}}},
            {"type": "event", "event": {"tool": "finish_task", "kind": "report",
             "observed": {"outcome": "unknown", "success": None}}},
            {"type": "final_state", "final_state": {"effects": [], "actual_success": False}},
        ]
        parsed = parse_upload("v2.jsonl", "\n".join(json.dumps(row) for row in rows))
        analysis = parsed["analyses"][0]
        self.assertEqual(analysis["reported_outcome"], "unknown")
        self.assertEqual(analysis["report_assessment"], "uncertain")
        self.assertNotIn("REPORT_MISMATCH", {f["code"] for f in analysis["findings"]})
        self.assertNotIn("SYNTHETIC_HEADER", json.dumps(parsed))
        self.assertEqual(analysis["events"][0]["observed"]["fields"][0]["value"], "business-value")


if __name__ == "__main__":
    unittest.main()
