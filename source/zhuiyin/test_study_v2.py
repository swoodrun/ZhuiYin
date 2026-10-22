"""Offline checks for three-valued reports and independent prompt/tool factors."""
import copy
import unittest
from unittest.mock import patch

import engine
import study


def evaluated(outcome="success", actual=True, business=False, reporting=False,
              completed=True, infra=False):
    business_violations = [{"code": "TASK_INCOMPLETE", "message": "任务未完成"}] if business else []
    reporting_violations = [{"code": "REPORT_MISMATCH", "message": "报告不一致"}] if reporting else []
    execution_violations = [{"code": "AGENT_DID_NOT_FINISH", "message": "没有完成报告"}] if infra else []
    violations = business_violations + reporting_violations + execution_violations
    return {"completed": completed, "reported_outcome": outcome,
            "reported_success": True if outcome == "success" else False if outcome == "failure" else None,
            "report_assessment": "unreported" if outcome is None else "uncertain" if outcome == "unknown"
                                 else "inconsistent" if reporting else "consistent",
            "actual_success": actual, "business_failed": business, "report_failed": reporting,
            "failed": bool(violations), "violations": violations,
            "business_violations": business_violations, "reporting_violations": reporting_violations,
            "execution_violations": execution_violations,
            "failure_fingerprint": "|".join(sorted(value["code"] for value in violations)), "stats": {},
            "evaluation_protocol": "business-report-v2"}


