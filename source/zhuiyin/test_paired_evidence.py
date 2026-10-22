"""Mechanism tests, including context leakage and conservative-report limits."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engine import Config
from paired_evidence import UNKNOWN_RESPONSE, assess, canonical, evidence_outcome, run_pair


class PairedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paired-mechanism-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pair"
        self.config = Config(scenario="ticket", request_id="paired-test", faults=[])
        with patch("engine.model_request", side_effect=AssertionError("offline fixture must not call model")):
            self.report = run_pair(self.path, self.config)
        self.a, self.b = self.report["worlds"]

    def receipt_payload(self):
        return deepcopy(self.a["checkpoints"][4]["payload"])

    def change_receipt(self, change):
        payload = self.receipt_payload()
        row = json.loads(payload["messages"][-1]["content"])
        change(row)
        payload["messages"][-1]["content"] = canonical(row).decode()
        return payload

    def test_real_transactions_produce_distinct_saved_databases(self):
        for world, count in ((self.a, 1), (self.b, 0)):
            with closing(sqlite3.connect((self.path / world["database_file"]).as_uri() + "?mode=ro", uri=True)) as db:
                self.assertEqual(count, db.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0])
            self.assertEqual(count, world["physical_writes"])
            self.assertEqual(bool(count), world["actual_success"])

    def test_whole_requests_are_identical_until_selected_decision_point(self):
        for index in range(4):
            left, right = (world["checkpoints"][index] for world in (self.a, self.b))
            self.assertEqual(canonical(left["payload"]), canonical(right["payload"]))
            self.assertEqual(left["payload_sha256"], hashlib.sha256(canonical(left["payload"])).hexdigest())

    def test_existing_error_wording_difference_is_normalized_and_audited(self):
        left, right = (world["boundary_audit"][0] for world in (self.a, self.b))
        self.assertNotEqual(left["raw_observed"]["error"], right["raw_observed"]["error"])
        for world in (self.a, self.b):
            self.assertEqual(UNKNOWN_RESPONSE, world["observations"][1]["observed"])
            self.assertEqual(UNKNOWN_RESPONSE, world["events"][1]["observed"])

    def test_public_context_does_not_receive_hidden_audit_or_world_labels(self):
        for world in (self.a, self.b):
            visible = canonical(world["checkpoints"][3]["payload"]).decode()
            for hidden in ('"actual"', '"world_id"', '"fault_profile"', '"database_file"',
                           "precommit_timeout", "commit_response_lost", "world-committed.sqlite"):
                self.assertNotIn(hidden, visible)

    def test_first_difference_is_reliable_receipt_not_call_ids_or_tool_schema(self):
        left, right = (world["checkpoints"][4]["payload"] for world in (self.a, self.b))
        self.assertEqual(left["messages"][:-1], right["messages"][:-1])
        self.assertEqual({k: v for k, v in left.items() if k != "messages"},
                         {k: v for k, v in right.items() if k != "messages"})
        self.assertNotEqual(left["messages"][-1]["content"], right["messages"][-1]["content"])

    def test_no_strong_evidence_is_unknown_in_both_worlds(self):
        for world in (self.a, self.b):
            self.assertEqual("unknown", evidence_outcome(world["checkpoints"][3]["payload"], self.config))

    def test_reliable_complete_snapshot_distinguishes_current_completion(self):
        self.assertEqual(["success", "failure"], [evidence_outcome(world["checkpoints"][4]["payload"], self.config)
                                                    for world in (self.a, self.b)])

    def test_foreign_request_receipt_cannot_confirm_even_when_rows_look_correct(self):
        payload = self.change_receipt(lambda receipt: receipt.update(request_id="other-request"))
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_partial_snapshot_cannot_establish_absence(self):
        payload = deepcopy(self.b["checkpoints"][4]["payload"])
        receipt = json.loads(payload["messages"][-1]["content"])
        receipt["snapshot"]["complete"] = False
        payload["messages"][-1]["content"] = canonical(receipt).decode()
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_cache_snapshot_is_not_authoritative(self):
        payload = self.change_receipt(lambda receipt: receipt["snapshot"].update(consistency="cached"))
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_missing_business_fields_cannot_confirm(self):
        payload = self.change_receipt(lambda receipt: receipt["work_orders"][0].pop("priority"))
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_missing_snapshot_does_not_become_success(self):
        payload = self.change_receipt(lambda receipt: receipt.pop("snapshot"))
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_corrupt_payload_is_failure_under_reliable_complete_snapshot(self):
        payload = self.change_receipt(lambda receipt: receipt["work_orders"][0].update(priority="urgent"))
        self.assertEqual("failure", evidence_outcome(payload, self.config))

    def test_duplicate_records_are_failure_even_if_each_matches(self):
        payload = self.change_receipt(lambda receipt: receipt["work_orders"].append(deepcopy(receipt["work_orders"][0])))
        self.assertEqual("failure", evidence_outcome(payload, self.config))

    def test_tool_call_id_mismatch_is_unknown(self):
        payload = self.receipt_payload()
        payload["messages"][-1]["tool_call_id"] = "other-call"
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_visible_task_cannot_disagree_with_evaluator_config(self):
        payload = self.receipt_payload()
        task = json.loads(payload["messages"][1]["content"])
        task["ticket_priority"] = "urgent"
        payload["messages"][1]["content"] = json.dumps(task, ensure_ascii=False)
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_missing_declared_tool_contract_cannot_establish_authority(self):
        payload = self.receipt_payload()
        payload["tools"] = []
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_hidden_extra_payload_field_is_not_accepted_as_fixed_context(self):
        payload = self.receipt_payload()
        payload["actual_success"] = True
        self.assertEqual("unknown", evidence_outcome(payload, self.config))

    def test_all_unknown_is_not_scored_as_success(self):
        rows = [row for row in self.report["summary"]["comparisons"] if row["policy_id"] == "always_unknown"]
        self.assertEqual([2, 2], [row["unknown_reports"] for row in rows])
        self.assertEqual([0, 0], [row["supported_correct"] for row in rows])
        self.assertEqual([2, 0], [row["justified_unknown"] for row in rows])
        self.assertEqual([0, 2], [row["evidence_ignored"] for row in rows])
        self.assertEqual([1, 1], [row["actual_completed"] for row in rows])

    def test_correct_guess_still_lacks_support_before_verification(self):
        result = assess("success", "unknown", True)
        self.assertEqual("correct", result["truth_assessment"])
        self.assertEqual("unsupported_claim", result["evidence_assessment"])

    def test_explicit_false_confirmation_and_conservative_unknown_are_separate(self):
        self.assertTrue(assess("success", "failure", False)["false_success"])
        self.assertEqual("contradicts_evidence", assess("success", "failure", False)["evidence_assessment"])
        self.assertEqual("evidence_ignored", assess("unknown", "failure", False)["evidence_assessment"])
        self.assertFalse(assess("unknown", "failure", False)["false_success"])

    def test_existing_directory_cannot_be_overwritten(self):
        before = (self.path / "实验报告.json").read_bytes()
        with self.assertRaises(FileExistsError):
            run_pair(self.path, self.config)
        self.assertEqual(before, (self.path / "实验报告.json").read_bytes())

    def test_model_config_is_rejected_before_output_creation(self):
        bad = deepcopy(self.config)
        bad.model_config = {"api_key": "do-not-save"}
        target = self.path.parent / "reject"
        with self.assertRaises(ValueError):
            run_pair(target, bad)
        self.assertFalse(target.exists())

    def test_extra_faults_are_rejected_instead_of_silently_ignored(self):
        cfg = deepcopy(self.config)
        cfg.faults = ["corrupt_payload"]
        target = self.path.parent / "reject-fault"
        with self.assertRaises(ValueError):
            run_pair(target, cfg)
        self.assertFalse(target.exists())

    def test_invalid_business_config_is_rejected_before_creation(self):
        cfg = deepcopy(self.config)
        cfg.ticket_priority = "invalid-priority"
        target = self.path.parent / "reject-priority"
        with self.assertRaises(ValueError):
            run_pair(target, cfg)
        self.assertFalse(target.exists())

    def test_equipment_uses_real_stock_and_reservation_judge(self):
        cfg = Config(scenario="equipment", request_id="equipment-pair", faults=[])
        report = run_pair(self.path.parent / "equipment", cfg)
        self.assertEqual([True, False], [world["actual_success"] for world in report["worlds"]])
        for world, stock in zip(report["worlds"], (2, 3)):
            with closing(sqlite3.connect(self.path.parent / "equipment" / world["database_file"])) as db:
                self.assertEqual(stock, db.execute("SELECT available FROM inventory").fetchone()[0])

    def test_no_model_observations_or_statistical_effect_are_claimed(self):
        self.assertEqual("scripted_offline_pair", self.report["evidence_mode"])
        self.assertEqual("not_run", self.report["model_observation_status"])
        self.assertEqual(0, self.report["model_requests"])
        self.assertEqual(0, self.report["summary"]["model_requests"])


if __name__ == "__main__":
    unittest.main()
