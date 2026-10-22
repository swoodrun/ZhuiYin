"""Budget and lifecycle checks using synthetic protocol replies, never a real model."""
import io
import json
import unittest
from unittest.mock import patch

import engine
import study


MODEL = {"base_url": "https://synthetic.invalid/v1", "model": "synthetic-budget-test", "api_key": "SYNTHETIC_ONLY"}


def config(**changes):
    return engine.Config.from_dict({"agent_mode": "llm", "model_config": MODEL, **changes})


class SyntheticModel:
    """Execute the real model_request accounting and agent/tool loop, offline."""
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.requests.append(payload)
        messages = payload["messages"]
        verify = any(tool["function"]["name"] == "verify_receipt" for tool in payload["tools"])
        task = json.loads(messages[1]["content"])
        write = "create_ticket" if "ticket_title" in task else "reserve_device"
        query = "find_ticket" if write == "create_ticket" else "query_reservations"
        history = [message for message in messages if message["role"] == "tool"]
        success = True
        if not history:
            name = query
        else:
            name_before = messages[-2]["tool_calls"][0]["function"]["name"]
            observed = json.loads(messages[-1]["content"])
            rows = observed.get("reservations", observed.get("work_orders", []))
            if name_before == write:
                if observed.get("code") == "COMMIT_UNKNOWN":
                    name = "verify_receipt" if verify else query
                else:
                    name = "verify_receipt" if verify else "finish_task"
                    success = observed.get("ok", False)
            elif name_before == "verify_receipt":
                success = len(rows) == 1 and all(row.get(key) == value for row in rows
                           for key, value in task.items() if key != "request_id")
                name = write if not rows and len(history) < 4 else "finish_task"
            else:
                name = "finish_task" if rows else write
        args = {"success": bool(success), "reason": "synthetic protocol test"} if name == "finish_task" else {}
        answer = {"role": "assistant", "tool_calls": [{"id": f"offline-{len(history)}", "type": "function",
                  "function": {"name": name, "arguments": json.dumps(args)}}]}
        return io.BytesIO(json.dumps({"choices": [{"message": answer}],
                                     "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}).encode())


class ExperimentBudgetTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(vars(engine.MODEL_CONTEXT))
        vars(engine.MODEL_CONTEXT).clear()

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.saved)

    def execute(self, cfg, **kwargs):
        transport = SyntheticModel()
        with patch("engine.urllib.request.build_opener", return_value=transport):
            result = engine.run_experiment(cfg, **kwargs)
        return result, transport

    def test_config_budget_bounds_default_and_public_roundtrip(self):
        self.assertEqual(config().max_requests, 400)
        for number in (1, 200, 400, "37"):
            cfg = config(max_requests=number)
            self.assertEqual(cfg.public()["max_requests"], int(number))
            self.assertEqual(engine.Config.from_dict({**cfg.public(), "model_config": MODEL}).max_requests, int(number))
        for value in (0, -1, 401, True, False, 1.5, "1.5", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config(max_requests=value)

    def test_real_request_path_can_complete_more_than_forty_offline_requests(self):
        report, transport = self.execute(config())
        self.assertGreater(len(transport.requests), 40)
        self.assertEqual(report["model_usage"]["requests"], len(transport.requests))
        self.assertLessEqual(len(transport.requests), 400)
        self.assertEqual(report["status"], "finished")
        self.assertTrue(report["minimization"]["completed"])
        self.assertFalse(report["minimization"]["one_minimal"])
        self.assertEqual(set(report["minimized_faults"]), {"commit_response_lost", "stale_read"})
        self.assertFalse(report["fixed"]["failed"])
        self.assertEqual(report["model_usage"]["records"][0]["total_tokens"], 18)
        self.assertEqual(sum(report["model_usage"]["requests_by_phase"].values()), len(transport.requests))

    def test_low_budget_preserves_baseline_without_running_partial_candidate(self):
        report, transport = self.execute(config(max_requests=20))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stopped_reason"], "request_budget_exhausted")
        self.assertFalse(report["minimization"]["completed"])
        self.assertFalse(report["minimization"]["one_minimal"])
        self.assertEqual(report["minimization"]["trials"], 0)
        self.assertEqual(report["minimized_faults"], list(engine.DEFAULT_FAULTS))
        self.assertTrue(report["fixed"]["completed"])
        self.assertFalse(report["fixed"]["failed"])
        self.assertLess(len(transport.requests), 20)

    def test_fixed_gets_full_trial_reserve_after_partial_minimization(self):
        reserve = []
        def progress(phase, *_):
            if phase == "fixed":
                reserve.append(engine.MODEL_CONTEXT.remaining)
        report, transport = self.execute(config(max_requests=50), on_progress=progress)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["minimization"]["completed"])
        self.assertTrue(report["minimization"]["steps"])
        self.assertTrue(all(step["completed"] for step in report["minimization"]["steps"]))
        self.assertGreaterEqual(reserve[0], engine.MAX_TRIAL_REQUESTS)
        self.assertTrue(report["fixed"]["completed"])
        self.assertFalse(report["fixed"]["failed"])
        self.assertLessEqual(len(transport.requests), 50)

    def test_insufficient_fixed_budget_is_unrun_not_a_business_failure(self):
        report, transport = self.execute(config(max_requests=7))
        fixed = report["fixed"]
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(fixed["status"], "not_run")
        self.assertIsNone(fixed["failed"])
        self.assertIsNone(fixed["actual_success"])
        self.assertIsNone(fixed["reported_success"])
        self.assertTrue(fixed["skipped"])
        self.assertNotIn("sqlite_dump", fixed)
        self.assertEqual(fixed["violations"], [])
        self.assertEqual(report["stopped_reason"], "request_budget_exhausted")

    def test_unfinished_baseline_raises_without_more_requests_or_context_leak(self):
        transport = SyntheticModel()
        with patch("engine.urllib.request.build_opener", return_value=transport), self.assertRaisesRegex(RuntimeError, "主试验未完成"):
            engine.run_experiment(config(max_requests=1))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(vars(engine.MODEL_CONTEXT), {})

    def test_success_restores_all_enclosing_thread_local_fields_by_identity(self):
        records = [{"enclosing_study": True}]
        cancel = lambda: False
        previous = {"remaining": 17, "request_limit": 19, "cancel_check": cancel,
                    "usage_records": records, "budget_exhausted": True}
        vars(engine.MODEL_CONTEXT).update(previous)
        self.execute(config(max_requests=20))
        self.assertEqual(vars(engine.MODEL_CONTEXT), previous)
        self.assertIs(engine.MODEL_CONTEXT.usage_records, records)
        self.assertIs(engine.MODEL_CONTEXT.cancel_check, cancel)

    def test_callback_error_restores_context(self):
        engine.MODEL_CONTEXT.remaining = 12
        with self.assertRaisesRegex(OSError, "synthetic callback"):
            engine.run_experiment(config(), on_progress=lambda *_: (_ for _ in ()).throw(OSError("synthetic callback")))
        self.assertEqual(vars(engine.MODEL_CONTEXT), {"remaining": 12})

    def test_cancel_before_baseline_never_starts_model_trial(self):
        with patch("engine.run_trial") as trial, self.assertRaisesRegex(RuntimeError, "停止"):
            engine.run_experiment(config(), cancel_check=lambda: True)
        trial.assert_not_called()
        self.assertEqual(vars(engine.MODEL_CONTEXT), {})

    def test_cancel_after_baseline_does_not_start_minimization_fixed_or_rules(self):
        stopped = [False]
        def progress(phase, *_):
            if phase == "minimizing":
                stopped[0] = True
        report, transport = self.execute(config(), on_progress=progress, cancel_check=lambda: stopped[0])
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(report["stopped_reason"], "cancelled")
        self.assertEqual(report["minimization"]["stopped_reason"], "cancelled")
        self.assertEqual(report["fixed"]["status"], "not_run")
        self.assertEqual(report["regression"]["cases"], [])
        self.assertEqual(report["ablation"]["arms"], [])
        self.assertEqual(report["expanded_regression"]["cases"], [])

    def test_cancel_during_rule_regression_marks_its_incomplete_scope(self):
        stopped = [False]
        def progress(phase, *_):
            if phase == "regression":
                stopped[0] = True
        report = engine.run_experiment(engine.Config(), on_progress=progress,
                                       cancel_check=lambda: stopped[0])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stopped_reason"], "cancelled")
        self.assertTrue(report["baseline"]["completed"])
        self.assertTrue(report["fixed"]["completed"])
        self.assertEqual(report["regression"]["total"], 1)
        self.assertEqual(report["regression"]["planned"], 16)
        self.assertFalse(report["regression"]["completed"])
        self.assertFalse(report["ablation"]["completed"])
        self.assertFalse(report["expanded_regression"]["completed"])

    def test_model_error_in_candidate_is_not_accepted_as_a_counterexample(self):
        cfg = config()
        engine.MODEL_CONTEXT.remaining = 400
        engine.MODEL_CONTEXT.request_limit = 400
        engine.MODEL_CONTEXT.budget_exhausted = False
        good = {"failed": True, "failure_fingerprint": "ORIGINAL_BUSINESS_ERROR"}
        bad = {"failed": True, "failure_fingerprint": "AGENT_DID_NOT_FINISH|MODEL_EXECUTION_ERROR"}
        with patch.object(engine, "run_trial", side_effect=[good, good, bad]) as trial:
            reduced = engine.minimize(cfg, cfg.faults, expected_fingerprint="ORIGINAL_BUSINESS_ERROR",
                                      reserve_requests=9)
        self.assertEqual(trial.call_count, 3)
        self.assertEqual(reduced["stopped_reason"], "model_execution_error")
        self.assertFalse(reduced["completed"])
        self.assertFalse(reduced["one_minimal"])
        self.assertEqual(reduced["faults"], cfg.faults)
        self.assertFalse(reduced["steps"][0]["accepted"])
        self.assertFalse(reduced["steps"][0]["completed"])
        self.assertEqual(reduced["steps"][0]["reproduced"], 0)

    def test_cancel_during_candidate_keeps_last_complete_deletion(self):
        stopped = [False]
        def progress(phase, *_):
            if phase == "minimizing" and engine.MODEL_CONTEXT.remaining < 380:
                stopped[0] = True
        report, _ = self.execute(config(), on_progress=progress, cancel_check=lambda: stopped[0])
        mini = report["minimization"]
        self.assertEqual(report["stopped_reason"], "cancelled")
        self.assertFalse(mini["completed"])
        self.assertFalse(mini["one_minimal"])
        self.assertTrue(mini["steps"])
        self.assertEqual(mini["faults"], mini["steps"][-1]["remaining"])

    def test_dynamic_regression_count_and_progress_match_fault_scope(self):
        phases = []
        with patch.object(engine, "DEFAULT_FAULTS", ("commit_response_lost", "stale_read")):
            report = engine.run_experiment(engine.Config.from_dict({}), on_progress=lambda *args: phases.append(args))
        self.assertEqual(report["regression"]["total"], 4)
        self.assertEqual(report["regression"]["planned"], 4)
        regression = [phase for phase in phases if phase[0] == "regression"]
        self.assertTrue(regression[-1][2].endswith("4/4"))
        self.assertTrue(any("4 组全组合" in limitation for limitation in report["limitations"]))

    def test_study_budget_is_independent_of_config_single_experiment_budget(self):
        cfg = config(max_requests=1, faults=[])
        transport = SyntheticModel()
        with patch("engine.urllib.request.build_opener", return_value=transport):
            report = study.run_study(cfg, {"repetitions": 2, "strategies": ["baseline"], "max_requests": 20})
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["model_usage"]["requests"], 6)
        self.assertEqual(report["model_usage"]["limit"], 20)
        self.assertEqual(vars(engine.MODEL_CONTEXT), {})


if __name__ == "__main__":
    unittest.main()