class PromptFactorTests(unittest.TestCase):
    def test_prompt_options_default_deduplicate_and_reject_invalid_policies(self):
        self.assertEqual(study.validate_study_options({})["prompt_policies"], ["neutral"])
        self.assertEqual(study.validate_study_options({"prompt_policies": ["recovery", "recovery", "neutral"]})[
            "prompt_policies"], ["recovery", "neutral"])
        for invalid in (None, [], "neutral", ["adaptive"], [True], [{}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                study.validate_study_options({"prompt_policies": invalid})

    def test_default_still_has_four_arms_and_two_repetitions_mean_eight_trials(self):
        with patch.object(engine, "run_trial", return_value=evaluated()):
            report = study.run_study(engine.Config(), {"repetitions": 2})
        self.assertEqual((report["planned"], report["completed"], len(report["arms"])), (8, 8, 4))
        self.assertEqual({arm["prompt_policy"] for arm in report["arms"]}, {"neutral"})
        self.assertEqual(report["evaluation_protocol"], "business-report-v2")

    def test_two_prompt_layers_are_independent_balanced_groups_without_mutating_input(self):
        cfg = engine.Config()
        original = copy.deepcopy(cfg.public())
        calls = []

        def execute(config, faults, strategy, **kwargs):
            calls.append((config.prompt_policy, strategy, config.seed, tuple(faults)))
            return evaluated()

        with patch.object(engine, "run_trial", side_effect=execute):
            report = study.run_study(cfg, {"repetitions": 4, "prompt_policies": ["neutral", "recovery"]})
        self.assertEqual((report["planned"], report["completed"], len(report["arms"])), (32, 32, 8))
        self.assertEqual(cfg.public(), original)
        self.assertTrue(all(call[2:] == (cfg.seed, tuple(cfg.faults)) for call in calls))
        self.assertEqual([(sample["prompt_policy"], sample["strategy"]) for sample in report["samples"]],
                         [(call[0], call[1]) for call in calls])
        for policy in ("neutral", "recovery"):
            arms = [arm for arm in report["arms"] if arm["prompt_policy"] == policy]
            self.assertTrue(all(arm["valid_trials"] == 4 and arm["planned"] == 4 for arm in arms))
            for strategy in study.STRATEGIES:
                group = [sample for sample in report["samples"] if sample["prompt_policy"] == policy
                         and sample["strategy"] == strategy]
                self.assertEqual(sorted(sample["order_position"] for sample in group), [1, 2, 3, 4])
            block_positions = [next(item["policy_order_position"] for item in row["trials"]
                                    if item["prompt_policy"] == policy) for row in report["execution_plan"]]
            self.assertEqual(sorted(block_positions), [1, 1, 2, 2])
        self.assertEqual(report["factor_design"]["group_by"], ["prompt_policy", "strategy"])
        self.assertEqual(report["factor_design"]["strategy_capabilities"]["verification_only"],
                         {"idempotency": False, "verification": True})
        self.assertIn("system_prompt", report["factor_design"]["fixed_within_policy"])

    def test_different_prompt_results_never_share_a_denominator(self):
        def execute(config, *args, **kwargs):
            return evaluated("unknown", actual=False, business=True) if config.prompt_policy == "neutral" else evaluated()

        with patch.object(engine, "run_trial", side_effect=execute):
            report = study.run_study(engine.Config(), {"repetitions": 2, "strategies": ["baseline"],
                                                      "prompt_policies": ["neutral", "recovery"]})
        neutral, recovery = report["arms"]
        self.assertEqual(neutral["uncertain_report_rate"]["rate"], 1)
        self.assertEqual(neutral["business_effect_failure_rate"]["rate"], 1)
        self.assertEqual(recovery["uncertain_report_rate"]["rate"], 0)
        self.assertEqual(recovery["confirmed_completion_rate"]["rate"], 1)
        self.assertEqual(neutral["task_completion_rate"]["denominator"], 2)
        self.assertEqual(recovery["task_completion_rate"]["denominator"], 2)

    def test_both_prompt_layers_and_executed_sample_manifests_are_directly_auditable(self):
        def execute(config, faults, strategy, **kwargs):
            result = evaluated()
            result["factor_manifest"] = {**engine.agent_spec(config, strategy)["manifest"],
                                         "api_key": "OFFLINE_SECRET_MUST_NOT_PERSIST"}
            return result

        cfg = engine.Config(agent_mode="llm", model_config={"base_url": "http://localhost:9999/v1",
                            "model": "offline-test-only"})
        with patch.object(engine, "run_trial", side_effect=execute):
            report = study.run_study(cfg, {"repetitions": 2, "prompt_policies": ["neutral", "recovery"]})
        manifests = report["factor_design"]["manifests_by_prompt_policy"]
        hashes = {}
        for policy in ("neutral", "recovery"):
            hashes[policy] = {manifest["system_prompt_sha256"] for manifest in manifests[policy].values()}
            self.assertEqual(len(hashes[policy]), 1)
            self.assertEqual(set(manifests[policy]), set(study.STRATEGIES))
        self.assertNotEqual(hashes["neutral"], hashes["recovery"])
        for sample in report["samples"]:
            self.assertEqual(sample["factor_manifest"], manifests[sample["prompt_policy"]][sample["strategy"]])
            self.assertNotIn("api_key", sample["factor_manifest"])
            self.assertNotIn("OFFLINE_SECRET_MUST_NOT_PERSIST", str(sample["factor_manifest"]))

    def test_multi_policy_budget_truncation_keeps_each_arm_unrun_count(self):
        def execute(*args, **kwargs):
            engine.MODEL_CONTEXT.remaining -= 1
            engine.MODEL_CONTEXT.usage_records.append({"status": "ok", "prompt_tokens": 3,
                "completion_tokens": 2, "total_tokens": 5, "usage_available": True})
            return evaluated()

        cfg = engine.Config(agent_mode="llm", model_config={"base_url": "http://localhost:9999/v1",
                            "model": "offline-test-only"})
        with patch.object(engine, "run_trial", side_effect=execute):
            report = study.run_study(cfg, {"repetitions": 2, "strategies": ["baseline"], "max_requests": 3,
                                          "prompt_policies": ["neutral", "recovery"]})
        self.assertEqual((report["status"], report["planned"], report["completed"]), ("partial", 4, 3))
        self.assertEqual(report["model_usage"]["requests"], 3)
        self.assertEqual(sum(arm["attempted"] for arm in report["arms"]), 3)
        self.assertEqual(sum(arm["unrun"] for arm in report["arms"]), 1)
        self.assertTrue(all("prompt_policy" in row for row in report["execution_order"]))


class ThreeValuedStudyTests(unittest.TestCase):
    def test_unknown_is_a_valid_finished_trial_without_a_false_success_claim(self):
        with patch.object(engine, "run_trial", return_value=evaluated("unknown")):
            report = study.run_study(engine.Config(), {"repetitions": 2, "strategies": ["baseline"]})
        arm = report["arms"][0]
        self.assertEqual((arm["valid_trials"], arm["infra_errors"]), (2, 0))
        self.assertEqual(arm["task_completion_rate"]["rate"], 1)
        self.assertEqual(arm["confirmed_completion_rate"]["rate"], 0)
        self.assertEqual(arm["uncertain_report_rate"]["rate"], 1)
        self.assertEqual(arm["report_error_rate"]["rate"], 0)
        self.assertEqual(arm["business_effect_failure_rate"]["rate"], 0)
        self.assertEqual(arm["report_outcomes"], {"success": 0, "failure": 0, "unknown": 2})
        self.assertTrue(all(sample["completed"] and sample["reported_success"] is None
                            and sample["report_assessment"] == "uncertain" for sample in report["samples"]))

    def test_business_side_effect_failure_and_reporting_error_have_separate_counts(self):
        outcomes = [evaluated("unknown", actual=False, business=True),
                    evaluated("failure", actual=True, reporting=True),
                    evaluated("unknown"), evaluated()]
        with patch.object(engine, "run_trial", side_effect=outcomes):
            report = study.run_study(engine.Config(), {"repetitions": 4, "strategies": ["baseline"]})
        arm = report["arms"][0]
        self.assertEqual(arm["business_failure_rate"]["rate"], 1 / 2)
        self.assertEqual(arm["business_effect_failure_rate"]["rate"], 1 / 4)
        self.assertEqual(arm["report_error_rate"]["rate"], 1 / 4)
        self.assertEqual(arm["uncertain_report_rate"]["rate"], 1 / 2)
        self.assertEqual(arm["task_completion_rate"]["rate"], 3 / 4)
        self.assertEqual(arm["confirmed_completion_rate"]["rate"], 1 / 4)
        self.assertEqual(arm["business_effect_failure_rate"]["ci95"], study.wilson_interval(1, 4))
        self.assertEqual(report["samples"][0]["business_violations"][0]["code"], "TASK_INCOMPLETE")
        self.assertEqual(report["samples"][1]["reporting_violations"][0]["code"], "REPORT_MISMATCH")

    def test_no_report_and_explicit_incomplete_unknown_still_count_as_infra(self):
        outcomes = [evaluated(None, completed=False, infra=True),
                    evaluated("unknown", completed=False, infra=True)]
        with patch.object(engine, "run_trial", side_effect=outcomes):
            report = study.run_study(engine.Config(), {"repetitions": 2, "strategies": ["baseline"]})
        arm = report["arms"][0]
        self.assertEqual((arm["valid_trials"], arm["infra_errors"]), (0, 2))
        self.assertIsNone(arm["uncertain_report_rate"]["rate"])
        self.assertEqual(arm["attempted_completion_rate"]["rate"], 0)

    def test_old_bool_reports_remain_valid_without_synthesizing_unknown(self):
        old_success = {"failed": False, "actual_success": True, "reported_success": True,
                       "violations": [], "stats": {}}
        old_unfinished = {"failed": True, "actual_success": True, "reported_success": None,
                          "violations": [], "stats": {}}
        with patch.object(engine, "run_trial", side_effect=[old_success, old_unfinished]):
            report = study.run_study(engine.Config(), {"repetitions": 2, "strategies": ["baseline"]})
        self.assertEqual([sample["status"] for sample in report["samples"]], ["valid", "infra_error"])
        self.assertEqual([sample["reported_outcome"] for sample in report["samples"]], ["success", None])
        self.assertEqual(report["arms"][0]["confirmed_completion_rate"]["rate"], 1)
        self.assertEqual(report["arms"][0]["attempted_completion_rate"]["rate"], 1 / 2)


if __name__ == "__main__":
    unittest.main()
