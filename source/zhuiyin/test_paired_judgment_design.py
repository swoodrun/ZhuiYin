"""Frozen judgment contracts: genuine rereads, no hidden truth, fixed plan."""
from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from paired_judgment_design import (BRANCH_IDS, CONTROLLED_BOUNDARY, DEFAULT_SOURCE,
                                    JUDGMENT_INSTRUCTION, WORLD_IDS, build_design)
from tools.verify_paired_evidence import canonical, readonly


def file_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(root).rglob("*") if path.is_file()}


class PairedJudgmentDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = build_design()

    def setUp(self):
        self.design = deepcopy(self.original)
        self.branches = {branch["branch_id"]: branch for branch in self.design["branches"]}

    def copied_source(self):
        temp = tempfile.TemporaryDirectory(prefix="frozen-judgment-")
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / "source"
        shutil.copytree(DEFAULT_SOURCE, target)
        return target

    def world(self, branch, world_id):
        return next(world for world in self.branches[branch]["worlds"] if world["world_id"] == world_id)

    def test_source_passes_existing_independent_checks_before_design(self):
        self.assertEqual({"passed": True, "check_count": 86}, self.design["source_verification"])
        self.assertEqual("paired-evidence-v1", self.design["source_protocol"])
        self.assertEqual("frozen_state_judgment", self.design["task_mode"])

    def test_all_source_files_are_preserved_and_no_new_output_created(self):
        source = self.copied_source()
        before = file_hashes(source)
        build_design(source)
        self.assertEqual(before, file_hashes(source))

    def test_no_model_request_is_made(self):
        with patch("engine.model_request", side_effect=AssertionError("design is offline")):
            result = build_design()
        self.assertEqual("not_run", result["model_observation_status"])
        self.assertEqual(0, result["model_requests"])

    def test_thirty_samples_fit_remaining_request_allowance_without_retry(self):
        self.assertEqual(30, self.design["planned_requests"])
        self.assertEqual(30, len(self.design["execution_plan"]))
        self.assertLessEqual(self.design["planned_requests"], 73)
        self.assertEqual({1}, {row["max_model_requests"] for row in self.design["execution_plan"]})
        self.assertEqual("disabled", self.design["business_tool_execution"])

    def test_plan_has_all_six_conditions_exactly_five_times(self):
        counts = Counter((row["branch_id"], row["world_id"]) for row in self.design["execution_plan"])
        self.assertEqual({(branch, world): 5 for branch in BRANCH_IDS for world in WORLD_IDS}, counts)
        self.assertEqual(list(range(1, 31)), [row["sample_index"] for row in self.design["execution_plan"]])

    def test_each_block_has_six_cells_and_positions_are_incompletely_balanced(self):
        blocks, positions = defaultdict(set), defaultdict(set)
        for row in self.design["execution_plan"]:
            cell = (row["branch_id"], row["world_id"])
            blocks[row["repetition"]].add(cell)
            positions[cell].add(row["block_position"])
        self.assertEqual([6] * 5, [len(blocks[index]) for index in range(1, 6)])
        self.assertEqual({5}, {len(values) for values in positions.values()})
        self.assertFalse(self.design["order_balance"]["complete_position_balance"])

    def test_fixed_seed_plan_is_repeatable_without_global_random_side_effect(self):
        state = random.getstate()
        self.assertEqual(self.design, build_design())
        self.assertEqual(state, random.getstate())

    def test_ambiguous_worlds_keep_identical_full_original_payloads(self):
        report = json.loads((DEFAULT_SOURCE / "实验报告.json").read_text(encoding="utf-8"))
        for world in report["worlds"]:
            actual = self.world("ambiguous", world["world_id"])["payload"]
            self.assertEqual(world["checkpoints"][3]["payload"], actual)
        self.assertTrue(self.branches["ambiguous"]["world_contexts_identical"])

    def test_all_branches_share_exact_system_task_and_tool_contracts(self):
        baseline = self.world("ambiguous", "committed")["payload"]
        for branch in BRANCH_IDS:
            for world_id in WORLD_IDS:
                payload = self.world(branch, world_id)["payload"]
                self.assertEqual(baseline["messages"][:2], payload["messages"][:2])
                self.assertEqual({k: v for k, v in baseline.items() if k != "messages"},
                                 {k: v for k, v in payload.items() if k != "messages"})

    def test_three_branches_have_same_prefix_and_same_next_call_id(self):
        prefix = self.world("ambiguous", "committed")["payload"]["messages"]
        for branch in ("ordinary_reread", "reliable_verification"):
            for world_id in WORLD_IDS:
                payload = self.world(branch, world_id)["payload"]
                self.assertEqual(prefix, payload["messages"][:-2])
                self.assertEqual("pair-call-4", payload["messages"][-2]["tool_calls"][0]["id"])
                self.assertEqual("pair-call-4", payload["messages"][-1]["tool_call_id"])

    def test_ordinary_reread_reads_real_database_and_does_not_invent_authority(self):
        for world_id, expected_count in (("committed", 1), ("not_committed", 0)):
            row = self.world("ordinary_reread", world_id)
            message = row["payload"]["messages"][-1]
            response = json.loads(message["content"])
            self.assertEqual({"work_orders"}, set(response))
            self.assertEqual(expected_count, len(response["work_orders"]))
            call = row["payload"]["messages"][-2]["tool_calls"][0]
            self.assertEqual("find_ticket", call["function"]["name"])
            with closing(readonly(DEFAULT_SOURCE / row["database_file"])) as db:
                real = [dict(item) for item in db.execute("SELECT * FROM work_orders ORDER BY id")]
            self.assertEqual(real, response["work_orders"])

    def test_source_databases_are_opened_read_only_for_additional_branch(self):
        statements = []
        def observe(path):
            db = readonly(path)
            self.assertEqual(1, db.execute("PRAGMA query_only").fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM work_orders")
            db.set_trace_callback(statements.append)
            return db
        with patch("paired_judgment_design.readonly", side_effect=observe) as opener:
            build_design()
        self.assertEqual(2, opener.call_count)
        self.assertEqual(2, len(statements))
        self.assertTrue(all(item.startswith("SELECT ") for item in statements))

    def test_reliable_branch_preserves_original_checkpoint_and_metadata(self):
        report = json.loads((DEFAULT_SOURCE / "实验报告.json").read_text(encoding="utf-8"))
        for world in report["worlds"]:
            row = self.world("reliable_verification", world["world_id"])
            self.assertEqual(world["checkpoints"][4]["payload"], row["payload"])
            receipt = json.loads(row["payload"]["messages"][-1]["content"])
            self.assertEqual({"complete": True, "consistency": "authoritative"}, receipt["snapshot"])

    def test_shared_controlled_boundary_enables_positive_ordinary_evidence(self):
        self.assertIn(CONTROLLED_BOUNDARY, JUDGMENT_INSTRUCTION)
        self.assertEqual(CONTROLLED_BOUNDARY, self.design["controlled_boundary"])
        self.assertNotIn("缓存已恢复", JUDGMENT_INSTRUCTION)
        a = self.world("ordinary_reread", "committed")
        self.assertEqual("success", a["supported_outcome"])
        self.assertTrue(a["positive_record_observed"])
        self.assertTrue(a["evidence_contract"]["current_state_sufficient"])
        self.assertFalse(a["evidence_contract"]["absence_sufficient"])

    def test_empty_ordinary_read_cannot_prove_absence(self):
        b = self.world("ordinary_reread", "not_committed")
        self.assertEqual("unknown", b["supported_outcome"])
        self.assertFalse(b["positive_record_observed"])
        self.assertFalse(b["evidence_contract"]["current_state_sufficient"])
        self.assertFalse(b["evidence_contract"]["absence_sufficient"])

    def test_ambiguous_unknown_and_reliable_failure_are_different_contracts(self):
        for world_id in WORLD_IDS:
            self.assertEqual("unknown", self.world("ambiguous", world_id)["supported_outcome"])
        self.assertEqual("success", self.world("reliable_verification", "committed")["supported_outcome"])
        b = self.world("reliable_verification", "not_committed")
        self.assertEqual("failure", b["supported_outcome"])
        self.assertTrue(b["evidence_contract"]["absence_sufficient"])
        self.assertFalse(b["actual_success"])

    def test_frozen_truth_does_not_change_across_evidence_branches(self):
        for branch in BRANCH_IDS:
            self.assertTrue(self.world(branch, "committed")["actual_success"])
            self.assertFalse(self.world(branch, "not_committed")["actual_success"])

    def test_evidence_support_uses_visible_response_not_hidden_truth_summary(self):
        from paired_judgment_design import _rows_and_truth
        def false_hidden_flag(*args):
            rows, actual = _rows_and_truth(*args)
            return rows, not actual
        with patch("paired_judgment_design._rows_and_truth", side_effect=false_hidden_flag):
            result = build_design()
        worlds = {branch["branch_id"]: {row["world_id"]: row for row in branch["worlds"]}
                  for branch in result["branches"]}
        self.assertEqual("success", worlds["ordinary_reread"]["committed"]["supported_outcome"])
        self.assertEqual("unknown", worlds["ordinary_reread"]["not_committed"]["supported_outcome"])
        self.assertEqual("success", worlds["reliable_verification"]["committed"]["supported_outcome"])
        self.assertEqual("failure", worlds["reliable_verification"]["not_committed"]["supported_outcome"])

    def test_visible_payload_never_contains_world_labels_or_judge_truth(self):
        for branch in BRANCH_IDS:
            for world_id in WORLD_IDS:
                payload = self.world(branch, world_id)["payload"]
                visible = canonical(payload).decode("utf-8") + JUDGMENT_INSTRUCTION
                for hidden in ('"actual_success"', '"world_id"', '"database_file"',
                               '"supported_outcome"', "world-committed.sqlite", "precommit_timeout"):
                    self.assertNotIn(hidden, visible)

    def test_instruction_distinguishes_judgment_from_action_and_no_report(self):
        self.assertIn("不继续完成或修复任务", JUDGMENT_INSTRUCTION)
        self.assertIn("不会执行任何业务工具", JUDGMENT_INSTRUCTION)
        self.assertIn("不评价是否应继续取证", JUDGMENT_INSTRUCTION)
        self.assertIn("not", build_design.__doc__)

    def test_each_payload_digest_recomputes_from_full_payload(self):
        for branch in self.design["branches"]:
            for world in branch["worlds"]:
                self.assertEqual(hashlib.sha256(canonical(world["payload"])).hexdigest(), world["payload_sha256"])

    def test_hash_mismatch_is_rejected_before_design(self):
        source = self.copied_source()
        (source / "实验报告.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_design(source)

    def test_self_consistent_hash_update_cannot_hide_false_world_summary(self):
        source = self.copied_source()
        report_file = source / "实验报告.json"
        report = json.loads(report_file.read_text(encoding="utf-8"))
        report["worlds"][0]["actual_success"] = False
        report_file.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        manifest_file = source / "file-manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["实验报告.json"] = hashlib.sha256(report_file.read_bytes()).hexdigest()
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_design(source)

    def test_source_change_during_build_is_rejected(self):
        source = self.copied_source()
        from paired_judgment_design import _rows_and_truth
        mutated = False
        def mutate_after_read(*args):
            nonlocal mutated
            rows = _rows_and_truth(*args)
            if not mutated:
                target = source / "source_snapshot/engine.py"
                target.write_bytes(target.read_bytes() + b"\n# changed during design\n")
                mutated = True
            return rows
        with patch("paired_judgment_design._rows_and_truth", side_effect=mutate_after_read):
            with self.assertRaises(ValueError):
                build_design(source)


if __name__ == "__main__":
    unittest.main()
