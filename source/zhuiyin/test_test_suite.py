"""Batch workflow tests using unrelated, explicitly synthetic inputs only."""
import copy
from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import engine
import test_suite as suite


def scenario(number=1, **changes):
    config = {"equipment": "SYNTHETIC TEST CAMERA", "stock": 5, "quantity": 1,
              "request_id": f"SYNTHETIC-BATCH-{number}", "seed": 7, "faults": []}
    config.update(changes)
    return {"kind": "scenario", "name": f"SYNTHETIC case {number}", "config": config}


def upload(value, filename="SYNTHETIC.json"):
    return {"filename": filename, "content": json.dumps(value, ensure_ascii=False)}


def trace(number=1, **changes):
    value = {"kind": "trace", "name": f"SYNTHETIC TRACE {number}",
        "evaluation_protocol": "business-report-v2", "task": {"request_id": "SYNTHETIC-SHARED-LOG-ID"},
        "events": [{"step": 1, "tool": "SYNTHETIC_WRITE", "actual": {"committed": True},
                    "observed": {"ok": True}}],
        "final_state": {"effects": [{"effect_id": f"SYNTHETIC-EFFECT-{number}",
            "request_id": "SYNTHETIC-SHARED-LOG-ID", "status": "active"}]}}
    value.update(changes)
    return value


def archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as handle:
        for name, content in entries:
            handle.writestr(name, content)
    return {"filename": "SYNTHETIC.zip", "data": stream.getvalue()}


