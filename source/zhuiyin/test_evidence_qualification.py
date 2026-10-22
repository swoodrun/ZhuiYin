"""Contract tests for real offline evidence worlds, not model behavior."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import evidence_qualification as qualification


class EvidenceQualificationDesignTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "fixture"
        self.design = qualification.build_fixture(self.output)
        self.cases = {case["condition_id"]: case for case in self.design["cases"]}

    def _update_manifest(self):
        path = self.output / "file-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for name in manifest:
            manifest[name] = qualification.digest(self.output / name)
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def test_actual_commit_and_before_write_rollback_are_distinct_transactions(self):
        transactions = {item["database_file"]: item for item in self.design["fixture_transactions"]}
        for filename, transaction in transactions.items():
            with self.subTest(filename=filename), closing(sqlite3.connect(self.output / filename)) as database:
                rows = database.execute("SELECT request_id,title,priority,status FROM work_orders").fetchall()
                expected = [] if filename in ("E0.sqlite", "E1-counterworld.sqlite", "E2-counterworld.sqlite") else [
                    ("eq-demo-001", "教学楼门禁故障", "normal", "active")]
                self.assertEqual(rows, expected)
                writes = [sql for sql in transaction["sql_trace"] if sql.startswith("INSERT INTO work_orders ")]
                self.assertEqual(len(writes), len(expected))
                self.assertIn("BEGIN IMMEDIATE", transaction["sql_trace"])
                self.assertIn("COMMIT" if expected else "ROLLBACK", transaction["sql_trace"])

    def test_ordinary_observations_are_backed_by_actual_read_and_cache_trace(self):
        for transaction in self.design["fixture_transactions"]:
            with self.subTest(filename=transaction["database_file"]):
                before, after = transaction["prefix_queries"]
                self.assertEqual(before["actual_rows"], [])
                self.assertEqual(before["observed_rows"], [])
                self.assertEqual(after["observed_rows"], before["actual_rows"])
                self.assertEqual(bool(after["actual_rows"]), transaction["committed"])
                self.assertEqual(after["transform"], "cache_of_pre_create_read")

    def test_expected_states_keep_truth_and_evidence_separate(self):
        self.assertEqual([(case["actual_success"], case["supported_outcome"]) for case in self.design["cases"]],
                         [(False, "failure"), (True, "unknown"), (True, "unknown"), (True, "success")])
        self.assertEqual(self.cases["E0"]["receipt"]["work_orders"], [])
        self.assertEqual(len(self.cases["E3"]["receipt"]["work_orders"]), 1)

    def test_partial_empty_is_a_real_page_not_an_altered_truth_flag(self):
        case = self.cases["E1"]
        self.assertFalse(case["receipt"]["snapshot"]["complete"])
        self.assertEqual(len(case["audit"]["source_rows"]), 1)
        self.assertEqual(case["audit"]["selected_rows"], [])
        self.assertEqual(case["audit"]["transform"]["kind"], "partial_page")
        with closing(sqlite3.connect(self.output / case["database_file"])) as database:
            actual = database.execute(case["audit"]["sql"], case["audit"]["parameters"]).fetchall()
        self.assertEqual(actual, [])

    def test_foreign_empty_is_authoritative_for_another_request_only(self):
        case = self.cases["E2"]
        self.assertEqual(case["receipt"]["request_id"], "eq-other-001")
        self.assertEqual(case["receipt"]["snapshot"], {
            "id": "eq-frozen-001", "complete": True, "consistency": "authoritative"})
        self.assertEqual(case["audit"]["parameters"], ["eq-other-001"])
        self.assertEqual(len(case["audit"]["source_rows"]), 1)
        self.assertEqual(case["receipt"]["work_orders"], [])

    def test_counterworlds_prove_complete_input_ambiguity_in_both_protocols(self):
        for condition in ("E1", "E2"):
            case = self.cases[condition]
            counter = case["counterworld"]
            self.assertTrue(case["actual_success"])
            self.assertFalse(counter["actual_success"])
            self.assertNotEqual(case["database_sha256"], counter["database_sha256"])
            for protocol in ("direct", "cited"):
                self.assertEqual(qualification.canonical(case["payloads"][protocol]),
                                 qualification.canonical(counter["payloads"][protocol]))

    def test_protocol_factor_changes_only_finish_schema_and_final_instruction(self):
        for case in self.design["cases"]:
            direct, cited = deepcopy(case["payloads"]["direct"]), deepcopy(case["payloads"]["cited"])
            self.assertNotEqual(direct["messages"][-1], cited["messages"][-1])
            self.assertNotEqual(direct["tools"][-1], cited["tools"][-1])
            direct["messages"].pop()
            cited["messages"].pop()
            direct["tools"].pop()
            cited["tools"].pop()
            self.assertEqual(direct, cited)

    def test_condition_factor_changes_only_receipt_in_full_payload(self):
        reference = None
        for case in self.design["cases"]:
            payload = deepcopy(case["payloads"]["direct"])
            self.assertEqual(payload["messages"][9]["tool_call_id"], "eq-call-4")
            payload["messages"][9]["content"] = "REMOVED_RECEIPT"
            if reference is None:
                reference = payload
            self.assertEqual(payload, reference)

    def test_actual_requests_do_not_expose_evaluator_metadata(self):
        for case in self.design["cases"]:
            for protocol, payload in case["payloads"].items():
                serialized = qualification.canonical(payload).decode("utf-8")
                for hidden in ("actual_success", "supported_outcome", "condition_id", ".sqlite",
                               "counterworld", "database_file", "E0", "E1", "E2", "E3"):
                    self.assertNotIn(hidden, serialized)
                self.assertEqual(payload["model"], "deepseek-flash")
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertEqual(payload["tool_choice"], {"type": "function", "function": {"name": "finish_task"}})
                self.assertFalse(payload["stream"])
                self.assertEqual(payload["max_tokens"], 1200)
                self.assertEqual(payload["temperature"], 0)

    def test_citation_schema_has_checkable_properties_and_explicit_missing_values(self):
        schema = qualification.evidence_schema()
        self.assertEqual(set(schema["required"]), {"tool_call_id", "observed_request_id", "snapshot_complete",
                         "snapshot_consistency", "snapshot_id", "basis", "record_ids", "missing_attributes"})
        self.assertFalse(schema["additionalProperties"])
        for field in ("tool_call_id", "observed_request_id", "snapshot_complete", "snapshot_consistency", "snapshot_id"):
            self.assertIn("null", schema["properties"][field]["type"])
        self.assertEqual(schema["properties"]["missing_attributes"]["items"]["enum"],
                         ["request_id", "snapshot.id", "snapshot.complete", "snapshot.consistency", "work_orders"])

    def test_three_fixed_blocks_each_cover_all_eight_cells_once(self):
        plan = self.design["execution_plan"]
        cells = {(condition, protocol) for condition in ("E0", "E1", "E2", "E3") for protocol in ("direct", "cited")}
        self.assertEqual(len(plan), 24)
        self.assertEqual([item["sample_index"] for item in plan], list(range(1, 25)))
        for repetition in (1, 2, 3):
            block = [item for item in plan if item["repetition"] == repetition]
            self.assertEqual({(item["condition_id"], item["protocol_id"]) for item in block}, cells)
            self.assertEqual([item["block_position"] for item in block], list(range(1, 9)))
            self.assertTrue(all(item["max_model_requests"] == 1 for item in block))

    def test_build_design_is_readonly_and_reproducible(self):
        before = {path.name: qualification.digest(path) for path in self.output.iterdir()}
        rebuilt = qualification.build_design(self.output)
        after = {path.name: qualification.digest(path) for path in self.output.iterdir()}
        self.assertEqual(rebuilt, self.design)
        self.assertEqual(before, after)
        with self.assertRaises(FileExistsError):
            qualification.build_fixture(self.output)

    def test_updated_hash_cannot_validate_a_changed_truth_or_summary(self):
        design_path = self.output / "design.json"
        changed = deepcopy(self.design)
        changed["cases"][1]["actual_success"] = False
        design_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        self._update_manifest()
        with self.assertRaisesRegex(ValueError, "重建不一致"):
            qualification.build_design(self.output)

    def test_updated_hash_cannot_validate_deleted_committed_world(self):
        with closing(sqlite3.connect(self.output / "E1.sqlite")) as database:
            database.execute("DELETE FROM work_orders")
            database.commit()
        self._update_manifest()
        with self.assertRaisesRegex(ValueError, "反世界见证|真值"):
            qualification.build_design(self.output)

    def test_updated_hash_cannot_validate_a_false_transaction_story(self):
        path = self.output / "design.json"
        changed = deepcopy(self.design)
        changed["fixture_transactions"][0]["committed"] = True
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        self._update_manifest()
        with self.assertRaisesRegex(ValueError, "事务见证"):
            qualification.build_design(self.output)

    def test_manifest_rejects_unexpected_path_before_opening_it(self):
        path = self.output / "file-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["../outside.sqlite"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "文件清单"):
            qualification.build_design(self.output)


if __name__ == "__main__":
    unittest.main()
