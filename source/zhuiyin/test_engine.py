"""Behavior checks for the local fault-minimization prototype.

Run with: python -m unittest -v test_engine.py
These checks exercise the executable rule agent and real SQLite side effects.
They do not establish reliability of an external language model.
"""

import itertools
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from email.message import Message
from urllib.request import HTTPHandler
from urllib.response import addinfourl
from unittest.mock import patch

import engine


FAULTS = [
    "commit_response_lost",
    "stale_read",
    "truncate_response",
    "latency",
]
TRIGGER = {"commit_response_lost", "stale_read"}


def make_config(**changes):
    values = {
        "equipment": "单反相机",
        "stock": 3,
        "quantity": 1,
        "request_id": "test-request-001",
        "seed": 17,
        "faults": FAULTS,
    }
    values.update(changes)
    return engine.Config.from_dict(values)


class TrialTests(unittest.TestCase):
    def trial(self, faults, strategy="baseline", **changes):
        result = engine.run_trial(make_config(**changes), faults, strategy=strategy)
        self.assertIsInstance(result, dict)
        for key in (
            "failed", "failure_fingerprint", "violations", "reservations",
            "stock_initial", "stock_remaining", "reported_success", "stats", "events",
        ):
            self.assertIn(key, result, key)
        self.assertGreaterEqual(result["stock_remaining"], 0)
        self.assertLessEqual(result["stock_remaining"], result["stock_initial"])
        return result

    def assert_healthy(self, result):
        self.assertFalse(result["failed"], result.get("violations"))
        self.assertFalse(result["violations"])

    def test_baseline_without_faults_completes_once(self):
        result = self.trial([])
        self.assert_healthy(result)
        self.assertTrue(result["reported_success"])
        self.assertEqual(result["stock_initial"], 3)
        self.assertEqual(result["stock_remaining"], 2)

    def test_commit_and_stale_read_create_verified_business_failure(self):
        result = self.trial(sorted(TRIGGER))
        self.assertTrue(result["failed"])
        self.assertTrue(result["violations"])
        self.assertTrue(result["failure_fingerprint"])
        self.assertEqual(result["stock_remaining"], 1)

    def test_single_faults_do_not_duplicate_booking(self):
        for fault in FAULTS:
            with self.subTest(fault=fault):
                result = self.trial([fault])
                self.assert_healthy(result)
                self.assertEqual(result["stock_remaining"], 2)

    def test_irrelevant_faults_preserve_failure_type(self):
        minimal = self.trial(sorted(TRIGGER))
        full = self.trial(FAULTS)
        self.assertTrue(full["failed"])
        self.assertEqual(full["failure_fingerprint"], minimal["failure_fingerprint"])

    def test_verified_strategy_survives_all_faults(self):
        result = self.trial(FAULTS, strategy="verified")
        self.assert_healthy(result)
        self.assertTrue(result["reported_success"])
        self.assertEqual(result["stock_remaining"], 2)

    def test_stock_one_never_overdraws_or_invents_success(self):
        for strategy in ("baseline", "verified"):
            with self.subTest(strategy=strategy):
                result = self.trial(FAULTS, strategy=strategy, stock=1)
                self.assertEqual(result["stock_remaining"], 0)
                if result["reported_success"]:
                    self.assertEqual(len(result["reservations"]), 1)
                    self.assertTrue(result["actual_success"])
                if result["reported_success"] != result["actual_success"]:
                    self.assertTrue(result["failed"])
                    self.assertIn("REPORT_MISMATCH", result["failure_fingerprint"])

    def test_insufficient_stock_is_a_business_rejection(self):
        for strategy in ("baseline", "verified"):
            with self.subTest(strategy=strategy):
                result = self.trial(FAULTS, strategy=strategy, stock=1, quantity=2)
                self.assert_healthy(result)
                self.assertFalse(result["reported_success"])
                self.assertEqual(result["stock_remaining"], 1)
                self.assertFalse(result["reservations"])

    def test_custom_quantity_is_applied_to_every_real_write(self):
        baseline = self.trial(FAULTS, stock=6, quantity=2)
        self.assertTrue(baseline["failed"])
        self.assertEqual(baseline["stock_remaining"], 2)
        self.assertEqual([r["quantity"] for r in baseline["reservations"]], [2, 2])
        fixed = self.trial(FAULTS, strategy="verified", stock=6, quantity=2)
        self.assert_healthy(fixed)
        self.assertEqual(fixed["stock_remaining"], 4)
        self.assertEqual([r["quantity"] for r in fixed["reservations"]], [2])

    def test_full_sixteen_combination_regression(self):
        visited = 0
        for count in range(len(FAULTS) + 1):
            for combination in itertools.combinations(FAULTS, count):
                visited += 1
                with self.subTest(faults=combination):
                    baseline = self.trial(list(combination))
                    self.assertEqual(baseline["failed"], TRIGGER.issubset(combination))
                    fixed = self.trial(list(combination), strategy="verified")
                    self.assert_healthy(fixed)
                    self.assertTrue(fixed["reported_success"])
                    self.assertEqual(fixed["stock_remaining"], 2)
        self.assertEqual(visited, 16)


