"""Offline boundary checks keep contract status separate from model behavior."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import evidence_qualification
from tools import evidence_boundary


class EvidenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "fixture"
        self.output = Path(self.temporary.name) / "boundary"
        evidence_qualification.build_fixture(self.source)
        self.before = {path.name: evidence_boundary.digest(path) for path in self.source.iterdir()}
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("offline must not make requests")):
            self.report = evidence_boundary.run(self.output, self.source)
        self.conditions = {condition["polarity"]: condition for condition in self.report["conditions"]}

    def _candidate(self, polarity, name):
        return next(item for item in self.conditions[polarity]["candidates"]
                    if item["candidate_id"] == f"{polarity}-{name}")

    def _request(self, candidate):
        path = self.output / candidate["request_file"]
        self.assertEqual(evidence_boundary.digest(path), candidate["request_file_sha256"])
        return json.loads(path.read_text(encoding="utf-8"))

    def _receipt(self, candidate):
        return json.loads(self._request(candidate)["messages"][9]["content"])

    def test_real_sqlite_business_truth_is_separate_from_contract_and_model(self):
        for polarity, count, expected in (("negative", 0, "failure"), ("positive", 1, "success")):
            condition = self.conditions[polarity]
            with closing(sqlite3.connect(self.output / condition["database_file"])) as database:
                self.assertEqual(database.execute("SELECT count(*) FROM work_orders").fetchone()[0], count)
            self.assertEqual(condition["baseline_supported_outcome"], expected)
            self.assertEqual(condition["model_behavior"], "not_measured")
            self.assertEqual(condition["database_audit"]["actual_success"], bool(count))
            self.assertEqual(condition["database_audit"]["database_open_mode"], "ro_immutable")
            self.assertTrue(condition["database_audit"]["query_only"])

    def test_negative_and_positive_each_have_one_qualified_base_and_valid_unknown_weakening(self):
        self.assertEqual(self.report["summary"], {
            "candidates": 26, "valid": 20, "not_testable_malformed": 6,
            "qualified_definite": 2, "valid_unknown": 18, "local_1_minima": 2,
            "model_requests": 0, "model_behavior": "not_measured"})
        for polarity in ("negative", "positive"):
            base = self._candidate(polarity, "base")
            self.assertTrue(base["preserves_baseline_contract_outcome"])
            for name in ("incomplete-page", "foreign-request", "unmatched-snapshot", "cached-authority"):
                candidate = self._candidate(polarity, name)
                self.assertEqual(candidate["contract_criterion"]["candidate_status"], "valid")
                self.assertEqual(candidate["contract_criterion"]["supported_outcome"], "unknown")

    def test_foreign_candidate_is_backed_by_real_other_request_query(self):
        for polarity in ("negative", "positive"):
            condition = self.conditions[polarity]
            candidate = self._candidate(polarity, "foreign-request")
            receipt = self._receipt(candidate)
            self.assertEqual(receipt["request_id"], "eq-other-001")
            self.assertEqual(receipt["work_orders"], [])
            self.assertEqual(condition["database_audit"]["foreign_rows"], [])
            with closing(sqlite3.connect(self.output / condition["database_file"])) as database:
                rows = database.execute(condition["database_audit"]["foreign_sql"],
                                        condition["database_audit"]["foreign_parameters"]).fetchall()
            self.assertEqual(rows, [])
        self.assertEqual(self._candidate("positive", "foreign-request")["removed_or_weakened_attributes"],
                         ["request_binding", "records"])

    def test_missing_record_list_is_valid_omission_not_empty_evidence(self):
        for polarity in ("negative", "positive"):
            candidate = self._candidate(polarity, "without-records")
            self.assertNotIn("work_orders", self._receipt(candidate))
            self.assertEqual(candidate["contract_criterion"]["candidate_status"], "valid")
            self.assertEqual(candidate["contract_criterion"]["supported_outcome"], "unknown")

    def test_malformed_receipts_are_not_mislabeled_as_unknown_or_tested_deletions(self):
        for polarity in ("negative", "positive"):
            for name in ("malformed-snapshot", "malformed-records", "malformed-complete"):
                candidate = self._candidate(polarity, name)
                self.assertEqual(candidate["operation"], "malformed_negative_control")
                self.assertEqual(candidate["contract_criterion"]["candidate_status"], "not_testable")
                self.assertIsNone(candidate["contract_criterion"]["supported_outcome"])
                self.assertFalse(candidate["preserves_baseline_contract_outcome"])

    def test_every_deletion_has_saved_full_payload_and_local_witness(self):
        for polarity in ("negative", "positive"):
            condition = self.conditions[polarity]
            minimum = condition["local_minimum"]
            self.assertTrue(minimum["is_local_1_minimal"])
            self.assertEqual(minimum["attributes"], list(evidence_boundary.ATTRIBUTE_GROUPS))
            self.assertEqual(len(minimum["single_deletion_witnesses"]), 5)
            for witness in minimum["single_deletion_witnesses"]:
                candidate = self._candidate(polarity, "without-" + witness["deleted_attribute"])
                request = self._request(candidate)
                self.assertIn("messages", request)
                self.assertIn("tools", request)
                self.assertFalse(witness["preserves_baseline_contract_outcome"])
                self.assertEqual(witness["contract_criterion"], candidate["contract_criterion"])
            self.assertTrue(all(not step["accepted_deletion"] for step in condition["greedy_steps"]))

    def test_one_common_conditional_contract_covers_all_candidates(self):
        reference = None
        for condition in self.report["conditions"]:
            for candidate in condition["candidates"]:
                request = self._request(candidate)
                self.assertEqual(request["messages"][0]["content"], evidence_boundary.BOUNDARY_SYSTEM)
                verify_tool = next(tool["function"] for tool in request["tools"]
                                   if tool["function"]["name"] == "verify_ticket")
                self.assertIn("各资格属性均可缺失", verify_tool["description"])
                self.assertIn("工具名称本身不保证", verify_tool["description"])
                request["messages"][9]["content"] = "REMOVED_RECEIPT"
                if reference is None:
                    reference = request
                self.assertEqual(request, reference)

    def test_actual_payload_contains_no_research_truth_or_database_names(self):
        for condition in self.report["conditions"]:
            for candidate in condition["candidates"]:
                serialized = json.dumps(self._request(candidate), ensure_ascii=False)
                for hidden in ("actual_success", "database_file", ".sqlite", "candidate_id", "local_minimum"):
                    self.assertNotIn(hidden, serialized)
                self.assertEqual(candidate["model_behavior"], "not_measured")
                self.assertIsNone(candidate["model_report"])
                self.assertEqual(candidate["model_requests"], 0)

    def test_fixture_is_unchanged_and_archive_is_hash_checkable(self):
        after = {path.name: evidence_boundary.digest(path) for path in self.source.iterdir()}
        self.assertEqual(self.before, after)
        manifest = json.loads((self.output / "file-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 29)  # 26 complete requests, 2 SQL snapshots, report.
        self.assertTrue(all(evidence_boundary.digest(self.output / name) == sha for name, sha in manifest.items()))
        with self.assertRaises(FileExistsError):
            evidence_boundary.run(self.output, self.source)
        with self.assertRaises(ValueError):
            evidence_boundary.run(self.source / "forbidden", self.source)

    def test_changed_database_and_updated_manifest_cannot_keep_old_source_story(self):
        with closing(sqlite3.connect(self.source / "E0.sqlite")) as database:
            database.execute("INSERT INTO work_orders VALUES (1,'eq-demo-001','教学楼门禁故障','normal','active')")
            database.commit()
        manifest_path = self.source / "file-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["E0.sqlite"] = evidence_boundary.digest(self.source / "E0.sqlite")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "真值"):
            evidence_boundary.run(Path(self.temporary.name) / "changed", self.source)

    def test_limitations_do_not_claim_model_response_or_global_logical_minimality(self):
        self.assertEqual(self.report["model_behavior"], "not_measured")
        self.assertEqual(self.report["model_requests"], 0)
        limitations = " ".join(self.report["limitations"])
        self.assertIn("信息论上必要", limitations)
        self.assertIn("正记录", limitations)
        self.assertIn("不代表唯一、全局", limitations)
        self.assertIn("model_behavior=not_measured", limitations)


if __name__ == "__main__":
    unittest.main()
