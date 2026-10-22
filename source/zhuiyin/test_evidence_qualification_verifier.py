"""Disposable synthetic fixtures for the independent qualification audit."""
from copy import deepcopy
from contextlib import closing
import ast
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import evidence_qualification as generator
from tools import verify_evidence_qualification as verifier


ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rehash(root, filename, mutable=None):
    path = root / filename
    value = read(path)
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file() and p != path
             and p.relative_to(root).as_posix() != mutable}
    if filename == "file-manifest.json" and "files" not in value:
        write(path, files)
    else:
        value["files"] = files
        write(path, value)


def synthetic_prepared(root):
    """Only the test fixture calls a generator; the verifier never does."""
    root.mkdir()
    design = generator.build_fixture(root / "fixture")
    write(root / "fixed-design.json", design)
    (root / "requests").mkdir()
    for case in design["cases"]:
        for protocol, payload in case["payloads"].items():
            (root / "requests" / f"{case['condition_id']}--{protocol}.json").write_bytes(verifier.canonical(payload))
    (root / "campaign").mkdir()
    historical = ROOT / "data/evaluations/paired-judgment-real-20261018"
    for name in ("campaign-ledger-initial.sqlite", "campaign-ledger.sqlite"):
        shutil.copyfile(historical / "campaign-ledger-final.sqlite", root / "campaign" / name)
    policy = read(ROOT / "data/research/paired-judgment-20261018-prepared/campaign/campaign-policy.json")
    policy["ledger_path"] = str(root / "campaign/campaign-ledger.sqlite")
    write(root / "campaign/campaign-policy.json", policy)
    write(root / "campaign/initial-summary.json", read(historical / "study-report.json")["budget_after"])
    write(root / "preparation-manifest.json", {"mutable_file": "campaign/campaign-ledger.sqlite", "files": {}})
    rehash(root, "preparation-manifest.json", "campaign/campaign-ledger.sqlite")


class QualificationPreparedAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="SYNTHETIC-qualification-audit-", dir=ROOT)
        cls.base = Path(cls.workspace.name) / "base"
        synthetic_prepared(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="negative-copy-", dir=Path(self.workspace.name))
        self.addCleanup(self.temporary.cleanup)
        self.prepared = Path(self.temporary.name) / "prepared"
        shutil.copytree(self.base, self.prepared)

    def _rehash(self):
        rehash(self.prepared / "fixture", "file-manifest.json")
        rehash(self.prepared, "preparation-manifest.json", "campaign/campaign-ledger.sqlite")

    def fail(self, label=None):
        result = verifier.verify_prepared(self.prepared)
        self.assertFalse(result["passed"], result)
        self.assertIsNone(result["summary"])
        self.assertIsNone(result["accounting"])
        self.assertEqual(result["verification_status"], "unverified")
        if label:
            self.assertTrue(any(label in x["name"] and not x["passed"] for x in result["checks"]), result)

    def change_design(self, mutation):
        path = self.prepared / "fixture/design.json"
        value = read(path)
        mutation(value)
        write(path, value)
        write(self.prepared / "fixed-design.json", value)
        self._rehash()

    def test_locked_prepared_control_passes_and_does_not_modify_inputs(self):
        before = {p: verifier.digest(p) for p in self.prepared.rglob("*") if p.is_file()}
        result = verifier.verify_prepared(self.prepared)
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["summary"]["model_behavior_observed"])
        self.assertEqual(result["accounting"]["initial_requests"], 331)
        self.assertEqual(result["accounting"]["initial_accounted_cost_cny"], "0.922815")
        self.assertEqual(before, {p: verifier.digest(p) for p in before})

    def test_verifier_imports_only_standard_library_and_never_generator(self):
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        names = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names.extend(x.name.split(".")[0] for x in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append((node.module or "").split(".")[0])
        self.assertTrue(set(names) <= {"__future__", "argparse", "contextlib", "copy", "decimal", "hashlib", "json", "math", "pathlib", "sqlite3"}, names)

    def test_false_truth_label_rejected_after_rehash(self):
        self.change_design(lambda d: d["cases"][0].update(actual_success=True))
        self.fail("actual frozen database truth")

    def test_partial_receipt_changed_to_complete_rejected_after_rehash(self):
        self.change_design(lambda d: d["cases"][1]["receipt"]["snapshot"].update(complete=True))
        self.fail("real visible result")

    def test_foreign_request_rebound_to_current_rejected_after_rehash(self):
        self.change_design(lambda d: d["cases"][2]["receipt"].update(request_id=generator.TASK["request_id"]))
        self.fail("real visible result")

    def test_counterworld_truth_cannot_be_relabelled(self):
        self.change_design(lambda d: d["cases"][1]["counterworld"].update(actual_success=True))
        self.fail("different-truth counterworlds")

    def test_counterworld_context_leak_rejected_after_rehash(self):
        self.change_design(lambda d: d["cases"][2]["counterworld"]["payloads"]["direct"]["messages"].append({"role": "user", "content": "hidden truth false"}))
        self.fail("same complete visible inputs")

    def test_hidden_truth_request_field_rejected_after_rehash(self):
        path = self.prepared / "requests/E0--direct.json"
        payload = read(path)
        payload["actual_success"] = False
        path.write_bytes(verifier.canonical(payload))
        self._rehash()
        self.fail("actual prepared bytes")

    def test_unconditional_authority_contract_is_rejected(self):
        self.change_design(lambda d: d["cases"][1]["payloads"]["direct"]["messages"][0].update(content="all receipts are complete authoritative current-request snapshots"))
        self.fail("independently fixed complete candidate")

    def test_wrong_tool_response_binding_rejected(self):
        self.change_design(lambda d: d["cases"][0]["payloads"]["direct"]["messages"][9].update(tool_call_id="eq-call-3"))
        self.fail("independently fixed complete candidate")

    def test_plan_skip_rejected_after_rehash(self):
        self.change_design(lambda d: d["execution_plan"][0].update(sample_index=2))
        self.fail("fixed three full balanced blocks")

    def test_late_commit_scope_cannot_be_weakened(self):
        self.change_design(lambda d: d["scope"].update(no_late_commit=False))
        self.fail("scope fixed")

    def test_sql_audit_cannot_claim_different_page(self):
        self.change_design(lambda d: d["cases"][1]["audit"].update(parameters=[generator.TASK["request_id"], 0, 1]))
        self.fail("controlled SQL")

    def test_correct_database_record_cannot_be_silently_changed(self):
        path = self.prepared / "fixture/E3.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE work_orders SET title='wrong'")
            db.commit()
        self._rehash()
        self.fail("actual frozen database truth")

    def test_frozen_snapshot_metadata_cannot_change(self):
        with closing(sqlite3.connect(self.prepared / "fixture/E0.sqlite")) as db:
            db.execute("UPDATE experiment_metadata SET snapshot_id='later'")
            db.commit()
        self._rehash()
        self.fail()

    def test_extra_candidate_request_rejected(self):
        (self.prepared / "requests/hidden-extra.json").write_text("{}", encoding="utf-8")
        self._rehash()
        self.fail("exact eight canonical")

    def test_historical_331_ledger_mutation_rejected(self):
        with closing(sqlite3.connect(self.prepared / "campaign/campaign-ledger-initial.sqlite")) as db:
            db.execute("UPDATE requests SET charged_micros=charged_micros+1 WHERE sequence=331")
            db.commit()
        self._rehash()
        self.fail("accepted 331-row historical prefix")

    def test_returning_to_historical_301_ledger_is_rejected(self):
        with closing(sqlite3.connect(self.prepared / "campaign/campaign-ledger-initial.sqlite")) as db:
            db.execute("DELETE FROM requests WHERE sequence>301")
            db.execute("UPDATE sqlite_sequence SET seq=301 WHERE name='requests'")
            db.commit()
        self._rehash()
        self.fail("accepted 331-row historical prefix")

    def test_budget_identity_does_not_allow_another_model(self):
        path = self.prepared / "campaign/campaign-policy.json"
        value = read(path)
        value["allowed_model"] = "another-model"
        write(path, value)
        self._rehash()
        self.fail("authorized model origin")

    def test_manifest_path_escape_rejected(self):
        path = self.prepared / "preparation-manifest.json"
        value = read(path)
        value["files"]["../outside.json"] = "0" * 64
        write(path, value)
        self.fail()

    def test_prepared_cli_runs_real_module_entry_point(self):
        target = Path(self.temporary.name) / "cli-prepared"
        completed = subprocess.run([sys.executable, "-X", "utf8", verifier.__file__,
                                   "--prepared", str(self.prepared), "--output", str(target)],
                                  capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["passed"])
        self.assertTrue(read(target / "verification.json")["passed"])