class MinimizationTests(unittest.TestCase):
    def test_minimization_preserves_failure_and_is_one_minimal(self):
        config = make_config()
        progress = []
        result = engine.minimize(config, FAULTS, on_step=progress.append, repeats=2)
        self.assertEqual(set(result["faults"]), TRIGGER)
        self.assertEqual(result["original_count"], 4)
        self.assertEqual(result["final_count"], 2)
        self.assertTrue(result["one_minimal"])
        self.assertTrue(result["stable"])
        self.assertGreater(result["trials"], 0)
        self.assertTrue(result["steps"])
        self.assertTrue(progress)
        original = engine.run_trial(config, FAULTS)
        minimal = engine.run_trial(config, result["faults"])
        self.assertTrue(minimal["failed"])
        self.assertEqual(minimal["failure_fingerprint"], original["failure_fingerprint"])
        for fault in result["faults"]:
            smaller = [item for item in result["faults"] if item != fault]
            self.assertFalse(engine.run_trial(config, smaller)["failed"])

    def test_same_seed_repeats_same_business_outcome(self):
        config = make_config()
        results = [engine.run_trial(config, FAULTS) for _ in range(3)]
        for result in results[1:]:
            self.assertEqual(result["failure_fingerprint"], results[0]["failure_fingerprint"])
            self.assertEqual(result["stock_remaining"], results[0]["stock_remaining"])
            self.assertEqual(result["reported_success"], results[0]["reported_success"])

    def test_seed_repeats_the_minimization_search_order(self):
        config = make_config()
        first = engine.minimize(config, FAULTS)
        second = engine.minimize(config, FAULTS)
        self.assertEqual(first["steps"], second["steps"])

    def test_healthy_run_is_not_presented_as_a_minimized_failure(self):
        result = engine.minimize(make_config(), [], repeats=2)
        self.assertFalse(result["stable"])
        self.assertFalse(result["one_minimal"])
        self.assertEqual(result["faults"], [])

    def test_complete_report_contains_sixteen_distinct_regression_cases(self):
        phases = []
        result = engine.run_experiment(make_config(), on_progress=lambda *args: phases.append(args))
        self.assertTrue(result["baseline"]["failed"])
        self.assertFalse(result["fixed"]["failed"])
        self.assertEqual(set(result["minimized_faults"]), TRIGGER)
        regression = result["regression"]
        self.assertEqual(regression["agent_mode"], "rules")
        self.assertEqual(regression["total"], 16)
        self.assertEqual(len(regression["cases"]), 16)
        self.assertEqual(len({frozenset(c["faults"]) for c in regression["cases"]}), 16)
        self.assertEqual(regression["baseline_failures"], 4)
        self.assertEqual(regression["fixed_failures"], 0)
        self.assertEqual(phases[-1][0], "done")
        self.assertEqual(phases[-1][1], 100)


