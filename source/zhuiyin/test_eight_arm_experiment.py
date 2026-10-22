"""Fixed eight-arm collection is offline by default and retains fresh evidence."""
import copy
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import engine
from tools import eight_arm_experiment as experiment


class EightArmExperimentTests(unittest.TestCase):
    def setUp(self):
        # Stay inside the writable project instead of depending on system TEMP
        # permissions, which may differ under independent review sandboxes.
        self.temporary = tempfile.TemporaryDirectory(prefix="eight-arm-test-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.design = experiment.fixed_design()
        self.budget = experiment.readonly_budget()

    def prepare(self, name="prepared"):
        destination = self.root / name
        with patch.object(experiment, "fixed_design", return_value=copy.deepcopy(self.design)), \
                patch.object(experiment, "readonly_budget", return_value=copy.deepcopy(self.budget)):
            experiment.prepare(destination)
        return destination

    def execute(self, prepared, output, *args, **kwargs):
        with patch.object(experiment, "fixed_design", return_value=copy.deepcopy(self.design)):
            return experiment.execute(prepared, output, *args, **kwargs)

    @staticmethod
    def read_json(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_fixed_design_is_two_policies_four_tools_two_repetitions_with_144_request_bound(self):
        design = self.design
        self.assertEqual(design["config"]["scenario"], "ticket")
        self.assertEqual(design["config"]["seed"], 42)
        self.assertEqual(design["config"]["faults"], ["commit_response_lost", "stale_read"])
        self.assertEqual(design["options"]["prompt_policies"], ["neutral", "recovery"])
        self.assertEqual(design["options"]["strategies"],
                         ["baseline", "idempotency_only", "verification_only", "verified"])
        self.assertEqual(design["options"]["repetitions"], 2)
        self.assertEqual((design["planned_trials"], design["max_requests_per_trial"],
                          design["worst_case_requests"]), (16, 9, 144))
        self.assertEqual(design["options"]["max_requests"], 144)
        self.assertEqual(design["planned_trials"] * engine.MAX_TRIAL_REQUESTS,
                         design["worst_case_requests"])
        actual = [(trial["prompt_policy"], trial["strategy"])
                  for row in design["execution_plan"] for trial in row["trials"]]
        expected = [("neutral", strategy) for strategy in
                    ("baseline", "idempotency_only", "verification_only", "verified")]
        expected += [("recovery", strategy) for strategy in
                     ("idempotency_only", "verification_only", "verified", "baseline")]
        expected += [("recovery", strategy) for strategy in
                     ("verification_only", "verified", "baseline", "idempotency_only")]
        expected += [("neutral", strategy) for strategy in
                     ("idempotency_only", "verification_only", "verified", "baseline")]
        self.assertEqual(actual, expected)
        for policy in ("neutral", "recovery"):
            for strategy in ("baseline", "idempotency_only", "verification_only", "verified"):
                self.assertEqual(actual.count((policy, strategy)), 2)

    def test_prepare_is_model_free_and_keeps_existing_ledger_and_policy_bytes(self):
        policy = self.read_json(experiment.POLICY)
        ledger = Path(policy["ledger_path"])
        original_ledger = ledger.read_bytes()
        original_policy = experiment.POLICY.read_bytes()
        destination = self.root / "prepared-readonly"
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial:
            experiment.prepare(destination)
        model.assert_not_called()
        trial.assert_not_called()
        self.assertEqual(ledger.read_bytes(), original_ledger)
        self.assertEqual(experiment.POLICY.read_bytes(), original_policy)
        state = self.read_json(destination / "prepared-at.json")
        self.assertEqual(state["state"], "prepared_not_executed")
        self.assertEqual(state["new_model_requests"], 0)
        budget = self.read_json(destination / "budget-readiness.json")
        self.assertEqual(budget["readiness"]["status"], "blocked")
        self.assertIn("full_design_request_capacity_insufficient", budget["readiness"]["reasons"])
        self.assertEqual(budget["readiness"]["required_requests"], 144)
        self.assertFalse(budget["readiness"]["proposal_active"])
        self.assertTrue((destination / "NOT_A_MODEL_RESULT.md").is_file())
        self.assertTrue((destination / "source_snapshot" / "eight_arm_experiment.py").is_file())
        self.assertEqual(self.read_json(destination / "design-sha256.json")["sha256"],
                         hashlib.sha256(experiment.canonical(
                             self.read_json(destination / "fixed-design.json"))).hexdigest())

    def test_explicit_policy_binds_design_and_prepare_to_same_policy_without_mutation(self):
        policy_path = self.root / "selected-policy.json"
        ledger = self.root / "selected-ledger.sqlite"
        raw = self.read_json(experiment.POLICY)
        raw.update(ledger_path=str(ledger), input_price_per_million="4")
        experiment.write_json(policy_path, raw)
        with experiment.campaign_environment(policy_path):
            experiment.model_budget.public_summary()
        policy_before, ledger_before = policy_path.read_bytes(), ledger.read_bytes()
        selected_budget = experiment.readonly_budget(policy_path)
        selected_design = experiment.fixed_design(policy_path)
        self.assertNotEqual(selected_design["expected_campaign_policy_sha256"],
                            self.design["expected_campaign_policy_sha256"])
        self.assertEqual(selected_design["expected_campaign_policy_sha256"], selected_budget["policy_sha256"])
        self.assertEqual(selected_design["execution_plan"], self.design["execution_plan"])
        self.assertEqual((selected_design["planned_trials"], selected_design["worst_case_requests"]), (16, 144))
        destination = self.root / "selected-prepare"
        with patch.object(engine, "model_request") as model:
            experiment.prepare(destination, policy_path=policy_path)
        model.assert_not_called()
        self.assertEqual(self.read_json(destination / "fixed-design.json"), selected_design)
        self.assertEqual(self.read_json(destination / "budget-readiness.json")["budget"], selected_budget)
        self.assertEqual(policy_path.read_bytes(), policy_before)
        self.assertEqual(ledger.read_bytes(), ledger_before)

        output = self.root / "wrong-selected-policy"
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial:
            with self.assertRaisesRegex(ValueError, "预算政策与准备时不同"):
                experiment.execute(destination, output, policy_path=experiment.POLICY)
        model.assert_not_called()
        trial.assert_not_called()
        self.assertFalse(output.exists())

    def test_source_registration_or_policy_change_requires_fresh_preparation(self):
        prepared = self.prepare()
        for key in ("registration_sha256", "collector_sha256", "runtime_sha256",
                    "expected_campaign_policy_sha256"):
            changed = copy.deepcopy(self.design)
            changed[key] = {"engine.py": "0" * 64} if key == "runtime_sha256" else "0" * 64
            output = self.root / ("changed-" + key)
            with self.subTest(binding=key), \
                    patch.object(experiment, "fixed_design", return_value=changed), \
                    patch.object(engine, "model_request") as model, \
                    patch.object(engine, "run_trial") as trial:
                with self.assertRaisesRegex(ValueError, "重新准备"):
                    experiment.execute(prepared, output)
                model.assert_not_called()
                trial.assert_not_called()
                self.assertFalse(output.exists())

    def test_cli_selected_policy_reaches_prepare_and_execute(self):
        selected = self.root / "selected-cli-policy.json"
        output = self.root / "cli-output"
        prepared = self.root / "cli-prepared"
        with patch.object(experiment.sys, "argv", ["eight-arm", "prepare", "--output", str(output),
                                                  "--policy", str(selected)]), \
                patch.object(experiment, "prepare", return_value=self.design) as prepare, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(experiment.main(), 0)
        prepare.assert_called_once_with(str(output), policy_path=selected)
        report = {"mode": "llm", "status": "finished", "completed": 16,
                  "model_usage": {"requests": 0}}
        with patch.object(experiment.sys, "argv", ["eight-arm", "run", "--output", str(output),
                                                  "--prepared", str(prepared), "--policy", str(selected),
                                                  "--execute-real-model"]), \
                patch.object(experiment, "execute", return_value=report) as execute, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(experiment.main(), 0)
        execute.assert_called_once_with(str(prepared), str(output), "llm", policy_path=selected,
                                        execute_real_model=True)

    def test_cli_errors_publish_only_fixed_reason_codes_not_credential_or_exception_text(self):
        secret = "never-print-this-provider-key"
        output = self.root / "safe-cli-output"
        for error, code in ((experiment.ExperimentCheckError("process_model_credential_missing", secret),
                             "process_model_credential_missing"),
                            (RuntimeError(secret), "preparation_or_execution_failed")):
            stream = io.StringIO()
            with self.subTest(code=code), \
                    patch.object(experiment.sys, "argv", ["eight-arm", "prepare", "--output", str(output)]), \
                    patch.object(experiment, "prepare", side_effect=error), \
                    redirect_stdout(stream):
                self.assertEqual(experiment.main(), 1)
            self.assertNotIn(secret, stream.getvalue())
            self.assertEqual(json.loads(stream.getvalue())["reason_code"], code)

    def test_model_credential_missing_is_detected_without_output_or_model_call(self):
        prepared = self.prepare()
        output = self.root / "missing-model-secret"
        budget = {**self.budget, "requests_remaining": 144,
                  "max_requests": self.budget["requests_made"] + 144}
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial, \
                patch.object(experiment, "readonly_budget", return_value=budget), \
                patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(experiment.ExperimentCheckError) as error:
                self.execute(prepared, output, "llm", execute_real_model=True)
        self.assertEqual(error.exception.reason_code, "process_model_credential_missing")
        model.assert_not_called()
        trial.assert_not_called()
        self.assertFalse(output.exists())

    def test_llm_reads_credential_only_from_environment_and_clears_runtime_config_after_collection(self):
        prepared = self.prepare()
        output = self.root / "fake-llm-no-network"
        secret = "test-process-only-credential-no-provider-request"
        budget = {**self.budget, "requests_remaining": 144,
                  "max_requests": self.budget["requests_made"] + 144}
        runtime_configs = []

        def fake_study(config, options, on_trial, on_checkpoint):
            self.assertEqual(config.model_config["api_key"], secret)
            self.assertEqual(config.model_config["base_url"], "https://api.deepseek.com")
            self.assertEqual(experiment.os.environ["ZHUIYIN_CAMPAIGN_POLICY"], str(experiment.POLICY.resolve()))
            runtime_configs.append(config)
            report = {"mode": "llm", "status": "finished", "completed": 0,
                      "model_usage": {"requests": 0}}
            on_checkpoint(report)
            return report

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), \
                patch.object(experiment, "readonly_budget", return_value=budget), \
                patch.object(experiment.study, "run_study", side_effect=fake_study), \
                patch.object(engine, "model_request") as model:
            report = self.execute(prepared, output, "llm", execute_real_model=True)
        model.assert_not_called()
        self.assertEqual(report["model_usage"]["requests"], 0)
        self.assertEqual(len(runtime_configs), 1)
        self.assertEqual(runtime_configs[0].model_config, {})
        for path in output.rglob("*.json"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    def test_llm_requires_explicit_switch_even_with_capacity_and_creates_no_output(self):
        prepared = self.prepare()
        output = self.root / "blocked-switch"
        budget = {**self.budget, "requests_remaining": 144,
                  "max_requests": self.budget["requests_made"] + 144}
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial, \
                patch.object(experiment, "readonly_budget", return_value=budget):
            with self.assertRaisesRegex(ValueError, "显式执行开关"):
                self.execute(prepared, output, "llm")
        model.assert_not_called()
        trial.assert_not_called()
        self.assertFalse(output.exists())

    def test_llm_insufficient_capacity_is_blocked_before_any_trial_or_output(self):
        prepared = self.prepare()
        output = self.root / "blocked-budget"
        budget = {**self.budget, "requests_remaining": 143,
                  "max_requests": self.budget["requests_made"] + 143}
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial, \
                patch.object(experiment, "readonly_budget", return_value=budget):
            with self.assertRaisesRegex(ValueError, "预算不足"):
                self.execute(prepared, output, "llm", execute_real_model=True)
        model.assert_not_called()
        trial.assert_not_called()
        self.assertFalse(output.exists())

    def test_llm_changed_campaign_policy_is_rejected_before_model_or_output(self):
        prepared = self.prepare()
        output = self.root / "changed-policy"
        policy_path = self.root / "different-policy.json"
        ledger = self.root / "different-ledger.sqlite"
        raw = self.read_json(experiment.POLICY)
        raw.update(ledger_path=str(ledger), input_price_per_million="4")
        experiment.write_json(policy_path, raw)
        # Initialize a fresh real guard without reserving or sending a request.
        with experiment.campaign_environment(policy_path):
            summary = experiment.model_budget.public_summary()
        self.assertEqual(summary["requests_made"], 0)
        budget = experiment.readonly_budget(policy_path)
        self.assertTrue(budget["policy_consistent"])
        self.assertGreaterEqual(budget["requests_remaining"], 144)
        self.assertNotEqual(self.design["expected_campaign_policy_sha256"], budget["policy_sha256"])
        ledger_before = ledger.read_bytes()
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial:
            with self.assertRaisesRegex(ValueError, "预算政策与准备时不同"):
                self.execute(prepared, output, "llm", policy_path=policy_path, execute_real_model=True)
        model.assert_not_called()
        trial.assert_not_called()
        self.assertFalse(output.exists())
        self.assertEqual(ledger.read_bytes(), ledger_before)

    def test_changed_design_is_rejected_even_if_its_local_lock_was_recomputed(self):
        prepared = self.prepare()
        changed = self.read_json(prepared / "fixed-design.json")
        changed["config"]["seed"] += 1
        experiment.write_json(prepared / "fixed-design.json", changed)
        experiment.write_json(prepared / "design-sha256.json",
                              {"sha256": hashlib.sha256(experiment.canonical(changed)).hexdigest()})
        output = self.root / "tampered-design"
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial:
            with self.assertRaisesRegex(ValueError, "设计或源码已变化"):
                self.execute(prepared, output)
        model.assert_not_called()
        trial.assert_not_called()
        self.assertFalse(output.exists())

    def test_rules_rehearsal_retains_all_sixteen_actual_trial_databases_and_empty_request_records(self):
        prepared = self.prepare()
        output = self.root / "rules-rehearsal"
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial", wraps=engine.run_trial) as execute:
            report = self.execute(prepared, output)
        model.assert_not_called()
        self.assertEqual(execute.call_count, 16)
        self.assertEqual((report["mode"], report["status"], report["completed"]), ("rules", "finished", 16))
        self.assertEqual(report["model_usage"]["requests"], 0)
        self.assertFalse(report["model_usage"]["external_model_verified"])
        self.assertEqual(len(report["arms"]), 8)
        index = self.read_json(output / "trial-index.json")
        self.assertEqual(index["evidence_mode"], "rules_rehearsal_not_model_evidence")
        self.assertEqual(len(index["trials"]), 16)
        self.assertEqual(len(list((output / "trials").glob("*.json"))), 16)
        self.assertEqual(len(list((output / "trials").glob("*.sqlite"))), 16)
        for entry, sample in zip(index["trials"], report["samples"]):
            with self.subTest(execution_index=entry["execution_index"]):
                record = self.read_json(output / entry["json"])
                self.assertEqual(record["evidence_mode"], "rules_rehearsal_not_model_evidence")
                self.assertEqual(record["sample"], sample)
                self.assertEqual(record["request_records"], [])
                self.assertEqual(record["sample"]["model_usage"]["requests"], 0)
                self.assertEqual(record["result"]["events"], sample["events"])
                self.assertTrue(record["result"]["sqlite_dump"])
                self.assertEqual(entry["arm_id"], sample["arm_id"])
                self.assertEqual(entry["database_status"], "matched_fresh_trial_records")
                database_path = output / entry["database"]
                with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as database:
                    database.row_factory = sqlite3.Row
                    rows = [dict(row) for row in database.execute("SELECT * FROM work_orders ORDER BY id")]
                self.assertEqual(rows, sample["business_records"])
                self.assertEqual(rows, record["result"]["business_records"])
        state = self.read_json(output / "run-state.json")
        self.assertEqual(state["new_model_requests"], 0)
        self.assertEqual((state["trials_indexed"], state["trial_json_files_retained"],
                          state["trial_databases_retained"]), (16, 16, 16))
        for name, checksum in self.read_json(output / "file-manifest.json").items():
            self.assertEqual(experiment.digest(output / name), checksum)

    def test_second_trial_database_write_failure_stops_without_retry_and_preserves_prior_evidence(self):
        prepared = self.prepare()
        output = self.root / "interrupted-collection"
        connect = sqlite3.connect

        def fail_second_export(database, *args, **kwargs):
            if Path(str(database)) == output / "trials" / "trial-02.sqlite":
                # A failed export may leave a database file. Its existence must
                # not promote it into the index of matched complete evidence.
                connect(database, *args, **kwargs).close()
                raise OSError("simulated-export-write-failure")
            return connect(database, *args, **kwargs)

        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial", wraps=engine.run_trial) as execute, \
                patch.object(experiment.sqlite3, "connect", side_effect=fail_second_export):
            with self.assertRaisesRegex(OSError, "simulated-export-write-failure"):
                self.execute(prepared, output)
        model.assert_not_called()
        self.assertEqual(execute.call_count, 2)
        state = self.read_json(output / "run-state.json")
        self.assertEqual((state["mode"], state["status"], state["trials_indexed"]),
                         ("rules", "interrupted", 1))
        self.assertEqual((state["trial_json_files_retained"], state["trial_databases_retained"]), (2, 2))
        self.assertEqual(state["reason"], "execution_or_collection_interrupted")
        self.assertTrue((output / "trials" / "trial-01.json").is_file())
        self.assertTrue((output / "trials" / "trial-01.sqlite").is_file())
        self.assertTrue((output / "trials" / "trial-02.json").is_file())
        self.assertFalse((output / "trials" / "trial-03.json").exists())
        self.assertEqual(self.read_json(output / "study-report.json")["completed"], 1)
        self.assertEqual(len(self.read_json(output / "trial-index.json")["trials"]), 1)
        self.assertEqual(self.read_json(output / "trials" / "trial-02.json")["sample"]["execution_index"], 2)
        with closing(connect(output / "trials" / "trial-02.sqlite")) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        self.assertFalse((output / "file-manifest.json").exists())

    def test_archive_splits_original_request_records_by_trial_without_mixing_arms(self):
        output = self.root / "usage-slices"
        output.mkdir()
        archive = experiment.TrialArchive(output, "test_fixture_not_model_evidence")
        first = {"status": "ok", "prompt_tokens": 11, "completion_tokens": 1,
                 "usage_available": True}
        second = {"status": "ok", "prompt_tokens": 22, "completion_tokens": 2,
                  "usage_available": True}
        third = {"status": "network_error", "prompt_tokens": None,
                 "completion_tokens": None, "usage_available": False}
        with patch.object(engine.MODEL_CONTEXT, "usage_records", [first], create=True):
            archive.observe({"execution_index": 1, "arm_id": "neutral:baseline", "trial_index": 1,
                             "status": "valid", "model_usage": {"requests": 1}}, {"events": []})
            engine.MODEL_CONTEXT.usage_records.extend([second, third])
            archive.observe({"execution_index": 2, "arm_id": "recovery:verified", "trial_index": 1,
                             "status": "infra_error", "model_usage": {"requests": 2}}, {"events": []})
        first_record = self.read_json(output / "trials" / "trial-01.json")
        second_record = self.read_json(output / "trials" / "trial-02.json")
        self.assertEqual(first_record["request_records"], [first])
        self.assertEqual(second_record["request_records"], [second, third])
        self.assertEqual(first_record["sample"]["arm_id"], "neutral:baseline")
        self.assertEqual(second_record["sample"]["arm_id"], "recovery:verified")
        self.assertEqual([entry["database_status"] for entry in archive.entries],
                         ["not_available", "not_available"])

    def test_existing_output_directories_are_never_overwritten(self):
        prepared = self.prepare()
        output = self.root / "existing-output"
        output.mkdir()
        sentinel = output / "preserve.txt"
        sentinel.write_bytes(b"existing evidence stays intact")
        with patch.object(engine, "model_request") as model, \
                patch.object(engine, "run_trial") as trial:
            with self.assertRaises(FileExistsError):
                self.execute(prepared, output)
            with self.assertRaises(FileExistsError):
                experiment.prepare(output)
        model.assert_not_called()
        trial.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"existing evidence stays intact")
        self.assertEqual(list(output.iterdir()), [sentinel])


if __name__ == "__main__":
    unittest.main()
