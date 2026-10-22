"""Checks for statistical accounting, fresh execution, and bounded model use."""
import copy
import json
import unittest
from unittest.mock import patch

import engine
import study


def config(mode="rules", **changes):
    return engine.Config(agent_mode=mode, model_config={"base_url": "http://localhost:9999/v1",
                         "model": "synthetic-test-only", "api_key": "not-a-real-secret"}, **changes)


def result(failed=False, complete=True, fingerprint="", codes=None, finished=True):
    return {"failed": failed, "actual_success": complete, "failure_fingerprint": fingerprint,
            "reported_success": complete if finished else None,
            "violations": [{"code": code, "message": code} for code in codes or []],
            "stats": {"tool_calls": 2, "physical_writes": 1}}


def request(known=True, status="ok", prompt=100, completion=50):
    context = engine.MODEL_CONTEXT
    if context.remaining <= 0:
        return False
    if context.cancel_check and context.cancel_check():
        return False
    context.remaining -= 1
    context.usage_records.append({"status": status,
        "prompt_tokens": prompt if known else None,
        "completion_tokens": completion if known else None,
        "total_tokens": prompt + completion if known else None,
        "usage_available": known, "elapsed_ms": 4, "http_status": 200 if status == "ok" else None})
    return True


class OptionsTests(unittest.TestCase):
    def test_defaults_and_deduplication(self):
        options = study.validate_study_options({})
        self.assertEqual(options["repetitions"], 5)
        self.assertEqual(options["max_requests"], 80)
        self.assertEqual(options["strategies"], list(study.STRATEGIES))
        self.assertEqual(options["order_mode"], "balanced")
        self.assertIsNone(options["input_price_per_million"])
        self.assertEqual(study.validate_study_options({"strategies": ["baseline", "baseline"]})["strategies"], ["baseline"])

    def test_boundaries_and_hostile_options_are_rejected(self):
        for options in (None, [], {"repetitions": 1}, {"repetitions": 21}, {"repetitions": True},
                        {"max_requests": 0}, {"max_requests": 401}, {"strategies": []},
                        {"strategies": ["arbitrary"]}, {"api_key": "secret"},
                        {"input_price_per_million": float("nan")},
                        {"input_price_per_million": "inf"}, {"input_price_per_million": -1},
                        {"output_price_per_million": True}, {"currency": "x\n"}, {"order_mode": "random"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                study.validate_study_options(options)

    def test_wilson_uses_valid_denominator_and_handles_empty(self):
        self.assertIsNone(study.wilson_interval(0, 0))
        interval = study.wilson_interval(0, 10)
        self.assertAlmostEqual(interval[0], 0, places=12)
        self.assertAlmostEqual(interval[1], 0.2775327998628892)
        interval = study.wilson_interval(10, 10)
        self.assertAlmostEqual(interval[0], 0.7224672001371107)
        self.assertAlmostEqual(interval[1], 1)


class StudyTests(unittest.TestCase):
    def test_real_rules_trials_are_labeled_and_reexecuted(self):
        checkpoints = []
        report = study.run_study(config(), {"repetitions": 2, "strategies": ["baseline", "verified"]},
                                 on_checkpoint=checkpoints.append)
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["mode"], "rules")
        self.assertEqual(report["model_usage"]["requests"], 0)
        self.assertFalse(report["model_usage"]["external_model_verified"])
        self.assertEqual(report["arms"][0]["business_failures"], 2)
        self.assertEqual(report["arms"][1]["business_failures"], 0)
        self.assertEqual(len(checkpoints), 5)
        self.assertEqual(checkpoints[-1]["status"], "finished")
        self.assertIn("本地模拟", report["limitations"][0])
        self.assertNotIn("not-a-real-secret", json.dumps(report))

    def test_four_groups_interleave_and_keep_identical_injections(self):
        calls = []
        cfg = config()
        original = copy.deepcopy(cfg.public())
        def fake(cfg, faults, strategy, **kwargs):
            calls.append((strategy, list(faults), cfg.seed, kwargs["agent_mode"]))
            return result()
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(cfg, {"repetitions": 4})
        expected = [strategy for row in report["execution_plan"] for strategy in row["strategies"]]
        self.assertEqual([call[0] for call in calls], expected)
        self.assertEqual([row["strategy"] for row in report["execution_order"]], expected)
        self.assertEqual(len(report["samples"]), 16)
        for position in range(4):
            self.assertEqual(set(row["strategies"][position] for row in report["execution_plan"]), set(study.STRATEGIES))
        self.assertEqual(report["execution_plan"], study.execution_plan(report["options"], cfg.seed))
        self.assertTrue(all(sample["execution_index"] == index + 1 for index, sample in enumerate(report["samples"])))
        self.assertTrue(all(call[1] == cfg.faults and call[2] == cfg.seed for call in calls))
        self.assertEqual(cfg.public(), original)

    def test_real_four_arm_evidence_is_auditable_for_both_business_scenarios(self):
        for scenario in ("equipment", "ticket"):
            with self.subTest(scenario=scenario):
                report = study.run_study(config(scenario=scenario), {"repetitions": 2})
                self.assertEqual(len(report["samples"]), 8)
                baseline = report["samples"][0]
                self.assertEqual(len(baseline["business_records"]), 2)
                self.assertTrue(baseline["events"])
                self.assertTrue(any(event.get("actual") for event in baseline["events"]))
                self.assertTrue(baseline["reported_success"])
                self.assertNotIn("sqlite_dump", baseline)
                for sample in report["samples"]:
                    if sample["strategy"] != "baseline":
                        self.assertEqual(len(sample["business_records"]), 1)
                        self.assertFalse(sample["failed"])

    def test_infrastructure_errors_are_excluded_and_fingerprint_anchor_is_explicit(self):
        outcomes = [result(True, False, "DUPLICATE", ["DUPLICATE"]),
                    result(True, False, "MODEL_EXECUTION_ERROR", ["MODEL_EXECUTION_ERROR"], finished=False),
                    result(True, True, "REPORT_MISMATCH", ["REPORT_MISMATCH"]), result()]
        with patch.object(engine, "run_trial", side_effect=outcomes):
            report = study.run_study(config(), {"repetitions": 4, "strategies": ["baseline"]})
        arm = report["arms"][0]
        self.assertEqual((arm["attempted"], arm["valid_trials"], arm["infra_errors"]), (4, 3, 1))
        self.assertEqual(arm["business_failure_rate"]["rate"], 2 / 3)
        self.assertEqual(arm["task_completion_rate"]["rate"], 2 / 3)
        self.assertEqual(arm["attempted_completion_rate"]["numerator"], 2)
        self.assertEqual(arm["attempted_completion_rate"]["denominator"], 4)
        self.assertEqual(arm["attempted_completion_rate"]["rate"], 1 / 2)
        self.assertEqual(arm["reproduction"]["anchor_fingerprint"], "DUPLICATE")
        self.assertEqual(arm["reproduction"]["matched"], 0)
        self.assertEqual(arm["reproduction"]["denominator"], 2)
        self.assertEqual(arm["reproduction"]["anchor_trial_index"], 1)
        self.assertEqual(arm["reproduction"]["ci95"], study.wilson_interval(0, 2))

    def test_successful_pilot_is_not_replaced_by_later_failure(self):
        outcomes = [result(), result(True, False, "DUPLICATE", ["DUPLICATE"])]
        with patch.object(engine, "run_trial", side_effect=outcomes):
            report = study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]})
        arm = report["arms"][0]
        self.assertEqual(arm["business_failure_rate"]["rate"], 0.5)
        self.assertFalse(arm["reproduction"]["pilot_failed"])
        self.assertEqual(arm["reproduction"]["pilot_trial_index"], 1)
        self.assertIsNone(arm["reproduction"]["anchor_fingerprint"])
        self.assertIsNone(arm["reproduction"]["rate"])
        self.assertEqual(arm["attempted_completion_rate"]["rate"], 0.5)
        self.assertIsNone(arm["reproduction"]["ci95"])

    def test_pilot_skips_infrastructure_error_and_needs_followup_for_reproduction(self):
        outcomes = [result(True, False, "MODEL_EXECUTION_ERROR", ["MODEL_EXECUTION_ERROR"], finished=False),
                    result(True, False, "DUPLICATE", ["DUPLICATE"])]
        with patch.object(engine, "run_trial", side_effect=outcomes):
            report = study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]})
        reproduction = report["arms"][0]["reproduction"]
        self.assertEqual(reproduction["pilot_trial_index"], 2)
        self.assertTrue(reproduction["pilot_failed"])
        self.assertEqual(reproduction["denominator"], 0)
        self.assertIsNone(reproduction["rate"])
        self.assertIsNone(reproduction["ci95"])

    def test_all_infrastructure_errors_produce_no_rates_or_intervals(self):
        with patch.object(engine, "run_trial", return_value=result(True, False, "AGENT_DID_NOT_FINISH",
                          ["AGENT_DID_NOT_FINISH"], finished=False)):
            report = study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]})
        arm = report["arms"][0]
        self.assertEqual(arm["valid_trials"], 0)
        self.assertEqual(arm["infra_errors"], 2)
        self.assertIsNone(arm["business_failure_rate"]["rate"])
        self.assertIsNone(arm["business_failure_rate"]["ci95"])
        self.assertIsNone(arm["reproduction"]["rate"])
        self.assertEqual(arm["attempted_completion_rate"]["rate"], 0)
        self.assertEqual(arm["attempted_completion_rate"]["ci95"], study.wilson_interval(0, 2))

    def test_one_request_budget_is_shared_across_trials_and_partial_is_kept(self):
        def fake(*args, **kwargs):
            for _ in range(2):
                if not request():
                    return result(True, False, "MODEL_EXECUTION_ERROR", ["MODEL_EXECUTION_ERROR"], finished=False)
            return result()
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(config("llm"), {"repetitions": 2,
                "strategies": ["baseline", "verified"], "max_requests": 5})
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stopped_reason"], "request_budget_exhausted")
        self.assertEqual(report["model_usage"]["requests"], 5)
        self.assertEqual(report["completed"], 3)
        self.assertEqual([s["model_usage"]["requests"] for s in report["samples"]], [2, 2, 1])
        # Round two rotates verified to the front; truncation remains visible.
        self.assertEqual([row["strategy"] for row in report["execution_order"]], ["baseline", "verified", "verified"])
        self.assertEqual(report["arms"][1]["infra_errors"], 1)
        self.assertEqual(report["arms"][0]["unrun"], 1)
        self.assertEqual(report["arms"][1]["attempted_completion_rate"]["rate"], 1 / 2)

    def test_fixed_order_is_available_and_balanced_order_is_seeded(self):
        with patch.object(engine, "run_trial", return_value=result()):
            fixed = study.run_study(config(), {"repetitions": 3, "order_mode": "fixed"})
            repeated = study.run_study(config(), {"repetitions": 3})
            same = study.run_study(config(), {"repetitions": 3})
            changed = study.run_study(config(seed=0), {"repetitions": 3})
        self.assertEqual([row["strategy"] for row in fixed["execution_order"]], list(study.STRATEGIES) * 3)
        self.assertEqual(repeated["execution_plan"], same["execution_plan"])
        self.assertNotEqual(repeated["execution_plan"], changed["execution_plan"])

    def test_exact_budget_final_completion_is_finished(self):
        def fake(*args, **kwargs):
            request()
            return result()
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(config("llm"), {"repetitions": 2,
                "strategies": ["baseline"], "max_requests": 2})
        self.assertEqual(report["status"], "finished")
        self.assertIsNone(report["stopped_reason"])

    def test_known_tokens_have_user_priced_estimate_and_no_price_is_unknown(self):
        def fake(*args, **kwargs):
            request()
            return result()
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(config("llm"), {"repetitions": 2, "strategies": ["baseline"],
                    "input_price_per_million": 2, "output_price_per_million": 8, "currency": "USD"})
            unpriced = study.run_study(config("llm"), {"repetitions": 2, "strategies": ["baseline"]})
        usage = report["model_usage"]
        self.assertEqual((usage["requests"], usage["prompt_tokens"], usage["completion_tokens"]), (2, 200, 100))
        self.assertAlmostEqual(usage["estimated_cost"], 0.0012)
        self.assertEqual(usage["currency"], "USD")
        self.assertIsNone(unpriced["model_usage"]["estimated_cost"])

    def test_model_identity_keeps_only_name_and_origin_and_rules_have_no_identity(self):
        cfg = config("llm")
        cfg.model_config["base_url"] = "https://example.test:8443/private-path/v1?credential=hidden#private"
        with patch.object(engine, "run_trial", return_value=result()):
            report = study.run_study(cfg, {"repetitions": 2, "strategies": ["baseline"]})
            offline = study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]})
        self.assertEqual(report["model_identity"], {"model": "synthetic-test-only", "api_origin": "https://example.test:8443"})
        self.assertIsNone(offline["model_identity"])
        self.assertNotIn("private-path", json.dumps(report))
        self.assertNotIn("credential=hidden", json.dumps(report))

    def test_missing_usage_and_failed_network_are_not_zero_cost(self):
        count = [0]
        def fake(*args, **kwargs):
            count[0] += 1
            request(known=count[0] == 1, status="ok" if count[0] == 1 else "network_error")
            return result() if count[0] == 1 else result(True, False, "MODEL_EXECUTION_ERROR", ["MODEL_EXECUTION_ERROR"], finished=False)
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(config("llm"), {"repetitions": 2, "strategies": ["baseline"],
                     "input_price_per_million": 2, "output_price_per_million": 8})
        usage = report["model_usage"]
        self.assertEqual(usage["usage_unknown_requests"], 1)
        self.assertEqual(usage["failed_requests"], 1)
        self.assertEqual(usage["known_prompt_tokens"], 100)
        self.assertIsNone(usage["prompt_tokens"])
        self.assertIsNone(usage["estimated_cost"])

    def test_cancel_before_start_preserves_all_as_unrun(self):
        with patch.object(engine, "run_trial") as runner:
            report = study.run_study(config("llm"), {}, cancel_check=lambda: True)
        runner.assert_not_called()
        self.assertEqual(report["stopped_reason"], "cancelled")
        self.assertEqual(report["completed"], 0)
        self.assertTrue(all(arm["unrun"] == 5 for arm in report["arms"]))
        self.assertEqual(report["execution_order"], [])
        self.assertTrue(all(arm["attempted_completion_rate"]["rate"] is None for arm in report["arms"]))

    def test_cancel_during_trial_is_excluded_and_keeps_request_usage(self):
        stopped = [False]
        def fake(*args, **kwargs):
            request()
            stopped[0] = True
            return result(True, False, "MODEL_EXECUTION_ERROR", ["MODEL_EXECUTION_ERROR"], finished=False)
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(config("llm"), {"repetitions": 2, "strategies": ["baseline"]},
                                     cancel_check=lambda: stopped[0])
        self.assertEqual(report["samples"][0]["status"], "cancelled")
        self.assertEqual(report["model_usage"]["requests"], 1)
        self.assertEqual(report["arms"][0]["valid_trials"], 0)
        self.assertEqual(report["arms"][0]["cancelled"], 1)
        self.assertEqual(report["arms"][0]["unrun"], 1)
        self.assertEqual(report["arms"][0]["attempted_completion_rate"]["numerator"], 0)
        self.assertEqual(report["arms"][0]["attempted_completion_rate"]["denominator"], 1)

    def test_thread_local_context_is_restored_even_if_checkpoint_fails(self):
        missing = object()
        old = {key: getattr(engine.MODEL_CONTEXT, key, missing)
               for key in ("remaining", "usage_records", "cancel_check", "request_limit", "budget_exhausted")}
        previous_records = [{"existing": True}]
        previous_cancel = lambda: False
        engine.MODEL_CONTEXT.remaining = 17
        engine.MODEL_CONTEXT.usage_records = previous_records
        engine.MODEL_CONTEXT.cancel_check = previous_cancel
        engine.MODEL_CONTEXT.request_limit = 19
        engine.MODEL_CONTEXT.budget_exhausted = True
        try:
            def checkpoint(_):
                raise OSError("local write unavailable")
            with patch.object(engine, "run_trial", return_value=result()), self.assertRaises(OSError):
                study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]}, on_checkpoint=checkpoint)
            self.assertEqual(engine.MODEL_CONTEXT.remaining, 17)
            self.assertIs(engine.MODEL_CONTEXT.usage_records, previous_records)
            self.assertIs(engine.MODEL_CONTEXT.cancel_check, previous_cancel)
            self.assertEqual(engine.MODEL_CONTEXT.request_limit, 19)
            self.assertTrue(engine.MODEL_CONTEXT.budget_exhausted)
        finally:
            for key, value in old.items():
                if value is missing:
                    if hasattr(engine.MODEL_CONTEXT, key):
                        delattr(engine.MODEL_CONTEXT, key)
                else:
                    setattr(engine.MODEL_CONTEXT, key, value)

    def test_checkpoint_cannot_mutate_report_and_exception_text_is_private(self):
        def checkpoint(report):
            report["samples"].clear()
            report["options"]["strategies"].clear()
        with patch.object(engine, "run_trial", side_effect=RuntimeError("not-a-real-secret")):
            report = study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]},
                                     on_checkpoint=checkpoint)
        self.assertEqual(report["completed"], 2)
        self.assertEqual(len(report["samples"]), 2)
        self.assertEqual(report["options"]["strategies"], ["baseline"])
        self.assertNotIn("not-a-real-secret", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
