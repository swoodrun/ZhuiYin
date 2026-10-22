"""Status transport and checkpoint tests with synthetic large reports only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from job_store import JobStore, compact_snapshot
import server


def large_report():
    return {"kind": "batch", "completed": 20, "cases": [
        {"result": {"baseline": {"events": [{"step": j, "observed": "synthetic-event" * 20} for j in range(50)],
                                  "sqlite_dump": "-- synthetic SQL\n" * 2000}}}
        for _ in range(20)]}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-transport-", dir=Path(__file__).resolve().parent)
        self.data = Path(self.temp.name)
        self.patches = [patch.object(server, "DATA", self.data), patch.object(server, "JOBS", {}), patch.object(server, "STOP_EVENTS", {})]
        for replacement in self.patches:
            replacement.start()
        self.store = server.store()

    def tearDown(self):
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temp.cleanup()

    def get(self, path):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {"Host": "127.0.0.1"}
        handler.path = path
        handler.server = SimpleNamespace(server_port=8765)
        handler.json = Mock()
        handler.send_bytes = Mock()
        handler.do_GET()
        return handler

    def publish(self, report=None):
        run_id = server.create_job("batch")
        server.JOBS[run_id].update(partial_result=report or large_report(), progress=50)
        server.JOBS[run_id]["events"] = [{"message": f"status {i}"} for i in range(100)]
        server.checkpoint(run_id)
        return run_id

    def test_lite_response_omits_payload_and_bounds_status_events(self):
        run_id = self.publish()
        status, lite = self.get(f"/api/runs/{run_id}?lite=1").json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(lite["has_partial_result"])
        self.assertFalse(lite["has_result"])
        self.assertNotIn("partial_result", lite)
        self.assertNotIn("result", lite)
        self.assertEqual(len(lite["events"]), 20)
        full = self.get(f"/api/runs/{run_id}").json.call_args.args[1]
        self.assertEqual(full["partial_result"]["completed"], 20)
        self.assertLess(len(json.dumps(lite)), len(json.dumps(full)) / 100)

    def test_lite_never_traverses_or_copies_result(self):
        run_id = server.create_job()
        class OpaqueResult(dict):
            def items(self):
                raise AssertionError("status polling traversed the result")
            def __deepcopy__(self, memo):
                raise AssertionError("status polling copied the result")
        server.JOBS[run_id]["result"] = OpaqueResult(value="present")
        status, lite = self.get(f"/api/runs/{run_id}?lite=1").json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(lite["has_result"])

    def test_lite_validates_query_and_preserves_full_default(self):
        run_id = self.publish()
        for query in ("lite=", "lite=true", "lite=2"):
            self.assertEqual(self.get(f"/api/runs/{run_id}?{query}").json.call_args.args[0], 400)
        self.assertEqual(self.get(f"/api/runs/{run_id}?lite=0").json.call_args.args,
                         self.get(f"/api/runs/{run_id}").json.call_args.args)

    def test_progress_checkpoint_does_not_serialize_or_rewrite_result(self):
        run_id = self.publish()
        def raw():
            with closing(self.store.connect()) as database:
                return database.execute("SELECT snapshot FROM jobs WHERE run_id=?", (run_id,)).fetchone()[0]
        before = raw()
        revision = server.JOBS[run_id]["result_revision"]
        server.JOBS[run_id].update(phase="later", progress=73)
        server.JOBS[run_id]["events"].append({"message": "new progress"})
        with patch("job_store.compact_snapshot", side_effect=AssertionError("progress serialized payload")):
            server.checkpoint(run_id, payload_changed=False)
        self.assertEqual(raw(), before)
        saved = self.store.get(run_id)
        self.assertEqual(saved["progress"], 73)
        self.assertEqual(saved["phase"], "later")
        self.assertEqual(saved["events"][-1]["message"], "new progress")
        self.assertEqual(saved["result_revision"], revision)
        self.assertEqual(saved["partial_result"]["completed"], 20)

    def test_progress_updates_a_separate_status_table_not_large_snapshot_rows(self):
        run_id = self.publish()
        statements = []
        connect = self.store.connect
        def observed_connection():
            database = connect()
            database.set_trace_callback(statements.append)
            return database
        server.JOBS[run_id]["progress"] = 72
        with patch.object(self.store, "connect", side_effect=observed_connection):
            server.checkpoint(run_id, payload_changed=False)
        mutations = [sql.lower() for sql in statements if sql.lstrip().lower().startswith(("insert", "update", "delete", "replace"))]
        self.assertTrue(mutations)
        self.assertTrue(all("job_status" in sql for sql in mutations))
        self.assertEqual(self.store.get(run_id, lite=True)["progress"], 72)

    def test_sql_is_not_persisted_but_business_evidence_survives(self):
        run_id = self.publish()
        saved = self.store.get(run_id)
        self.assertNotIn("sqlite_dump", json.dumps(saved))
        trial = saved["partial_result"]["cases"][0]["result"]["baseline"]
        self.assertEqual(len(trial["events"]), 50)
        # The caller-owned original remains available to full reads/downloads.
        self.assertIn("sqlite_dump", server.JOBS[run_id]["partial_result"]["cases"][0]["result"]["baseline"])

    def test_durable_lite_and_history_read_only_small_metadata(self):
        run_id = self.publish()
        server.JOBS.clear()
        original = json.loads
        lengths = []
        def observed(value, *args, **kwargs):
            lengths.append(len(value))
            return original(value, *args, **kwargs)
        with patch("job_store.json.loads", side_effect=observed):
            lite = self.store.get(run_id, lite=True)
            history = self.store.list(10)
        self.assertTrue(lite["has_partial_result"])
        self.assertEqual(history["total"], 1)
        self.assertTrue(lengths)
        self.assertLess(max(lengths), 5000)

    def test_restart_keeps_checkpoint_and_recovers_full_report_without_sql_execution(self):
        run_id = self.publish({"kind": "study", "completed": 2, "samples": [{"trial_index": 1}]})
        self.assertEqual(self.store.recover_interrupted(), 1)
        server.JOBS.clear()
        restored = server.get_job(run_id)
        self.assertEqual(restored["status"], "interrupted")
        self.assertEqual(restored["partial_result"]["completed"], 2)
        out = self.data / run_id
        out.mkdir()
        report = {"kind": "study", "completed": 2, "sql_dump": "CREATE TABLE NEVER_EXECUTE(value);",
                  "key": "SYNTHETIC_HIDDEN_VALUE", "model_config": {"api_key": "SYNTHETIC_HIDDEN_VALUE"}}
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        restored = server.get_job(run_id)
        self.assertNotIn("SYNTHETIC_HIDDEN_VALUE", json.dumps(restored))
        self.assertEqual(restored["partial_result"]["sql_dump"], report["sql_dump"])
        with closing(self.store.connect()) as database:
            self.assertIsNone(database.execute("SELECT name FROM sqlite_master WHERE name='NEVER_EXECUTE'").fetchone())

    def test_skipped_fixed_trial_does_not_export_a_fake_database(self):
        out = self.data / "skipped"
        out.mkdir()
        result = {"baseline": {"sqlite_dump": "CREATE TABLE real_baseline(value);"},
                  "fixed": {"skipped": True, "failed": None}, "ablation": {"arms": []}}
        server.save_databases(result, out, "skipped")
        self.assertTrue((out / "baseline.sqlite").is_file())
        self.assertFalse((out / "fixed.sqlite").exists())
        self.assertEqual(set(result["artifacts"]), {"baseline"})

    def test_actual_partial_model_worker_preserves_report_and_only_real_databases(self):
        from test_experiment_budget import SyntheticModel, config
        cfg = config(max_requests=7)
        run_id = server.create_job(config=cfg.public())
        with patch("engine.urllib.request.build_opener", return_value=SyntheticModel()):
            server.worker(run_id, cfg)
        job = server.get_job(run_id)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["result"]["status"], "partial")
        self.assertTrue(job["result"]["fixed"]["skipped"])
        self.assertIsNone(job["result"]["fixed"]["failed"])
        self.assertTrue((self.data / run_id / "baseline.sqlite").is_file())
        self.assertFalse((self.data / run_id / "fixed.sqlite").exists())
        self.assertEqual(cfg.model_config, {})
        server.JOBS.clear()
        restored = server.get_job(run_id)
        self.assertEqual(restored["status"], "error")
        self.assertEqual(restored["result"]["model_usage"]["requests"], 5)
        exported = self.get(f"/api/runs/{run_id}/export")
        self.assertEqual(exported.send_bytes.call_args.args[0], 200)
        self.assertEqual(json.loads(exported.send_bytes.call_args.args[1])["status"], "partial")

    def test_worker_cancel_after_valid_baseline_keeps_that_evidence(self):
        import engine
        cfg = engine.Config()
        run_id = server.create_job(config=cfg.public())
        original = server.run_experiment
        def execute(config, update, cancel_check):
            def signal(phase, progress, message):
                if phase == "minimizing":
                    server.STOP_EVENTS[run_id].set()
                update(phase, progress, message)
            return original(config, signal, cancel_check)
        with patch.object(server, "run_experiment", side_effect=execute):
            server.worker(run_id, cfg)
        job = server.get_job(run_id)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["result"]["stopped_reason"], "cancelled")
        self.assertTrue(job["result"]["baseline"]["failed"])
        self.assertTrue(job["result"]["fixed"]["skipped"])
        self.assertTrue((self.data / run_id / "baseline.sqlite").is_file())

    def test_old_schema_migration_builds_metadata_and_compacts_only_once(self):
        path = self.data / "old-history.sqlite"
        payload = {"run_id": "old", "kind": "experiment", "status": "done", "events": [],
                   "result": {"baseline": {"sqlite_dump": "ignored SQL", "business_records": [{"id": 1}]}}}
        with closing(sqlite3.connect(path)) as database:
            database.execute("CREATE TABLE jobs(run_id TEXT PRIMARY KEY,kind TEXT,status TEXT,created_at TEXT,updated_at TEXT,snapshot TEXT)")
            database.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", ("old", "experiment", "done", "2026-01-01", "2026-01-01", json.dumps(payload)))
            database.commit()
        upgraded = JobStore(path)
        self.assertTrue(upgraded.get("old", lite=True)["has_result"])
        self.assertEqual(upgraded.get("old")["result"]["baseline"]["business_records"], [{"id": 1}])
        self.assertNotIn("sqlite_dump", json.dumps(upgraded.get("old")))
        with patch("job_store.compact_snapshot", side_effect=AssertionError("repeated migration")):
            JobStore(path)

    def test_legacy_scan_does_not_query_each_existing_report(self):
        run_id = self.publish()
        out = self.data / run_id
        out.mkdir()
        (out / "report.json").write_text('{"kind":"batch"}', encoding="utf-8")
        with patch.object(self.store, "get", side_effect=AssertionError("per-file lookup")):
            self.assertEqual(self.store.import_legacy_reports(self.data), 0)


if __name__ == "__main__":
    unittest.main()
