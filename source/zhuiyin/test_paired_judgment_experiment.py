"""Synthetic responses only: request lock, frozen judgments, budget and failures."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import paired_judgment_experiment as experiment
from tools import paired_judgment_launcher as launcher


def response(outcome="unknown", **changes):
    value = {"id": "synthetic-not-model-evidence", "model": "deepseek-flash",
             "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                         "tool_calls": [{"id": "synthetic-call", "type": "function", "function": {
                             "name": "finish_task", "arguments": json.dumps({"outcome": outcome, "reason": "synthetic"})}}]}}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
    value.update(changes)
    return value


class FrozenJudgmentClassificationTests(unittest.TestCase):
    def test_unknown_is_valid_report(self):
        self.assertEqual(experiment.classify(response())["outcome"], "unknown")

    def test_no_report_is_not_unknown(self):
        value = response()
        value["choices"][0]["message"].pop("tool_calls")
        self.assertEqual(experiment.classify(value), {"status": "no_report", "outcome": None})

    def test_invalid_outcome_rejected(self):
        self.assertEqual(experiment.classify(response("completed"))["status"], "invalid_report")

    def test_wrong_tool_never_executed_or_scored(self):
        value = response()
        value["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "create_ticket"
        self.assertEqual(experiment.classify(value), {"status": "non_judgment_tool_proposed", "outcome": None})

    def test_truncated_response_not_valid_even_with_parseable_report(self):
        value = response()
        value["choices"][0]["finish_reason"] = "length"
        self.assertEqual(experiment.classify(value)["status"], "generation_incomplete")

    def test_two_reports_rejected(self):
        value = response()
        value["choices"][0]["message"]["tool_calls"] *= 2
        self.assertEqual(experiment.classify(value)["status"], "invalid_report")

    def test_duplicate_outcome_rejected(self):
        value = response()
        value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"outcome":"success","outcome":"unknown","reason":"duplicate"}'
        self.assertEqual(experiment.classify(value)["status"], "invalid_report")

    def test_missing_reason_and_extra_fields_rejected(self):
        for arguments in ({"outcome": "unknown"}, {"outcome": "unknown", "reason": "ok", "success": True}):
            value = response()
            value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments)
            self.assertEqual(experiment.classify(value)["status"], "invalid_report")

    def test_empty_group_interval_not_zero_perfect_score(self):
        self.assertIsNone(experiment.wilson(0, 0))
        self.assertGreater(experiment.wilson(5, 5)["upper"], .99)
        self.assertLess(experiment.wilson(5, 5)["lower"], .6)


class FrozenJudgmentCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paired-judgment-test-", dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prepared, self.output = self.root / "prepared", self.root / "output"
        self.design = experiment.prepare(self.prepared)
        self.secret = "synthetic-credential-never-a-real-key"

    def test_ambiguous_actual_request_bytes_equal_and_no_hidden_metadata(self):
        paths = [self.prepared / "requests" / f"ambiguous--{w}.json" for w in ("committed", "not_committed")]
        self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
        request = json.loads(paths[0].read_bytes())
        self.assertEqual(request["tool_choice"]["function"]["name"], "finish_task")
        self.assertEqual(request["thinking"]["type"], "disabled")
        self.assertNotIn("actual_success", request)
        self.assertNotIn("world_id", request)
        self.assertEqual(request["messages"][-1]["content"], self.design["judgment_instruction"])

    def test_tampered_request_even_with_updated_hash_rejected_by_design(self):
        path = self.prepared / "requests/ambiguous--committed.json"
        value = json.loads(path.read_bytes())
        value["messages"].append({"role": "user", "content": "answer leak"})
        path.write_bytes(experiment.canonical(value))
        manifest_path = self.prepared / "preparation-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"]["requests/ambiguous--committed.json"] = experiment.digest(path)
        experiment.write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "请求"):
            experiment.validate_prepared(self.prepared)

    def test_all_samples_fixed_once_history_preserved_no_business_actions(self):
        calls = []
        def transport(body, key):
            self.assertEqual(key, self.secret)
            calls.append(body)
            return response()
        report = experiment.execute(self.prepared, self.output, self.secret, transport)
        self.assertEqual(len(calls), 30)
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["budget_after"]["requests_made"], 331)
        self.assertEqual(report["summary"]["valid"], 30)
        self.assertEqual(report["summary"]["business_actions_executed"], 0)
        self.assertEqual(sum(g["qualified_correct"] for g in report["summary"]["groups"]), 0)
        for file in self.output.rglob("*"):
            if file.is_file():
                self.assertNotIn(self.secret.encode(), file.read_bytes())
        initial = experiment.ledger_rows(self.prepared / "campaign/campaign-ledger-initial.sqlite")
        final = experiment.ledger_rows(self.output / "campaign-ledger-final.sqlite")
        self.assertEqual(final[:301], initial)
        with self.assertRaises(ValueError):
            experiment.execute(self.prepared, self.root / "second", self.secret, transport)

    def test_correct_failure_is_not_business_completion(self):
        def transport(body, _):
            payload = json.loads(body)
            last_tool = [m for m in payload["messages"] if m["role"] == "tool"][-1]
            observation = json.loads(last_tool["content"])
            outcome = ("success" if observation.get("work_orders") else "failure") if "snapshot" in observation else "unknown"
            return response(outcome)
        report = experiment.execute(self.prepared, self.output, self.secret, transport)
        not_committed = [g for g in report["summary"]["groups"] if g["world_id"] == "not_committed"]
        self.assertTrue(all(g["actual_success"] is False for g in not_committed))
        verified = next(g for g in not_committed if g["branch_id"] == "reliable_verification")
        self.assertEqual(verified["qualified_correct"], 5)
        self.assertEqual(verified["outcomes"]["failure"], 5)

    def test_connection_error_keeps_one_charge_and_not_run_remainder(self):
        def fail(*_):
            raise experiment.ProviderError("synthetic_connection_failure")
        report = experiment.execute(self.prepared, self.output, self.secret, fail)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["summary"]["attempted"], 1)
        self.assertEqual(report["summary"]["not_run"], 29)
        self.assertEqual(report["budget_after"]["requests_made"], 302)
        self.assertEqual(report["budget_after"]["unknown_usage_requests"], 1)
        self.assertEqual(report["samples"][0]["status"], "infra_error")

    def test_credential_echo_never_saved(self):
        def echo(*_):
            return response(private_echo=self.secret)
        report = experiment.execute(self.prepared, self.output, self.secret, echo)
        self.assertEqual(report["status"], "partial")
        self.assertFalse((self.output / "samples/01/response.json").exists())
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.secret.encode(), path.read_bytes())

    def test_launcher_starts_once_and_key_not_in_public_status(self):
        state = launcher.LaunchState(self.prepared, self.output)
        with patch.object(launcher.experiment, "execute", return_value={"status": "finished"}) as execute:
            self.assertTrue(state.start(self.secret))
            self.assertFalse(state.start(self.secret))
            for thread in __import__("threading").enumerate():
                if thread.name.startswith("Thread-") and thread is not __import__("threading").current_thread():
                    thread.join(timeout=1)
            self.assertTrue(state.finished)
            self.assertNotIn(self.secret, str(state.public()))
            self.assertEqual(execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
