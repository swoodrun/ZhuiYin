"""Synthetic transports only: real budget reservations in isolated ledgers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import evidence_qualification_experiment as experiment
from tools import evidence_qualification_launcher as launcher
from qualification_scoring import visible_support
from test_qualification_scoring import response


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qualification-test-", dir=Path(__file__).parent / ".tmp-summary-tests")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prepared, self.output = self.root / "prepared", self.root / "output"
        self.design = experiment.prepare(self.prepared)
        self.secret = "synthetic-not-a-real-provider-key"

    def transport(self, body, key):
        self.assertEqual(key, self.secret)
        request = json.loads(body)
        receipt = json.loads(request["messages"][-2]["content"])
        args = {"outcome": visible_support(receipt, self.design["task"]), "reason": "synthetic offline test"}
        if "evidence" in request["tools"][-1]["function"]["parameters"]["properties"]:
            args["evidence"] = {"tool_call_id": "eq-call-4", "observed_request_id": receipt["request_id"],
                "snapshot_complete": receipt["snapshot"]["complete"], "snapshot_consistency": "authoritative",
                "snapshot_id": receipt["snapshot"]["id"], "basis": "record" if receipt["work_orders"] else "empty_result",
                "record_ids": [r["id"] for r in receipt["work_orders"]], "missing_attributes": []}
        return {**response(args), "usage": {"prompt_tokens": 100, "completion_tokens": 30}}

    def test_fixed_24_history_preserved_and_no_secret(self):
        report = experiment.execute(self.prepared, self.output, self.secret, self.transport)
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["budget_after"]["requests_made"], 355)
        self.assertEqual(sum(g["qualified_correct"] for g in report["summary"]["groups"]), 12)
        self.assertEqual(sum(g["justified_unknown"] for g in report["summary"]["groups"]), 12)
        self.assertEqual(sum(g["citation"]["reference_correct"] for g in report["summary"]["groups"] if g["citation"]), 12)
        self.assertEqual(experiment.ledger_rows(self.output / "campaign-ledger-final.sqlite")[:331],
                         experiment.ledger_rows(self.prepared / "campaign/campaign-ledger-initial.sqlite"))
        for file in self.output.rglob("*"):
            if file.is_file():
                self.assertNotIn(self.secret.encode(), file.read_bytes())
        self.assertEqual(len(list((self.output / "samples").glob("*/pending.json"))), 24)
        with self.assertRaises(ValueError):
            experiment.execute(self.prepared, self.root / "rerun", self.secret, self.transport)

    def test_service_error_stops_prefix_and_charges_unknown_usage(self):
        def fail(*_):
            raise experiment.ProviderError("synthetic_connection_error")
        report = experiment.execute(self.prepared, self.output, self.secret, fail)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["summary"]["attempted"], 1)
        self.assertEqual(report["summary"]["not_run"], 23)
        self.assertEqual(report["budget_after"]["requests_made"], 332)
        self.assertEqual(report["budget_after"]["unknown_usage_requests"], 1)
        self.assertFalse((self.output / "samples/01/response.json").exists())

    def test_invalid_reports_continue_and_are_not_unknown(self):
        def invalid(*_):
            return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "no tool"}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10}}
        report = experiment.execute(self.prepared, self.output, self.secret, invalid)
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["summary"]["valid"], 0)
        self.assertTrue(all(s["status"] == "no_report" and "assessment" not in s for s in report["samples"]))

    def test_echo_never_enters_evidence(self):
        def echo(body, key):
            return {"echo": key}
        report = experiment.execute(self.prepared, self.output, self.secret, echo)
        self.assertEqual(report["status"], "partial")
        for file in self.output.rglob("*"):
            if file.is_file():
                self.assertNotIn(self.secret.encode(), file.read_bytes())

    def test_rehashed_request_tamper_cannot_pass_generator(self):
        file = self.prepared / "requests/E0--direct.json"
        payload = json.loads(file.read_bytes())
        payload["messages"][0]["content"] += " assume success"
        file.write_bytes(experiment.canonical(payload))
        manifest_path = self.prepared / "preparation-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"]["requests/E0--direct.json"] = experiment.digest(file)
        experiment.write_json(manifest_path, manifest)
        with self.assertRaises(ValueError):
            experiment.validate_prepared(self.prepared)

    def test_budget_error_no_network_or_fabricated_assessment(self):
        with patch.object(experiment.model_budget, "reserve_request", side_effect=experiment.model_budget.CampaignBudgetError("request_limit")):
            transport = unittest.mock.Mock()
            report = experiment.execute(self.prepared, self.output, self.secret, transport)
        transport.assert_not_called()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["samples"][0]["status"], "budget_error")
        self.assertEqual(report["budget_after"]["requests_made"], 331)

    def test_launcher_once_and_clear_public_state(self):
        state = launcher.LaunchState(self.prepared, self.output)
        with patch.object(launcher.experiment, "execute", return_value={"status": "finished"}) as call:
            self.assertTrue(state.start(self.secret))
            self.assertFalse(state.start(self.secret))
            for thread in __import__("threading").enumerate():
                if thread.name.startswith("Thread-"):
                    thread.join(1)
            self.assertTrue(state.finished)
            self.assertNotIn(self.secret, str(state.public()))
            self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