class BatchIntakeTests(unittest.TestCase):
    def test_preview_does_not_execute_and_partial_errors_preserved(self):
        value = {"kind": "scenarios", "scenarios": [scenario(1), scenario(2, quantity=-1), scenario(3)]}
        with patch.object(suite, "run_trial", side_effect=AssertionError("preview executed")), \
             patch.object(suite, "run_experiment", side_effect=AssertionError("preview executed")):
            plan = suite.prepare_uploads([upload(value), {"filename": "bad.json", "content": "{"}])
        self.assertEqual(plan["counts"]["ready"], 2)
        self.assertEqual(plan["counts"]["import_errors"], 2)
        self.assertEqual(plan["counts"]["file_errors"], 2)
        self.assertFalse(any("strategies" in case for case in plan["cases"]))

    def test_single_json_and_csv_more_than_twenty_rows(self):
        cases = [scenario(index) for index in range(1, 26)]
        plan = suite.prepare_uploads([upload({"kind": "scenarios", "scenarios": cases})])
        self.assertEqual(plan["counts"]["ready"], 25)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["equipment", "stock", "quantity", "request_id", "seed", "faults"])
        for index in range(26, 51):
            writer.writerow(["SYNTHETIC CAMERA", 5, 1, f"SYNTHETIC-{index}", 7, ""])
        writer.writerow(["SYNTHETIC BAD", 5, "not a number", "SYNTHETIC-BAD", 7, ""])
        plan = suite.prepare_uploads([{"filename": "SYNTHETIC.csv", "content": output.getvalue()}])
        self.assertEqual(plan["counts"]["ready"], 25)
        self.assertEqual(plan["counts"]["import_errors"], 1)

    def test_semantic_dedup_across_formats_and_id_conflicts_all_skip(self):
        item = scenario(1, faults=["stale_read", "commit_response_lost"])
        csv_text = "equipment,stock,quantity,request_id,seed,faults\nSYNTHETIC TEST CAMERA,5,1,SYNTHETIC-BATCH-1,7,commit_response_lost;stale_read\n"
        plan = suite.prepare_uploads([upload(item), {"filename": "SYNTHETIC.csv", "content": csv_text}])
        self.assertEqual([c["status"] for c in plan["cases"]], ["ready", "duplicate"])
        plan = suite.prepare_uploads([upload(scenario(1)), upload(scenario(1, quantity=2), "SYNTHETIC-OTHER.json")])
        self.assertEqual([c["status"] for c in plan["cases"]], ["conflict", "conflict"])

    def test_logs_hash_duplicates_skip_distinct_same_id_kept(self):
        one = upload(trace(1))
        plan = suite.prepare_uploads([one, one, upload(trace(2))])
        self.assertEqual([c["status"] for c in plan["cases"]], ["ready", "duplicate", "ready"])

    def test_zip_prefers_inputs_never_reads_answers_and_rejects_paths(self):
        good = json.dumps(scenario(1))
        item = archive([("bundle/inputs/good.json", good), ("bundle/answers/secret.json", "NOT JSON"),
                        ("bundle/metadata.json", "NOT JSON"), ("elsewhere.json", "NOT JSON"),
                        ("../escape.json", good), ("/absolute.json", good)])
        original = suite.parse_upload
        seen = []
        def checked(name, content):
            seen.append(content)
            return original(name, content)
        with patch.object(suite, "parse_upload", side_effect=checked):
            plan = suite.prepare_uploads([item])
        self.assertEqual(len(seen), 1)
        self.assertEqual(plan["counts"]["ready"], 1)
        self.assertEqual(plan["counts"]["ignored_files"], 3)
        self.assertEqual(plan["counts"]["import_errors"], 2)

    def test_zip_duplicate_path_symlink_encryption_and_size_guards(self):
        info = zipfile.ZipInfo("link.json")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        item = archive([("same.json", json.dumps(scenario(1))), ("SAME.json", json.dumps(scenario(2))),
                        (info, "target.json"), ("good.json", json.dumps(scenario(3)))])
        plan = suite.prepare_uploads([item])
        self.assertEqual(plan["counts"]["ready"], 1)
        self.assertEqual(plan["counts"]["import_errors"], 3)
        # ZIP encryption flag exists in both headers. The body is never opened.
        raw = bytearray(archive([("encrypted.json", "{}")])["data"])
        first = raw.find(b"PK\x03\x04")
        central = raw.find(b"PK\x01\x02")
        raw[first + 6] |= 1
        raw[central + 8] |= 1
        plan = suite.prepare_uploads([{"filename": "SYNTHETIC.zip", "data": bytes(raw)}])
        self.assertIn("加密", plan["cases"][0]["error"])
        with self.assertRaisesRegex(ValueError, "20 MiB"):
            suite.prepare_uploads([{"filename": "SYNTHETIC.zip", "data": b"x" * (suite.MAX_UPLOAD_BYTES + 1)}])
        with self.assertRaisesRegex(ValueError, "500"):
            suite.prepare_uploads([upload({"kind": "scenarios", "scenarios": [scenario(i) for i in range(501)]})])

    def test_secret_and_uploaded_sql_scripts_not_durable(self):
        data = trace()
        data["events"][0]["observed"].update({"API-Key": "SYNTHETIC-SECRET", "script": "SYNTHETIC-SCRIPT", "sql_dump": "SYNTHETIC-SQL"})
        plan = suite.prepare_uploads([upload(data)])
        rendered = json.dumps(plan)
        for secret in ("SYNTHETIC-SECRET", "SYNTHETIC-SCRIPT", "SYNTHETIC-SQL"):
            self.assertNotIn(secret, rendered)
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory:
            suite_id = suite.save_prepared(directory, plan)
            saved = suite.load_prepared(directory, suite_id)
            self.assertEqual(saved["cases"][0]["source_sha256"], plan["cases"][0]["source_sha256"])
            self.assertEqual(saved["suite_id"], suite_id)


