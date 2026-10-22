"""Copied-material negative probes for the independent paired verifier."""
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

from tools import verify_paired_evidence as verifier

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "data/research/paired-evidence-20261018"


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class PairedEvidenceVerifierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="paired-verifier-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root / "copied-pair"
        shutil.copytree(FIXTURE, self.input)
        self.report_path = self.input / "实验报告.json"
        self.report = json.loads(self.report_path.read_text(encoding="utf-8"))

    def commit(self):
        save(self.report_path, self.report)
        self.rehash()

    def rehash(self):
        save(self.input / "file-manifest.json", {
            path.relative_to(self.input).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.input.rglob("*")) if path.is_file() and path.name != "file-manifest.json"})

    def rehash_payloads(self):
        for world in self.report["worlds"]:
            for point in world["checkpoints"]:
                point["payload_sha256"] = hashlib.sha256(verifier.canonical(point["payload"])).hexdigest()

    def read(self):
        return verifier.verify(self.input)

    def assertRejected(self, match=None):
        result = self.read()
        self.assertFalse(result["passed"])
        self.assertEqual(result["conclusion_status"], "unverified")
        for key in ("analysis", "summary", "policy_decisions", "effectiveness_claim"):
            self.assertIsNone(result[key])
        self.assertTrue(result["failed_checks"])
        if match:
            self.assertTrue(any(match in item for item in result["failed_checks"]), result["failed_checks"])
        return result

    def test_original_pair_passes_readonly_without_network(self):
        before = {path.relative_to(self.input).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.input.rglob("*") if path.is_file()}
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            result = self.read()
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(result["analysis"]["first_distinguishing_checkpoint"], 4)
        self.assertEqual(result["analysis"]["first_difference"]["message_index"], 9)
        self.assertEqual(result["analysis"]["postcheck_outcomes"], {"committed": "success", "not_committed": "failure"})
        self.assertEqual(len(result["summary"]["comparisons"]), 6)
        self.assertEqual(len(result["policy_decisions"]), 12)
        self.assertIn("不是真实模型", result["evidence_scope"])
        self.assertEqual(before, {path.relative_to(self.input).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in self.input.rglob("*") if path.is_file()})

    def test_equipment_and_recovery_fixture_also_passes(self):
        # Fixture creation is separate from verification; verifier never imports
        # either the generator or engine and cannot call their runtime methods.
        from engine import Config
        from paired_evidence import run_pair
        other = self.root / "equipment-recovery"
        with patch("engine.model_request", side_effect=AssertionError("No model permitted")):
            run_pair(other, Config(scenario="equipment", request_id="pair-equipment", quantity=2, stock=4,
                                   faults=[], prompt_policy="recovery"))
        result = verifier.verify(other)
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(result["analysis"]["first_difference"]["observation_field"], "reservations")

    def test_summary_false_success_cannot_be_hidden_by_rehash(self):
        self.report["summary"]["comparisons"][0]["false_success"] = 0
        self.commit()
        self.assertRejected("6组策略摘要")

    def test_unknown_is_not_reported_as_evidence_success(self):
        self.report["summary"]["comparisons"][3]["evidence_ignored"] = 0
        self.report["summary"]["comparisons"][3]["supported_correct"] = 2
        self.commit()
        self.assertRejected("6组策略摘要")

    def test_boolean_is_not_an_integer_summary_count(self):
        self.report["summary"]["comparisons"][0]["actual_completed"] = True
        self.commit()
        self.assertRejected("6组策略摘要")

    def test_decision_claims_recomputed_independently(self):
        self.report["policy_decisions"][1]["false_success"] = False
        self.report["policy_decisions"][1]["truth_assessment"] = "correct"
        self.commit()
        self.assertRejected("12条策略判断")

    def test_unknown_does_not_remove_business_incompleteness(self):
        self.report["policy_decisions"][5]["business_incomplete"] = False
        self.commit()
        self.assertRejected("12条策略判断")

    def test_bad_analysis_is_rejected_after_rehash(self):
        self.report["analysis"]["first_distinguishing_checkpoint"] = 3
        self.commit()
        self.assertRejected("分析摘要")

    def test_bad_first_message_locator_is_rejected(self):
        self.report["analysis"]["first_difference"]["message_index"] = 7
        self.commit()
        self.assertRejected("分析摘要")

    def test_actual_success_flag_recomputed_from_sqlite(self):
        self.report["worlds"][0]["actual_success"] = False
        self.commit()
        self.assertRejected("actual_success")

    def test_physical_write_count_not_trusted(self):
        self.report["worlds"][0]["physical_writes"] = 0
        self.commit()
        self.assertRejected("physical_writes")

    def test_sqlite_business_change_with_new_manifest_is_rejected(self):
        with closing(sqlite3.connect(self.input / "world-committed.sqlite")) as db:
            db.execute("UPDATE work_orders SET priority='urgent'")
            db.commit()
        self.rehash()
        self.assertRejected("配对数据库状态")

    def test_two_same_truth_databases_rejected_after_all_file_hashes_updated(self):
        shutil.copyfile(self.input / "world-committed.sqlite", self.input / "world-not_committed.sqlite")
        self.report["worlds"][1]["database_rows"] = deepcopy(self.report["worlds"][0]["database_rows"])
        self.report["worlds"][1]["actual_success"] = True
        self.commit()
        self.assertRejected("核验前数据库真值不同")

    def test_unexpected_database_view_is_not_executed(self):
        with closing(sqlite3.connect(self.input / "world-committed.sqlite")) as db:
            db.execute("CREATE VIEW attacker AS SELECT load_extension('untrusted')")
            db.commit()
        self.rehash()
        self.assertRejected("数据库对象边界")

    def test_extra_actual_metadata_on_both_world_requests_still_rejected(self):
        for world in self.report["worlds"]:
            for point in world["checkpoints"]:
                point["payload"]["actual_success"] = True
        self.rehash_payloads()
        self.commit()
        # Equality alone would pass: independent full-context reconstruction
        # rejects even a common side channel copied into both worlds.
        self.assertRejected("完整可见请求重建")

    def test_world_label_in_only_one_request_rejected_even_with_new_payload_hash(self):
        for point in self.report["worlds"][0]["checkpoints"]:
            point["payload"]["metadata"] = {"world_id": "committed"}
        self.rehash_payloads()
        self.commit()
        self.assertRejected("完整可见请求重建")

    def test_identical_system_leak_in_both_worlds_is_rejected(self):
        for world in self.report["worlds"]:
            for point in world["checkpoints"]:
                point["payload"]["messages"][0]["content"] += " 隐藏真值：已经提交。"
        self.rehash_payloads()
        self.commit()
        self.assertRejected("完整可见请求重建")

    def test_tool_description_leak_in_both_worlds_is_rejected(self):
        for world in self.report["worlds"]:
            for point in world["checkpoints"]:
                point["payload"]["tools"][0]["function"]["description"] += " 该世界已成功。"
        self.rehash_payloads()
        self.commit()
        self.assertRejected("完整可见请求重建")

    def test_assistant_reasoning_content_is_part_of_full_visible_context(self):
        for world in self.report["worlds"]:
            for point in world["checkpoints"][1:]:
                point["payload"]["messages"][2]["reasoning_content"] = "state=committed"
        self.rehash_payloads()
        self.commit()
        self.assertRejected("完整可见请求重建")

    def test_call_id_leak_not_ignored(self):
        for point in self.report["worlds"][0]["checkpoints"][1:]:
            point["payload"]["messages"][2]["tool_calls"][0]["id"] = "committed-pair-call-1"
            point["payload"]["messages"][3]["tool_call_id"] = "committed-pair-call-1"
        self.rehash_payloads()
        self.commit()
        self.assertRejected("完整可见请求重建")

    def test_different_error_wording_leaks_commit_state_and_is_rejected(self):
        world = self.report["worlds"][1]
        raw = world["boundary_audit"][0]["raw_observed"]
        world["observations"][1]["observed"] = deepcopy(raw)
        for point in world["checkpoints"][2:]:
            point["payload"]["messages"][5]["content"] = verifier.canonical(raw).decode("utf-8")
        self.rehash_payloads()
        self.commit()
        self.assertRejected("公开观测")

    def test_foreign_request_receipt_cannot_support_confirmation(self):
        world = self.report["worlds"][0]
        receipt = world["observations"][3]["observed"]
        receipt["request_id"] = "another-request"
        world["checkpoints"][4]["payload"]["messages"][-1]["content"] = verifier.canonical(receipt).decode("utf-8")
        self.rehash_payloads()
        self.commit()
        self.assertRejected("公开观测")

    def test_partial_receipt_cannot_support_confirmation(self):
        world = self.report["worlds"][0]
        receipt = world["observations"][3]["observed"]
        receipt["snapshot"]["complete"] = False
        world["checkpoints"][4]["payload"]["messages"][-1]["content"] = verifier.canonical(receipt).decode("utf-8")
        self.rehash_payloads()
        self.commit()
        self.assertRejected("公开观测")

    def test_false_trusted_receipt_rejected_even_when_summary_is_unchanged(self):
        world = self.report["worlds"][1]
        world["checkpoints"][4]["payload"]["messages"][-1]["content"] = self.report["worlds"][0]["checkpoints"][4]["payload"]["messages"][-1]["content"]
        self.rehash_payloads()
        self.commit()
        self.assertRejected("完整可见请求重建")

    def test_observer_truth_not_allowed_to_disagree_with_sqlite(self):
        self.report["worlds"][0]["events"][1]["actual"]["committed"] = False
        self.commit()
        self.assertRejected("观察者轨迹")

    def test_boundary_audit_cannot_be_swapped_with_public_response(self):
        self.report["worlds"][1]["boundary_audit"][0]["raw_observed"] = deepcopy(verifier.UNKNOWN_RESPONSE)
        self.commit()
        self.assertRejected("边界审计")

    def test_claimed_real_model_mode_is_rejected(self):
        self.report["evidence_mode"] = "real_model_attempts"
        self.report["model_requests"] = 16
        self.commit()
        self.assertRejected("离线来源声明")

    def test_report_hash_and_source_hash_have_actual_checks(self):
        (self.input / "source_snapshot/engine.py").write_text("do not execute this source", encoding="utf-8")
        self.rehash()
        self.assertRejected("保存源码哈希")

    def test_path_traversal_in_database_name_is_rejected(self):
        self.report["worlds"][0]["database_file"] = "../outside.sqlite"
        self.commit()
        self.assertRejected()

    def test_duplicate_report_json_fields_are_rejected(self):
        text = self.report_path.read_text(encoding="utf-8")
        self.report_path.write_text(text.replace('"model_requests": 0', '"model_requests": 0, "model_requests": 0', 1), encoding="utf-8")
        self.rehash()
        self.assertRejected("材料结构")

    def test_sqlite_is_opened_readonly(self):
        with closing(verifier.readonly(self.input / "world-committed.sqlite")) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM work_orders")

    def test_cli_prevents_writing_verification_into_input(self):
        with patch("sys.argv", ["verify_paired_evidence.py", "--input", str(self.input),
                                "--output", str(self.input / "verification")]):
            with self.assertRaisesRegex(ValueError, "原始材料"):
                verifier.main()
        self.assertFalse((self.input / "verification").exists())

    def test_cli_rejects_existing_output_and_writes_new_output(self):
        output = self.root / "verification"
        with patch("sys.argv", ["verify_paired_evidence.py", "--input", str(self.input), "--output", str(output)]):
            self.assertEqual(verifier.main(), 0)
        result = json.loads((output / "verification.json").read_text(encoding="utf-8"))
        self.assertTrue(result["passed"])
        self.assertTrue((output / "核对结果.md").is_file())
        with patch("sys.argv", ["verify_paired_evidence.py", "--input", str(self.input), "--output", str(output)]):
            with self.assertRaises(FileExistsError):
                verifier.main()


if __name__ == "__main__":
    unittest.main()