def synthetic_response(body, _key):
    payload = json.loads(body)
    receipt = json.loads(payload["messages"][9]["content"])
    outcome = ("unknown" if receipt["request_id"] != generator.TASK["request_id"]
               or receipt["snapshot"]["complete"] is not True else
               "success" if receipt["work_orders"] else "failure")
    args = {"outcome": outcome, "reason": "SYNTHETIC contract observation, not a model run"}
    if "evidence" in payload["tools"][-1]["function"]["parameters"]["properties"]:
        args["evidence"] = {"tool_call_id": "eq-call-4", "observed_request_id": receipt["request_id"],
                            "snapshot_complete": receipt["snapshot"]["complete"],
                            "snapshot_consistency": receipt["snapshot"]["consistency"],
                            "snapshot_id": receipt["snapshot"]["id"],
                            "basis": "record" if receipt["work_orders"] else "empty_result",
                            "record_ids": [x["id"] for x in receipt["work_orders"]], "missing_attributes": []}
    return {"id": "SYNTHETIC-NOT-REAL-MODEL-EVIDENCE", "model": "deepseek-flash",
            "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
             "tool_calls": [{"id": "synthetic-finish", "type": "function", "function": {
             "name": "finish_task", "arguments": json.dumps(args, ensure_ascii=False)}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class QualificationCollectedAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools import evidence_qualification_experiment as experiment
        cls.workspace = tempfile.TemporaryDirectory(prefix="SYNTHETIC-qualification-collection-", dir=ROOT)
        cls.root = Path(cls.workspace.name)
        cls.prepared_base, cls.output_base = cls.root / "prepared-base", cls.root / "output-base"
        experiment.prepare(cls.prepared_base)
        experiment.execute(cls.prepared_base, cls.output_base, "SYNTHETIC-NOT-A-REAL-KEY", synthetic_response)

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="negative-copy-", dir=self.root)
        self.addCleanup(self.temporary.cleanup)
        self.prepared = Path(self.temporary.name) / "prepared"
        self.output = Path(self.temporary.name) / "output"
        shutil.copytree(self.prepared_base, self.prepared)
        shutil.copytree(self.output_base, self.output)

    def fail(self, label=None):
        result = verifier.verify(self.prepared, self.output)
        self.assertFalse(result["passed"], result)
        self.assertIsNone(result["summary"])
        self.assertIsNone(result["accounting"])
        if label:
            self.assertTrue(any(label in x["name"] and not x["passed"] for x in result["checks"]), result)

    def mutate_sample(self, number, mutation):
        report = read(self.output / "study-report.json")
        mutation(report["samples"][number - 1])
        write(self.output / "study-report.json", report)
        write(self.output / "samples" / f"{number:02d}" / "sample.json", report["samples"][number - 1])
        rehash(self.output, "file-manifest.json")

    def mutate_raw_args(self, number, mutation):
        path = self.output / "samples" / f"{number:02d}" / "response.json"
        response = read(path)
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        args = json.loads(function["arguments"])
        mutation(args)
        function["arguments"] = json.dumps(args, ensure_ascii=False)
        write(path, response)
        rehash(self.output, "file-manifest.json")

    def test_synthetic_complete_result_passes_independent_recomputation(self):
        before = {p: verifier.digest(p) for root in (self.prepared, self.output) for p in root.rglob("*") if p.is_file()}
        result = verifier.verify(self.prepared, self.output)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["accounting"]["new_requests"], 24)
        self.assertEqual(result["accounting"]["cumulative_requests"], 355)
        self.assertEqual(result["accounting"]["cumulative_accounted_cost_cny"], "0.935775")
        self.assertEqual(sum(x["target_correct"] for x in result["summary"]["groups"]), 24)
        self.assertEqual(sum(x["qualified_correct"] for x in result["summary"]["groups"]), 12)
        self.assertEqual(before, {p: verifier.digest(p) for p in before})

    def test_foreign_reference_can_be_correct_but_not_task_bound(self):
        result = verifier.verify(self.prepared, self.output)
        group = next(x for x in result["summary"]["groups"] if (x["condition_id"], x["protocol_id"]) == ("E2", "cited"))
        self.assertEqual(group["citation"]["reference_correct"], 3)
        self.assertEqual(group["citation"]["task_bound"], 0)
        self.assertEqual(group["justified_unknown"], 3)

    def test_false_summary_rejected_even_after_rehash(self):
        path = self.output / "study-report.json"
        report = read(path)
        report["summary"]["groups"][0]["target_correct"] += 1
        write(path, report)
        rehash(self.output, "file-manifest.json")
        self.fail("all stratified counts")

    def test_raw_outcome_and_recorded_outcome_contradiction_rejected(self):
        self.mutate_raw_args(1, lambda args: args.update(outcome="success"))
        self.fail("raw response independently reparses")

    def test_false_score_cannot_turn_supported_unknown_into_success(self):
        self.mutate_sample(3, lambda item: item["assessment"].update(evidence_assessment="supported_correct"))
        self.fail("three-column assessment")

    def test_invented_citation_reference_rejected_when_claimed_valid(self):
        self.mutate_sample(1, lambda item: item["evidence"].update(tool_call_id="invented"))
        self.fail("raw response independently reparses")

    def test_forged_reference_score_rejected(self):
        self.mutate_sample(4, lambda item: item["citation"].update(task_bound=True))
        self.fail("reference attributes applicability")

    def test_foreign_record_not_allowed_to_impersonate_current_request(self):
        self.mutate_raw_args(4, lambda args: args["evidence"].update(observed_request_id=generator.TASK["request_id"]))
        self.fail("raw response independently reparses")

    def test_partial_false_is_not_missing_or_true(self):
        self.mutate_raw_args(8, lambda args: args["evidence"].update(snapshot_complete=True))
        self.fail("raw response independently reparses")

    def test_wrong_final_report_after_fully_consistent_raw_relabel_is_rejected(self):
        self.mutate_raw_args(1, lambda args: args.update(outcome="success"))
        self.mutate_sample(1, lambda item: item.update(outcome="success"))
        self.fail("three-column assessment")

    def test_actual_request_uses_prepared_full_body(self):
        path = self.output / "samples/01/request.json"
        payload = read(path)
        payload["temperature"] = 1
        path.write_bytes(verifier.canonical(payload))
        self.mutate_sample(1, lambda item: item.update(request_sha256=verifier.digest(path)))
        self.fail("actual saved complete request")

    def test_response_usage_mutation_cannot_keep_old_ledger(self):
        path = self.output / "samples/01/response.json"
        response = read(path)
        response["usage"]["completion_tokens"] += 1
        write(path, response)
        rehash(self.output, "file-manifest.json")
        self.fail("raw response usage")

    def test_pending_before_network_must_match_reservation(self):
        path = self.output / "samples/01/pending.json"
        pending = read(path)
        pending["reservation_id"] = "a" * 32
        write(path, pending)
        rehash(self.output, "file-manifest.json")
        self.fail("pre-network pending")

    def test_completed_plan_cannot_be_relabelled_partial(self):
        path = self.output / "study-report.json"
        report = read(path)
        report["status"] = "partial"
        write(path, report)
        rehash(self.output, "file-manifest.json")
        self.fail("finished status")

    def test_arbitrary_or_fabricated_stopping_reason_is_rejected(self):
        path = self.output / "study-report.json"
        report = read(path)
        report.update(status="partial", stop_reason="provider_or_connection_error")
        write(path, report)
        rehash(self.output, "file-manifest.json")
        self.fail("stopping reason is preregistered")

    def test_fabricated_settlement_error_does_not_justify_early_stop(self):
        path = self.output / "study-report.json"
        report = read(path)
        report.update(status="partial", stop_reason="budget_guard")
        report["samples"][-1]["settlement_error"] = "usage_bound_exceeded"
        write(path, report)
        write(self.output / "samples/24/sample.json", report["samples"][-1])
        rehash(self.output, "file-manifest.json")
        self.fail("no invented settlement failure")

    def test_running_material_cannot_yield_verified_conclusions(self):
        path = self.output / "study-report.json"
        report = read(path)
        report["status"] = "running"
        write(path, report)
        rehash(self.output, "file-manifest.json")
        self.fail("finished status")

    def test_selective_deletion_of_run_is_rejected(self):
        path = self.output / "study-report.json"
        report = read(path)
        report["samples"].pop(1)
        write(path, report)
        rehash(self.output, "file-manifest.json")
        self.fail("sample folders")

    def test_historical_prefix_mutation_rejected(self):
        with closing(sqlite3.connect(self.output / "campaign-ledger-final.sqlite")) as db:
            db.execute("UPDATE requests SET charged_micros=charged_micros+1 WHERE sequence=331")
            db.commit()
        rehash(self.output, "file-manifest.json")
        self.fail("accepted 331 historical rows")

    def test_new_cost_mutation_rejected(self):
        with closing(sqlite3.connect(self.output / "campaign-ledger-final.sqlite")) as db:
            db.execute("UPDATE requests SET charged_micros=charged_micros+1 WHERE sequence=332")
            db.commit()
        rehash(self.output, "file-manifest.json")
        self.fail("conservative state and charge")

    def test_collected_cli_runs_real_module_entry_point(self):
        target = Path(self.temporary.name) / "cli-collected"
        completed = subprocess.run([sys.executable, "-X", "utf8", verifier.__file__,
                                   "--prepared", str(self.prepared), "--input", str(self.output),
                                   "--output", str(target)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["passed"])
        self.assertEqual(read(target / "verification.json")["accounting"]["new_requests"], 24)

    def _new_partial(self, transport=None, reserve_error=False):
        from tools import evidence_qualification_experiment as experiment
        prepared = Path(self.temporary.name) / "partial-prepared"
        output = Path(self.temporary.name) / "partial-output"
        experiment.prepare(prepared)
        if reserve_error:
            with patch.object(experiment.model_budget, "reserve_request",
                              side_effect=experiment.model_budget.CampaignBudgetError("request_limit_reached")):
                experiment.execute(prepared, output, "SYNTHETIC-NOT-A-REAL-KEY", synthetic_response)
        else:
            experiment.execute(prepared, output, "SYNTHETIC-NOT-A-REAL-KEY", transport)
        result = verifier.verify(prepared, output)
        self.assertTrue(result["passed"], result)
        self.assertEqual(read(output / "study-report.json")["status"], "partial")
        return result

    def test_terminal_infrastructure_failure_retains_unknown_reservation(self):
        from tools.evidence_qualification_experiment import ProviderError
        def failed_transport(_body, _key):
            raise ProviderError("synthetic_connection_failure")
        result = self._new_partial(failed_transport)
        self.assertEqual(result["accounting"]["new_requests"], 1)
        self.assertEqual(result["accounting"]["unknown_usage_requests"], 1)

    def test_terminal_budget_rejection_without_reservation_is_valid_partial(self):
        result = self._new_partial(reserve_error=True)
        self.assertEqual(result["accounting"]["new_requests"], 0)
        self.assertEqual(result["summary"]["attempted"], 1)

    def test_real_usage_overrun_stops_and_is_verifiable(self):
        def overrun_transport(body, key):
            response = synthetic_response(body, key)
            response["usage"]["completion_tokens"] = 1201
            return response
        result = self._new_partial(overrun_transport)
        self.assertEqual(result["accounting"]["new_requests"], 1)
        self.assertEqual(result["summary"]["attempted"], 1)


if __name__ == "__main__":
    unittest.main()