class BatchExecutionTests(unittest.TestCase):
    def test_quick_is_exact_four_fresh_rules_arms_and_real_sqlite_exports(self):
        plan = suite.prepare_uploads([upload(scenario(1, faults=["commit_response_lost", "stale_read"]))])
        seen, callbacks = [], []
        def runner(config, faults, strategy, mode):
            self.assertEqual(mode, "rules")
            self.assertEqual(config.agent_mode, "rules")
            self.assertFalse(config.model_config.get("api_key"))
            seen.append((config, strategy))
            return engine.run_trial(config, faults, strategy, mode)
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory, \
             patch.object(suite, "run_experiment", side_effect=AssertionError("full stage in quick")), \
             patch.object(engine, "model_request", side_effect=AssertionError("network forbidden")):
            report = suite.execute_suite(plan, directory, callbacks.append, quick_runner=runner)
            self.assertEqual([strategy for _, strategy in seen], list(engine.STRATEGIES))
            self.assertEqual(len({id(config) for config, _ in seen}), 4)
            self.assertEqual(report["evaluation"]["execution_valid_trials"], 4)
            self.assertNotIn("items", report)
            self.assertNotIn("events", report["cases"][0]["strategies"]["baseline"])
            self.assertTrue((Path(directory) / "suite-export.zip").is_file())
            self.assertEqual(callbacks[-1]["status"], "completed")
            case = report["cases"][0]
            db_path = Path(directory) / case["artifacts"]["baseline"]["database"]
            with closing(sqlite3.connect(db_path)) as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 2)
            with zipfile.ZipFile(Path(directory) / "suite-export.zip") as handle:
                self.assertIn("summary.csv", handle.namelist())
                self.assertIn("cases/case-0001/baseline.sqlite", handle.namelist())
                content = json.loads(handle.read("cases/case-0001/baseline.json"))
                self.assertIn("events", content)
                self.assertNotIn("sqlite_dump", content)

    def test_full_calls_original_experiment_once_with_identical_rules_config(self):
        plan = suite.prepare_uploads([upload(scenario(1))], profile="full")
        seen = []
        def runner(config, cancel_check=None):
            seen.append(config.public())
            return engine.run_experiment(config, cancel_check=cancel_check)
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory, \
             patch.object(engine, "model_request", side_effect=AssertionError("network forbidden")):
            report = suite.execute_suite(plan, directory, full_runner=runner)
            self.assertEqual(seen, [plan["cases"][0]["config"]])
            self.assertEqual(report["cases"][0]["status"], "completed")
            full = json.loads((Path(directory) / "cases/case-0001/full-report.json").read_text(encoding="utf-8"))
            self.assertIn("minimization", full)
            self.assertEqual(len(full["ablation"]["arms"]), 4)

    def test_stop_during_trial_retains_completed_arm_and_unrun_positions(self):
        plan = suite.prepare_uploads([upload({"kind": "scenarios", "scenarios": [scenario(1), scenario(2)]})])
        stopped = False
        def runner(config, faults, strategy, mode):
            nonlocal stopped
            result = engine.run_trial(config, faults, strategy, mode)
            stopped = True
            return result
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory:
            report = suite.execute_suite(plan, directory, should_stop=lambda: stopped, quick_runner=runner)
            self.assertTrue(report["cancelled"])
            self.assertEqual(report["status"], "cancelled")
            self.assertTrue(report["cases"][0]["strategies"]["baseline"]["completed"])
            self.assertEqual(report["cases"][0]["strategies"]["verified"]["status"], "not_run")
            self.assertEqual(report["cases"][1]["status"], "not_run")
            self.assertEqual(report["evaluation"]["execution_valid_trials"], 1)
            self.assertEqual(report["evaluation"]["attempted_trials"], 1)
            self.assertEqual(report["evaluation"]["execution_invalid_trials"], 0)
            self.assertEqual(report["evaluation"]["not_run_trials"], 7)
            self.assertTrue((Path(directory) / "suite-export.zip").is_file())

    def test_checkpoint_recovery_only_exports_saved_data_and_marks_missing_arms(self):
        plan = suite.prepare_uploads([upload({"kind": "scenarios", "scenarios": [scenario(1), scenario(2)]})])
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory:
            def fail_after_first_arm(report):
                if report["cases"][0].get("strategies", {}).get("baseline"):
                    raise RuntimeError("SYNTHETIC HARD INTERRUPTION")
            with self.assertRaisesRegex(RuntimeError, "HARD INTERRUPTION"):
                suite.execute_suite(plan, directory, progress_callback=fail_after_first_arm)
            checkpoint = json.loads((Path(directory) / "suite-report.json").read_text(encoding="utf-8"))
            self.assertFalse((Path(directory) / "suite-export.zip").exists())
            with patch.object(suite, "run_trial", side_effect=AssertionError("recovery ran engine")), \
                 patch.object(suite, "run_experiment", side_effect=AssertionError("recovery ran engine")), \
                 patch.object(suite, "parse_upload", side_effect=AssertionError("recovery parsed business")), \
                 patch.object(suite.sqlite3, "connect", side_effect=AssertionError("recovery executed SQL")):
                recovered = suite.recover_suite(checkpoint, directory)
            self.assertEqual(recovered["status"], "interrupted")
            self.assertFalse(recovered["completed"])
            self.assertEqual(recovered["evaluation"]["attempted_trials"], 1)
            self.assertEqual(recovered["evaluation"]["execution_valid_trials"], 1)
            self.assertEqual(recovered["evaluation"]["not_run_trials"], 7)
            self.assertEqual(recovered["cases"][0]["status"], "partial")
            self.assertEqual(recovered["cases"][1]["status"], "not_run")
            self.assertTrue((Path(directory) / "suite-export.zip").is_file())

    def test_one_bad_trial_continues_other_arms_and_next_case(self):
        plan = suite.prepare_uploads([upload({"kind": "scenarios", "scenarios": [scenario(1), scenario(2)]})])
        calls = []
        def runner(config, faults, strategy, mode):
            calls.append((config.request_id, strategy))
            if config.request_id.endswith("-1") and strategy == "baseline":
                raise RuntimeError("SYNTHETIC EXECUTION ERROR")
            return engine.run_trial(config, faults, strategy, mode)
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory:
            report = suite.execute_suite(plan, directory, quick_runner=runner)
        self.assertEqual(len(calls), 8)
        self.assertEqual([c["status"] for c in report["cases"]], ["execution_error", "completed"])
        self.assertEqual(report["evaluation"]["execution_valid_trials"], 7)

    def test_unknown_outcome_is_valid_without_inventing_actual_success(self):
        plan = suite.prepare_uploads([upload(scenario())])
        def runner(config, faults, strategy, mode):
            return {"strategy": strategy, "evaluation_protocol": "business-report-v2", "completed": True,
                    "reported_outcome": "unknown", "reported_success": None, "actual_success": None,
                    "report_assessment": "uncertain", "business_violations": [{"code": "SYNTHETIC-BUSINESS"}],
                    "reporting_violations": [], "execution_violations": []}
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory:
            report = suite.execute_suite(plan, directory, quick_runner=runner)
        self.assertEqual(report["evaluation"]["execution_valid_trials"], 4)
        self.assertEqual(report["evaluation"]["db_actual_success"], 0)
        self.assertEqual(report["evaluation"]["db_actual_unknown"], 4)
        self.assertEqual(report["evaluation"]["business_violation_trials"], 4)

    def test_static_log_never_executes_uploaded_sql_and_export_is_sanitized(self):
        data = trace()
        data["sql_dump"] = "CREATE TABLE SYNTHETIC_EVIL (x);"
        data["events"][0]["observed"]["api_key"] = "SYNTHETIC-PRIVATE"
        plan = suite.prepare_uploads([upload(data)])
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory, \
             patch.object(suite, "run_trial", side_effect=AssertionError("static import executed")), \
             patch.object(suite.sqlite3, "connect", side_effect=AssertionError("uploaded SQL executed")):
            report = suite.execute_suite(plan, directory)
            self.assertEqual(report["evaluation"]["attempted_trials"], 0)
            self.assertNotIn("events", report["cases"][0]["analysis"])
            with zipfile.ZipFile(Path(directory) / "suite-export.zip") as handle:
                self.assertFalse(any(name.endswith(".sqlite") for name in handle.namelist()))
                self.assertIn("cases/case-0001/analysis.json", handle.namelist())
                all_text = b"".join(handle.read(name) for name in handle.namelist())
                self.assertNotIn(b"SYNTHETIC_EVIL", all_text)
                self.assertNotIn(b"SYNTHETIC-PRIVATE", all_text)

    def test_long_csv_rows_formula_escape_and_duplicates_not_valid(self):
        item = scenario(1)
        item["name"] = " =SYNTHETIC_FORMULA()"
        file = upload(item, "@SYNTHETIC.json")
        plan = suite.prepare_uploads([file, file])
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory:
            report = suite.execute_suite(plan, directory)
            raw = (Path(directory) / "summary.csv").read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 8)
            self.assertTrue(rows[0]["name"].startswith("'="))
            self.assertEqual(rows[0]["filename"], "'@SYNTHETIC.json")
            self.assertTrue(all(row["execution_valid"] == "False" for row in rows[4:]))
            self.assertEqual(report["evaluation"]["execution_valid_trials"], 4)

    def test_invalid_only_plan_can_export_without_runtime(self):
        plan = suite.prepare_uploads([{"filename": "SYNTHETIC-invalid.json", "content": "{"}])
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-batch-") as directory, \
             patch.object(suite, "run_trial", side_effect=AssertionError("invalid input ran")):
            report = suite.execute_suite(plan, directory)
            self.assertEqual(report["evaluation"]["attempted_trials"], 0)
            self.assertEqual(report["counts"]["import_errors"], 1)
            self.assertTrue((Path(directory) / "suite-export.zip").is_file())


if __name__ == "__main__":
    unittest.main()
