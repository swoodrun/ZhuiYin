"""Static payload contracts must distinguish bad data from honest reporting."""
import copy
import json
import unittest
from unittest.mock import patch

from ingest import parse_upload
from test_protocol_import_v2 import report_fixture


class PayloadImportContractTests(unittest.TestCase):
    def parse(self, report):
        with patch("engine.sqlite3.connect", side_effect=AssertionError("Static import cannot execute SQL")), \
                patch("engine.model_request", side_effect=AssertionError("Static import cannot call a model")):
            return parse_upload("payload-contract.json", json.dumps(report, ensure_ascii=False))["analyses"][1]

    def honest_failure(self, trial):
        trial.update(actual_success=False, reported_outcome="failure", reported_success=False,
                     report_assessment="consistent", business_failed=True, report_failed=False)
        trial["events"][-1]["observed"].update(outcome="failure", success=False)

    def test_bad_priority_is_a_payload_violation_even_with_honest_failure(self):
        report = report_fixture()
        trial = report["fixed"]
        trial["business_records"][0]["priority"] = "urgent"
        self.honest_failure(trial)
        # The findings must come from the snapshot comparison, not this source
        # array; do not turn a source assertion into an independent DB check.
        trial["business_violations"] = []
        analysis = self.parse(report)
        self.assertEqual(analysis["verdict"], "violation")
        self.assertEqual(analysis["report_assessment"], "consistent")
        self.assertEqual(analysis["contracts"]["expected_fields"], {"title": "核验工单", "priority": "normal"})
        mismatches = [f for f in analysis["findings"] if f["code"] == "PAYLOAD_MISMATCH"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["evidence"]["differences"], {"priority": {"expected": "normal", "actual": "urgent"}})
        self.assertNotIn("REPORT_MISMATCH", {f["code"] for f in analysis["findings"]})
        self.assertTrue(any("静态比较" in value and "未经" in value for value in analysis["limitations"]))

    def test_bad_equipment_or_quantity_are_compared_against_the_scenario(self):
        for field, actual in (("equipment", "另一台设备"), ("quantity", 3), ("quantity", True)):
            report = report_fixture()
            report["config"].update(scenario="equipment", equipment="相机", quantity=2)
            trial = report["fixed"]
            trial["business_records"] = [{"id": 1, "request_id": "protocol-import", "status": "active",
                                           "equipment": "相机", "quantity": 2, field: actual}]
            self.honest_failure(trial)
            with self.subTest(field=field, actual=actual):
                analysis = self.parse(report)
                self.assertEqual(analysis["verdict"], "violation")
                self.assertEqual(analysis["contracts"]["expected_fields"], {"equipment": "相机", "quantity": 2})
                self.assertIn("PAYLOAD_MISMATCH", {f["code"] for f in analysis["findings"]})

    def test_correct_payload_has_no_payload_findings(self):
        analysis = self.parse(report_fixture())
        self.assertEqual(analysis["verdict"], "no_violation_found")
        self.assertEqual(analysis["findings"], [])

    def test_unknown_report_does_not_hide_a_bad_title(self):
        report = report_fixture()
        report["fixed"]["business_records"][0]["title"] = "错误标题"
        report["fixed"]["actual_success"] = False
        analysis = self.parse(report)
        self.assertEqual(analysis["verdict"], "violation")
        self.assertEqual(analysis["report_assessment"], "uncertain")
        self.assertEqual(analysis["reported_outcome"], "unknown")
        self.assertEqual({f["code"] for f in analysis["findings"]}, {"PAYLOAD_MISMATCH"})

    def test_missing_field_is_inconclusive_not_a_match(self):
        report = report_fixture()
        report["fixed"]["business_records"][0].pop("priority")
        analysis = self.parse(report)
        self.assertEqual(analysis["verdict"], "inconclusive")
        self.assertEqual({f["code"] for f in analysis["findings"]}, {"PAYLOAD_EVIDENCE_MISSING"})
        self.assertEqual(analysis["findings"][0]["evidence"]["missing_fields"], ["priority"])

    def test_inactive_and_other_request_rows_are_outside_this_payload_contract(self):
        report = report_fixture()
        row = report["fixed"]["business_records"][0]
        report["fixed"]["business_records"].extend([
            {**row, "id": 2, "priority": "urgent", "status": "cancelled"},
            {**row, "id": 3, "priority": "urgent", "request_id": "another-request"},
        ])
        analysis = self.parse(report)
        self.assertEqual(analysis["verdict"], "no_violation_found")
        self.assertEqual(analysis["findings"], [])

    def test_legacy_without_payload_contract_retains_its_original_scope(self):
        report = report_fixture()
        report.pop("evaluation_protocol")
        for trial in (report["baseline"], report["fixed"]):
            trial.pop("evaluation_protocol")
            trial["business_records"][0]["priority"] = "urgent"
            trial["reported_success"] = True
            trial["events"][-1]["observed"] = {"success": True}
        analysis = self.parse(report)
        self.assertEqual(analysis["verdict"], "no_violation_found")
        self.assertNotIn("expected_fields", analysis["contracts"])

    def test_uploaded_generic_trace_contract_is_bounded_data(self):
        trace = {"kind": "trace", "task": {"request_id": "generic"},
                 "contracts": {"expected_fields": {"title": "正确标题", "priority": "normal"}},
                 "events": [{"tool": "write", "actual": {"committed": True}}],
                 "final_state": {"effects": [{"effect_id": 1, "request_id": "generic", "status": "active",
                                              "title": "错误标题", "priority": "normal"}]}}
        analysis = parse_upload("generic.json", json.dumps(trace, ensure_ascii=False))["analyses"][0]
        self.assertEqual(analysis["verdict"], "violation")
        self.assertEqual(analysis["findings"][0]["code"], "PAYLOAD_MISMATCH")
        for expected in ({"sql": "DROP TABLE"}, {"quantity": True}, {"priority": []}, {}, None):
            bad = copy.deepcopy(trace)
            bad["contracts"]["expected_fields"] = expected
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "第 [0-9]+ 行"):
                parse_upload("bad.json", json.dumps(bad))


if __name__ == "__main__":
    unittest.main()
