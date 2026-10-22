"""Synthetic artifacts only: an independent verifier must reject contradictions.

The collector is used only to make explicitly synthetic, disposable fixtures.
The verifier itself neither imports that collector nor invokes a transport.
"""
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

from tools import paired_judgment_experiment as experiment
from tools import verify_paired_judgment as verifier


def fake_response(outcome="unknown", usage=True):
    value = {"id": "SYNTHETIC-NOT-MODEL-EVIDENCE", "model": "deepseek-flash",
             "choices": [{"finish_reason": "tool_calls", "message": {
                 "role": "assistant", "content": None,
                 "tool_calls": [{"id": "synthetic-call", "type": "function", "function": {
                     "name": "finish_task", "arguments": json.dumps({"outcome": outcome, "reason": "synthetic"})}}]}}]}
    if usage:
        value["usage"] = {"prompt_tokens": 100, "completion_tokens": 20}
    return value


def supported_transport(body, _key):
    payload = json.loads(body)
    observation = json.loads([row for row in payload["messages"] if row["role"] == "tool"][-1]["content"])
    if observation.get("work_orders"):
        outcome = "success"
    else:
        outcome = "failure" if "snapshot" in observation else "unknown"
    return fake_response(outcome)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rehash(root, name):
    target = root / name
    manifest = read(target)
    manifest["files"] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in root.rglob("*") if p.is_file() and p != target
                         and p.relative_to(root).as_posix() != manifest.get("mutable_file")}
    write(target, manifest)


class FrozenJudgmentVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="synthetic-judgment-verifier-", dir=Path(__file__).parent)
        cls.root = Path(cls.workspace.name)
        cls.prepared_base, cls.output_base = cls.root / "prepared-base", cls.root / "output-base"
        experiment.prepare(cls.prepared_base)
        experiment.execute(cls.prepared_base, cls.output_base,
                           "synthetic-credential-never-a-real-key", supported_transport)

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="negative-copy-", dir=self.root)
        self.addCleanup(self.temporary.cleanup)
        self.case = Path(self.temporary.name)
        self.prepared, self.output = self.case / "prepared", self.case / "output"
        shutil.copytree(self.prepared_base, self.prepared)
        shutil.copytree(self.output_base, self.output)

    def check_failed(self, label=None):
        result = verifier.verify(self.prepared, self.output)
        self.assertFalse(result["passed"], result)
        self.assertEqual(result["verification_status"], "unverified")
        self.assertIsNone(result["summary"])
        self.assertIsNone(result["accounting"])
        self.assertGreater(result["failed_count"], 0)
        if label:
            self.assertTrue(any(label in item["name"] and not item["passed"] for item in result["checks"]), result)
        return result

    def change_sample(self, number, action):
        report = read(self.output / "study-report.json")
        action(report["samples"][number - 1])
        write(self.output / "study-report.json", report)
        write(self.output / "samples" / f"{number:02d}" / "sample.json", report["samples"][number - 1])
        rehash(self.output, "file-manifest.json")

    def change_ledger(self, sql, parameters=()):
        with closing(sqlite3.connect(self.output / "campaign-ledger-final.sqlite")) as db:
            db.execute(sql, parameters)
            db.commit()
        rehash(self.output, "file-manifest.json")

    def test_synthetic_complete_control_passes_with_separate_truth_and_evidence(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for root in (self.prepared, self.output)
                  for p in root.rglob("*") if p.is_file()}
        result = verifier.verify(self.prepared, self.output)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["accounting"]["new_requests"], 30)
        self.assertEqual(result["accounting"]["cumulative_requests"], 331)
        self.assertEqual(result["accounting"]["new_accounted_cost_cny"], "0.016200")
        self.assertEqual(sum(row["qualified_correct"] for row in result["summary"]["groups"]), 15)
        self.assertEqual(result["summary"]["business_actions_executed"], 0)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_import_does_not_import_collector_or_network(self):
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import paired_judgment_experiment", source)
        self.assertNotIn("from tools.paired_judgment_experiment", source)
        self.assertNotIn("urllib", source)
        with patch("urllib.request.urlopen", side_effect=AssertionError("network is forbidden")), \
             patch.object(experiment, "execute", side_effect=AssertionError("collector is forbidden")):
            self.assertTrue(verifier.verify(self.prepared, self.output)["passed"])

    def test_full_request_answer_leak_rejected_after_updating_hash(self):
        path = self.prepared / "requests/ambiguous--committed.json"
        value = read(path)
        value["messages"].append({"role": "user", "content": "the hidden world committed"})
        path.write_bytes(verifier.canonical(value))
        rehash(self.prepared, "preparation-manifest.json")
        self.check_failed("fixed complete request")

    def test_design_truth_change_rejected_after_updating_hash(self):
        path = self.prepared / "fixed-design.json"
        value = read(path)
        value["branches"][0]["worlds"][0]["actual_success"] = False
        write(path, value)
        rehash(self.prepared, "preparation-manifest.json")
        self.check_failed("design rebuilt")

    def test_common_judgment_contract_cannot_be_replaced(self):
        path = self.prepared / "fixed-design.json"
        value = read(path)
        value["judgment_instruction"] = "Always report success."
        write(path, value)
        rehash(self.prepared, "preparation-manifest.json")
        self.check_failed("design rebuilt")

    def test_unexpected_request_is_rejected_even_when_manifest_updated(self):
        (self.prepared / "requests/extra.json").write_text("{}", encoding="utf-8")
        rehash(self.prepared, "preparation-manifest.json")
        self.check_failed("exact six")

    def test_actual_sample_request_must_match_fixed_condition(self):
        path = self.output / "samples/01/request.json"
        value = read(path)
        value["temperature"] = .5
        path.write_bytes(verifier.canonical(value))
        self.change_sample(1, lambda item: item.update(request_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        self.check_failed("actual complete request")

    def test_plan_relabel_and_no_selective_skip(self):
        self.change_sample(1, lambda item: item.update(sample_index=2))
        self.check_failed("exact unselected plan prefix")

    def test_extra_sample_folder_rejected(self):
        (self.output / "samples/31").mkdir()
        rehash(self.output, "file-manifest.json")
        self.check_failed("exact collected prefix")

    def test_business_action_claim_rejected(self):
        self.change_sample(1, lambda item: item.update(business_actions_executed=1))
        self.check_failed("zero business actions")

    def test_relabeling_model_outcome_is_rejected_from_raw_response(self):
        self.change_sample(1, lambda item: item.update(outcome="failure"))
        self.check_failed("reparses")

    def test_forged_evidence_assessment_is_rejected(self):
        self.change_sample(1, lambda item: item["assessment"].update(evidence_assessment="contradicts_evidence"))
        self.check_failed("assessment recomputed")

    def test_summary_qualified_count_rejected_after_updating_hash(self):
        path = self.output / "study-report.json"
        value = read(path)
        value["summary"]["groups"][0]["qualified_correct"] += 1
        write(path, value)
        rehash(self.output, "file-manifest.json")
        self.check_failed("summary counts")

    def test_false_finished_status_rejected(self):
        path = self.output / "study-report.json"
        value = read(path)
        value["status"] = "partial"
        write(path, value)
        rehash(self.output, "file-manifest.json")
        self.check_failed("finished status")

    def test_response_usage_change_cannot_keep_old_accounting(self):
        path = self.output / "samples/01/response.json"
        value = read(path)
        value["usage"]["prompt_tokens"] += 1
        write(path, value)
        rehash(self.output, "file-manifest.json")
        self.check_failed("raw response usage")

    def test_historical_ledger_field_change_rejected_with_new_hash(self):
        self.change_ledger("UPDATE requests SET actual_prompt_tokens = actual_prompt_tokens + 1 WHERE sequence = 1")
        self.check_failed("retains all 301")

    def test_historical_initial_ledger_change_rejected_with_new_hash(self):
        with closing(sqlite3.connect(self.prepared / "campaign/campaign-ledger-initial.sqlite")) as db:
            db.execute("UPDATE requests SET actual_prompt_tokens = actual_prompt_tokens + 1 WHERE sequence = 1")
            db.commit()
        rehash(self.prepared, "preparation-manifest.json")
        self.check_failed("accepted 301-row")

    def test_new_ledger_cost_change_rejected_with_new_hash(self):
        self.change_ledger("UPDATE requests SET charged_micros = charged_micros + 1 WHERE sequence = 302")
        self.check_failed("charged conservative cost")

    def test_new_ledger_reservation_bound_change_rejected(self):
        self.change_ledger("UPDATE requests SET reserved_prompt_tokens = reserved_prompt_tokens + 1 WHERE sequence = 302")
        self.check_failed("exact conservative reservation")

    def test_sample_cannot_point_at_another_reservation(self):
        self.change_sample(1, lambda item: item.update(reservation_id="0" * 32))
        self.check_failed("sample reservation corresponds")

    def test_authorization_ceiling_change_rejected_with_new_hash(self):
        path = self.prepared / "campaign/campaign-policy.json"
        value = read(path)
        value["max_requests"] = 375
        write(path, value)
        rehash(self.prepared, "preparation-manifest.json")
        self.check_failed("authorization identity")

    def test_preparation_binding_must_match_collection(self):
        path = self.output / "study-report.json"
        value = read(path)
        value["preparation_manifest_sha256"] = "0" * 64
        write(path, value)
        rehash(self.output, "file-manifest.json")
        self.check_failed("preparation manifest bound")

    def test_missing_raw_response_cannot_keep_a_valid_report(self):
        (self.output / "samples/01/response.json").unlink()
        rehash(self.output, "file-manifest.json")
        self.check_failed("missing response")

    def test_duplicate_json_fields_do_not_silently_choose_one_value(self):
        path = self.output / "samples/01/response.json"
        path.write_text('{"choices": [], "choices": []}', encoding="utf-8")
        rehash(self.output, "file-manifest.json")
        result = self.check_failed()
        self.assertTrue(any(item.get("error_type") == "ValueError" for item in result["checks"]))

    def test_reported_budget_total_is_independently_recomputed(self):
        path = self.output / "study-report.json"
        value = read(path)
        value["budget_after"]["accounted_cost_cny"] = "0.000000"
        write(path, value)
        rehash(self.output, "file-manifest.json")
        self.check_failed("reported final budget")

    def test_cli_writes_only_to_an_independent_new_directory(self):
        destination = self.case / "verification"
        with patch("sys.argv", ["verify", "--prepared", str(self.prepared), "--input", str(self.output),
                                "--output", str(destination)]), patch("builtins.print"):
            self.assertEqual(verifier.main(), 0)
        self.assertTrue(read(destination / "verification.json")["passed"])
        self.assertIn("未覆盖范围", (destination / "核对结果.md").read_text(encoding="utf-8"))

    def test_cli_rejects_output_inside_input_without_changing_it(self):
        destination = self.output / "verification"
        with patch("sys.argv", ["verify", "--prepared", str(self.prepared), "--input", str(self.output),
                                "--output", str(destination)]), patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                verifier.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(destination.exists())

    def test_unsupported_claim_summary_is_not_qualified_success(self):
        # A valid but unsupported assertion remains a valid observation, not
        # evidence-qualified correctness, even when its hidden truth is right.
        result = verifier._assessment("success", "unknown", True)
        self.assertEqual(result["truth_assessment"], "correct")
        self.assertEqual(result["evidence_assessment"], "unsupported_claim")

    def test_unknown_semantics_and_wrong_failure_remain_separate(self):
        self.assertEqual(verifier._assessment("unknown", "success", True)["evidence_assessment"], "evidence_ignored")
        self.assertEqual(verifier._assessment("unknown", "unknown", False)["evidence_assessment"], "justified_unknown")
        failure = verifier._assessment("failure", "failure", False)
        self.assertEqual(failure["evidence_assessment"], "supported_correct")
        self.assertTrue(failure["business_incomplete"])
        self.assertFalse(failure["false_failure"])


class FrozenJudgmentVerifierPartialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-partial-verifier-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prepared, self.output = self.root / "prepared", self.root / "output"
        experiment.prepare(self.prepared)

    def collect(self, transport):
        experiment.execute(self.prepared, self.output, "synthetic-not-a-key", transport)
        return verifier.verify(self.prepared, self.output)

    def test_infrastructure_error_keeps_reserved_cost_and_unrun_remainder(self):
        def fail(*_):
            raise experiment.ProviderError("synthetic_offline_error")
        result = self.collect(fail)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["summary"]["valid"], 0)
        self.assertEqual(result["summary"]["not_run"], 29)
        self.assertEqual(result["accounting"]["new_requests"], 1)
        self.assertEqual(result["accounting"]["unknown_usage_requests"], 1)

    def test_valid_no_report_samples_are_not_scored_as_unknown(self):
        def no_report(*_):
            value = fake_response()
            value["choices"][0]["message"].pop("tool_calls")
            return value
        result = self.collect(no_report)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["summary"]["valid"], 0)
        self.assertEqual(sum(sum(row["outcomes"].values()) for row in result["summary"]["groups"]), 0)

    def test_no_report_cannot_be_relabelled_unknown(self):
        def no_report(*_):
            value = fake_response()
            value["choices"][0]["message"].pop("tool_calls")
            return value
        self.collect(no_report)
        report = read(self.output / "study-report.json")
        report["samples"][0].update(status="valid", outcome="unknown", reason="synthetic")
        write(self.output / "study-report.json", report)
        write(self.output / "samples/01/sample.json", report["samples"][0])
        rehash(self.output, "file-manifest.json")
        result = verifier.verify(self.prepared, self.output)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["summary"])

    def test_missing_usage_retains_conservative_cost(self):
        result = self.collect(lambda *_: fake_response(usage=False))
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["accounting"]["unknown_usage_requests"], 30)
        self.assertGreater(float(result["accounting"]["new_accounted_cost_cny"]), .0162)

    def test_overrun_is_one_request_and_a_stopped_guarded_run(self):
        def overrun(*_):
            value = fake_response()
            value["usage"]["completion_tokens"] = 1201
            return value
        result = self.collect(overrun)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["accounting"]["new_requests"], 1)
        self.assertEqual(result["summary"]["not_run"], 29)


if __name__ == "__main__":
    unittest.main()