class ConfigTests(unittest.TestCase):
    def test_invalid_user_parameters_are_rejected(self):
        cases = [
            {"equipment": ""},
            {"equipment": "   "},
            {"request_id": ""},
            {"stock": ""},
            {"stock": "not-a-number"},
            {"stock": -1},
            {"stock": 1.5},
            {"stock": True},
            {"quantity": ""},
            {"quantity": 0},
            {"quantity": -1},
            {"quantity": "1.5"},
            {"quantity": 1.5},
            {"faults": ["unknown_fault"]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    make_config(**changes)


class ModelBoundaryTests(unittest.TestCase):
    """Test protocol safeguards with synthetic replies; no external model is used."""

    def setUp(self):
        self.saved_context = dict(vars(engine.MODEL_CONTEXT))
        vars(engine.MODEL_CONTEXT).clear()
        self.model = {"base_url": "http://127.0.0.1:12345/v1", "model": "test", "api_key": "SYNTHETIC_ONLY_SECRET"}

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.saved_context)

    def test_cancellation_prevents_another_network_request(self):
        engine.MODEL_CONTEXT.cancel_check = lambda: True
        with patch("engine.urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(RuntimeError, "停止"):
                engine.model_request(self.model, [])
            opener.assert_not_called()

    def test_request_budget_prevents_a_forty_first_request(self):
        engine.MODEL_CONTEXT.remaining = 1
        content = json.dumps({"choices": [{"message": {"role": "assistant", "content": "OK"}}]}).encode()
        with patch("engine.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(content)
            engine.model_request(self.model, [])
            with self.assertRaisesRegex(RuntimeError, "40"):
                engine.model_request(self.model, [])
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_invalid_key_errors_do_not_echo_key(self):
        config = dict(self.model, api_key="SYNTHETIC_ONLY_SECRET\nINVALID")
        with patch("engine.urllib.request.build_opener") as opener:
            with self.assertRaises(ValueError) as raised:
                engine.model_request(config, [])
            self.assertNotIn("SYNTHETIC_ONLY_SECRET", str(raised.exception))
            opener.assert_not_called()

    def test_redirect_cannot_forward_authorization_to_second_server(self):
        received = []
        visited = []

        class SyntheticTransport(HTTPHandler):
            """Supply actual urllib HTTP responses without an external socket."""

            def http_open(self, request):
                visited.append(request.full_url)
                headers = Message()
                if "12345" in request.full_url:
                    headers["Location"] = "http://127.0.0.1:54321/capture"
                    response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                    response.msg = "Found"
                    return response
                received.append(request.get_header("Authorization"))
                payload = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
                response = addinfourl(io.BytesIO(payload), headers, request.full_url, 200)
                response.msg = "OK"
                return response

        real_build_opener = engine.urllib.request.build_opener

        def build_opener(*handlers):
            return real_build_opener(*handlers, SyntheticTransport(), engine.urllib.request.ProxyHandler({}))

        with patch("engine.urllib.request.build_opener", side_effect=build_opener):
            with self.assertRaisesRegex(RuntimeError, "302") as raised:
                engine.model_request(self.model, [])
        self.assertNotIn(self.model["api_key"], str(raised.exception))
        self.assertEqual(visited, [self.model["base_url"] + "/chat/completions"])
        self.assertEqual(received, [])

    def test_fingerprint_drift_does_not_start_minimization(self):
        config = make_config(agent_mode="llm", model_config=self.model)
        fake = {"failed": True, "failure_fingerprint": "DIFFERENT_BUSINESS_ERROR"}
        with patch("engine.run_trial", return_value=fake) as trial:
            result = engine.minimize(config, FAULTS, expected_fingerprint="ORIGINAL_BUSINESS_ERROR")
        self.assertFalse(result["stable"])
        self.assertFalse(result["one_minimal"])
        self.assertEqual(result["steps"], [])
        self.assertEqual(trial.call_count, 2)

    def test_model_infrastructure_errors_are_not_minimized_as_business_failures(self):
        config = make_config(agent_mode="llm", model_config=self.model)
        fake = {"failed": True, "failure_fingerprint": "AGENT_DID_NOT_FINISH|MODEL_EXECUTION_ERROR"}
        with patch("engine.run_trial", return_value=fake):
            result = engine.minimize(config, FAULTS)
        self.assertFalse(result["one_minimal"])
        self.assertEqual(result["steps"], [])

    def test_model_without_tool_execution_is_not_counted_as_success(self):
        config = make_config(agent_mode="llm", model_config=self.model)
        with patch("engine.model_request", return_value={"role": "assistant", "content": "已完成"}):
            result = engine.run_trial(config, [], agent_mode="llm")
        self.assertTrue(result["failed"])
        self.assertIn("AGENT_DID_NOT_FINISH", result["failure_fingerprint"])
        self.assertIn("MODEL_EXECUTION_ERROR", result["failure_fingerprint"])
        self.assertEqual(result["reservations"], [])
        self.assertFalse(result["reported_success"])

    def test_task_prompt_does_not_disclose_injected_faults_or_secret(self):
        prompts = []

        def reply(model_config, messages, tools):
            prompts.extend(messages[:2])
            return {"tool_calls": [{"id": "end", "type": "function", "function": {
                "name": "finish_task", "arguments": '{"success":false,"reason":"test"}'}}]}

        config = make_config(agent_mode="llm", model_config=self.model)
        with patch("engine.model_request", side_effect=reply):
            engine.run_trial(config, FAULTS, agent_mode="llm")
        user_task = json.loads(prompts[1]["content"])
        self.assertEqual(set(user_task), {"equipment", "quantity", "request_id"})
        self.assertNotIn(self.model["api_key"], json.dumps(prompts))


class ArtifactTests(unittest.TestCase):
    def test_worker_sqlite_exports_match_business_report_and_exclude_credentials(self):
        import server

        config = make_config(model_config={"api_key": "SYNTHETIC_ONLY_SECRET"})
        run_id = "test-artifact-roundtrip"
        jobs = {run_id: {"status": "running", "events": []}}
        stops = {run_id: threading.Event()}
        workspace = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="test-artifact-", dir=workspace) as folder:
            data = Path(folder)
            self.assertEqual(data.resolve().parent, workspace)
            with patch.object(server, "DATA", data), patch.object(server, "JOBS", jobs), patch.object(server, "STOP_EVENTS", stops):
                server.worker(run_id, config)
            self.assertEqual(jobs[run_id]["status"], "done", jobs[run_id].get("error"))
            report_text = (data / run_id / "report.json").read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC_ONLY_SECRET", report_text)
            self.assertNotIn("model_config", report_text)
            report = json.loads(report_text)
            for name, expected_count in (("baseline", 2), ("fixed", 1)):
                with closing(sqlite3.connect(data / run_id / f"{name}.sqlite")) as db:
                    actual_count = db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
                    stock = db.execute("SELECT available FROM inventory").fetchone()[0]
                    self.assertEqual(actual_count, expected_count)
                    self.assertEqual(stock, report[name]["stock_remaining"])
            self.assertEqual(config.model_config, {})


if __name__ == "__main__":
    unittest.main()
