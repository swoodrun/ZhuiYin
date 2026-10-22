"""Retest rules survive deduplication and reject ambiguous historical versions."""
import copy
from pathlib import Path
import tempfile
import unittest

from engine import Config, run_trial
from product_workflows import prepare_retest
from retest_templates import attach_retest_templates, business_identity, decorate_retest_trial
from task_templates import TemplateStore
from test_suite import prepare_uploads, public_plan


class RetestTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TemplateStore(Path(self.tmp.name) / "templates.sqlite")
        self.template = self.store.create({"name": "重测规则", "config": {
            "scenario": "equipment", "equipment": "会议投影仪", "stock": 6, "quantity": 2,
            "request_id": "retest-001", "seed": 42, "faults": [], "agent_mode": "llm",
            "prompt_policy": "recovery", "max_requests": 30}})

    def candidate(self, candidate_id="old-001", template=None, **config_changes):
        config = copy.deepcopy(self.template["config"])
        config.update(config_changes)
        config["template_snapshot"] = copy.deepcopy(self.template if template is None else template)
        return {"candidate_id": candidate_id, "eligible": True, "failed": True,
                "name": "待重测预约", "config": config}

    def preview(self, candidates, selected=None):
        selected = selected or [candidate["candidate_id"] for candidate in candidates]
        files, _ = prepare_retest({"run_id": "history-001"}, selected, candidates)
        return prepare_uploads(files), selected

    def test_original_snapshot_survives_new_preview_and_public_serialization(self):
        candidates = [self.candidate()]
        plan, selected = self.preview(candidates)
        before_plan, before_candidates = copy.deepcopy(plan), copy.deepcopy(candidates)
        attached = attach_retest_templates(plan, selected, candidates)
        self.assertEqual(attached["cases"][0]["template_snapshot"], self.template)
        self.assertEqual(public_plan(attached)["cases"][0]["template_snapshot"], self.template)
        self.assertEqual(attached["items"][0]["template_snapshot"], self.template)
        self.assertEqual(plan, before_plan)
        self.assertEqual(candidates, before_candidates)
        attached["cases"][0]["template_snapshot"]["contract"]["stock_delta"] = 99
        self.assertEqual(candidates[0]["config"]["template_snapshot"]["contract"]["stock_delta"], -2)

    def test_same_rules_merge_without_losing_version_when_policy_budget_change(self):
        first = self.candidate("old-001")
        second = self.candidate("old-002", prompt_policy="neutral", max_requests=200, agent_mode="rules", billing={})
        plan, selected = self.preview([first, second])
        attached = attach_retest_templates(plan, selected, [first, second])
        self.assertEqual(len(attached["cases"]), 1)
        self.assertEqual(attached["cases"][0]["template_snapshot"]["version"], 1)

    def test_fault_order_is_semantic_and_seed_stays_in_business_identity(self):
        left = {**self.template["config"], "faults": ["latency", "stale_read"]}
        right = {**left, "faults": ["stale_read", "latency"], "prompt_policy": "neutral", "max_requests": 1}
        self.assertEqual(business_identity(left), business_identity(right))
        right["seed"] = 99
        self.assertNotEqual(business_identity(left), business_identity(right))

    def test_different_historical_versions_cannot_silently_merge(self):
        updated = self.store.update(self.template["id"], {"name": "新版规则", "config": self.template["config"],
            "contract": {**self.template["contract"], "record_count": {"mode": "exactly", "value": 2}}})
        candidates = [self.candidate("old-001"), self.candidate("old-002", updated)]
        plan, selected = self.preview(candidates)
        with self.assertRaisesRegex(ValueError, "不同模板版本"):
            attach_retest_templates(plan, selected, candidates)

    def test_same_version_with_different_rules_is_also_rejected(self):
        changed = copy.deepcopy(self.template)
        changed["contract"]["stock_delta"] = 0
        candidates = [self.candidate("old-001"), self.candidate("old-002", changed)]
        plan, selected = self.preview(candidates)
        with self.assertRaisesRegex(ValueError, "不同模板版本"):
            attach_retest_templates(plan, selected, candidates)

    def test_rule_and_no_rule_selection_is_not_silently_combined(self):
        first, second = self.candidate("old-001"), self.candidate("old-002")
        second["config"].pop("template_snapshot")
        plan, selected = self.preview([first, second])
        with self.assertRaisesRegex(ValueError, "不同模板版本"):
            attach_retest_templates(plan, selected, [first, second])

    def test_invalid_credentials_or_wrong_task_snapshot_are_rejected(self):
        for modify in (lambda value: value["config"].update(api_key="secret"),
                       lambda value: value["config"].update(request_id="another-task")):
            candidate = self.candidate()
            modify(candidate["config"]["template_snapshot"])
            plan, selected = self.preview([candidate])
            with self.assertRaises(ValueError):
                attach_retest_templates(plan, selected, [candidate])

    def test_existing_conflicting_case_or_missing_preview_task_is_rejected(self):
        candidates = [self.candidate()]
        plan, selected = self.preview(candidates)
        changed = copy.deepcopy(self.template)
        changed["version"] = 2
        plan["cases"][0]["template_snapshot"] = changed
        with self.assertRaises(ValueError):
            attach_retest_templates(plan, selected, candidates)
        plan["cases"] = []
        with self.assertRaisesRegex(ValueError, "未出现在重测预览"):
            attach_retest_templates(plan, selected, candidates)

    def test_unselected_versions_do_not_create_false_conflict(self):
        changed = copy.deepcopy(self.template)
        changed["version"] = 2
        candidates = [self.candidate("chosen"), self.candidate("not-chosen", changed)]
        plan, selected = self.preview(candidates, ["chosen"])
        self.assertEqual(attach_retest_templates(plan, selected, candidates)["cases"][0]["template_snapshot"]["version"], 1)

    def test_retest_uses_old_rules_after_template_edit_and_delete(self):
        candidate = self.candidate()
        plan, selected = self.preview([candidate])
        plan = attach_retest_templates(plan, selected, [candidate])
        updated = self.store.update(self.template["id"], {"name": "之后的规则", "config": self.template["config"],
            "contract": {"record_count": {"mode": "exactly", "value": 2}}})
        self.store.delete(updated["id"])
        cfg = Config.from_dict({**plan["cases"][0]["config"], "agent_mode": "rules"})
        trial = run_trial(cfg, [], "baseline", "rules")
        before = copy.deepcopy(trial)
        decorated = decorate_retest_trial(trial, plan["cases"][0], cfg)
        self.assertEqual(decorated["template_contract"]["status"], "pass")
        self.assertEqual(plan["cases"][0]["template_snapshot"]["version"], 1)
        self.assertEqual(trial, before)
        self.assertEqual(decorated["actual_success"], trial["actual_success"])
        self.assertEqual(decorated["failed"], trial["failed"])

    def test_rule_failure_and_missing_final_records_stay_visible(self):
        template = copy.deepcopy(self.template)
        template["contract"]["record_count"]["value"] = 2
        candidate = self.candidate(template=template)
        plan, selected = self.preview([candidate])
        case = attach_retest_templates(plan, selected, [candidate])["cases"][0]
        cfg = Config.from_dict({**case["config"], "agent_mode": "rules"})
        trial = run_trial(cfg, [], "baseline", "rules")
        decorated = decorate_retest_trial(trial, case, cfg)
        self.assertTrue(decorated["actual_success"])
        self.assertFalse(decorated["failed"])
        self.assertEqual(decorated["template_contract"]["status"], "fail")
        self.assertEqual(decorate_retest_trial({"completed": False}, case, cfg)["template_contract"]["status"], "unknown")

    def test_different_execution_task_cannot_borrow_rules(self):
        candidate = self.candidate()
        plan, selected = self.preview([candidate])
        case = attach_retest_templates(plan, selected, [candidate])["cases"][0]
        cfg = Config.from_dict({**case["config"], "request_id": "different", "agent_mode": "rules"})
        with self.assertRaisesRegex(ValueError, "当前执行任务"):
            decorate_retest_trial({}, case, cfg)

    def test_cases_without_historical_templates_are_unchanged(self):
        candidate = self.candidate()
        candidate["config"].pop("template_snapshot")
        plan, selected = self.preview([candidate])
        attached = attach_retest_templates(plan, selected, [candidate])
        self.assertNotIn("template_snapshot", attached["cases"][0])
        original = {"completed": True, "business_records": []}
        self.assertEqual(decorate_retest_trial(original, attached["cases"][0], {}), original)


if __name__ == "__main__":
    unittest.main()
