"""Worker failures retain useful results without publishing exception details."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import engine
from job_store import JobStore
import server


SECRET = "PRIVATE_SYNTHETIC_API_CREDENTIAL"
MARKER = "PRIVATE_SYNTHETIC_LOCAL_PATH"
FAILURE = f"{MARKER}: {SECRET}"


class WorkerPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-worker-privacy-", dir=Path(__file__).resolve().parent)
        self.data = Path(self.temporary.name)
        self.patches = [patch.object(server, "DATA", self.data), patch.object(server, "JOBS", {}),
                        patch.object(server, "STOP_EVENTS", {}), patch.object(server, "STORES", {})]
        for replacement in self.patches:
            replacement.start()
        self.store = JobStore(self.data / "history.sqlite")

    def tearDown(self):
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temporary.cleanup()

    def handler(self, path):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {"Host": "127.0.0.1"}
        handler.path = path
        handler.server = SimpleNamespace(server_port=8765)
        handler.json = Mock()
        handler.send_bytes = Mock()
        handler.read_json = lambda **kwargs: {}
        return handler

    def get(self, path):
        handler = self.handler(path)
        handler.do_GET()
        return handler

    def assert_private(self, value):
        text = value.decode("utf-8") if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(MARKER, text)

    def assert_private_job_and_history(self, run_id):
        self.assert_private(server.JOBS[run_id])
        self.assert_private(self.store.get(run_id))
        for path in (f"/api/runs/{run_id}", f"/api/runs/{run_id}?lite=1", "/api/history"):
            handler = self.get(path)
            status, body = handler.json.call_args.args
            self.assertEqual(status, 200)
            self.assert_private(body)
        server.JOBS.clear()
        self.assert_private(self.get(f"/api/runs/{run_id}").json.call_args.args[1])

    def assert_diagnostic(self, stderr):
        text = stderr.getvalue()
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("RuntimeError: " + FAILURE, text)

    def test_single_failure_is_private_in_memory_persistence_and_export_response(self):
        config = engine.Config(model_config={"api_key": SECRET})
        run_id = server.create_job(config=config.public())
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(server, "run_experiment", side_effect=RuntimeError(FAILURE)):
            server.worker(run_id, config)
        self.assertEqual(server.JOBS[run_id]["status"], "error")
        self.assertEqual(server.JOBS[run_id]["error"], "实验运行中断；详情见 data/server-error.log。")
        self.assertEqual(config.model_config, {})
        self.assert_diagnostic(stderr)
        self.assert_private_job_and_history(run_id)
        export = self.get(f"/api/runs/{run_id}/export")
        self.assertEqual(export.json.call_args.args[0], 409)
        self.assert_private(export.json.call_args.args[1])
        export.send_bytes.assert_not_called()

    def test_batch_failure_preserves_finished_case_and_private_durable_export(self):
        entries, source = server.validate_batch({"scenarios": [
            {"name": "第一个场景", "config": {"request_id": "private-batch-1"}},
            {"name": "第二个场景", "config": {"request_id": "private-batch-2"}},
        ]})
        for entry in entries:
            entry["config"].model_config["api_key"] = SECRET
        run_id = server.create_job("batch")
        completed = {"baseline": {"failed": True}, "fixed": {"failed": False}, "minimized_faults": []}
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(server, "run_experiment", side_effect=[completed, RuntimeError(FAILURE)]):
            server.batch_worker(run_id, entries, source)
        job = server.JOBS[run_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("批量实验运行中断", job["error"])
        self.assertEqual(job["partial_result"]["error"], job["error"])
        self.assertFalse(job["partial_result"]["cancelled"])
        self.assertEqual(job["partial_result"]["completed"], 1)
        self.assertEqual(job["partial_result"]["cases"][0]["name"], "第一个场景")
        self.assertTrue(all(not entry["config"].model_config for entry in entries))
        self.assert_diagnostic(stderr)
        self.assert_private((self.data / run_id / "report.json").read_bytes())
        self.assert_private_job_and_history(run_id)
        export = self.get(f"/api/runs/{run_id}/export")
        status, body = export.send_bytes.call_args.args[:2]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["completed"], 1)
        self.assert_private(body)

    def test_study_failure_keeps_last_checkpoint_and_diagnostic_private(self):
        config = engine.Config(model_config={"api_key": SECRET})
        run_id = server.create_job("study", config.public())

        def interrupted(config, options, **callbacks):
            callbacks["on_checkpoint"]({"kind": "study", "status": "partial", "completed": 1,
                                         "samples": [{"trial_index": 1}]})
            raise RuntimeError(FAILURE)

        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(server, "run_study", side_effect=interrupted):
            server.study_worker(run_id, config, {})
        self.assertEqual(server.JOBS[run_id]["error"], "多轮实验未完成；已保存最后检查点。")
        self.assertEqual(server.JOBS[run_id]["partial_result"]["completed"], 1)
        self.assertEqual(config.model_config, {})
        self.assert_diagnostic(stderr)
        self.assert_private_job_and_history(run_id)
        export = self.get(f"/api/runs/{run_id}/export")
        status, body = export.send_bytes.call_args.args[:2]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["completed"], 1)
        self.assert_private(body)

    def test_batch_report_write_failure_still_records_private_partial_status(self):
        entries, source = server.validate_batch({"scenarios": [
            {"name": "已完成场景", "config": {"request_id": "private-write-failure"}},
        ]})
        entries[0]["config"].model_config["api_key"] = SECRET
        run_id = server.create_job("batch")
        completed = {"baseline": {"failed": True}, "fixed": {"failed": False}, "minimized_faults": []}
        original_write = Path.write_text
        failed_paths = []

        def fail_report_write(path, *args, **kwargs):
            if path.name == "report.json":
                failed_paths.append(path)
                raise OSError(FAILURE)
            return original_write(path, *args, **kwargs)

        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(Path, "write_text", new=fail_report_write), \
                patch.object(server, "run_experiment", return_value=completed):
            server.batch_worker(run_id, entries, source)
        self.assertEqual(failed_paths, [self.data / run_id / "report.json"] * 2)
        job = server.JOBS[run_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("批量实验运行中断", job["error"])
        self.assertEqual(job["partial_result"]["completed"], 1)
        self.assertEqual(job["partial_result"]["cases"][0]["name"], "已完成场景")
        self.assertFalse(job["partial_result"]["cancelled"])
        self.assertEqual(entries[0]["config"].model_config, {})
        self.assertIn("OSError: " + FAILURE, stderr.getvalue())
        self.assertIn("Traceback (most recent call last)", stderr.getvalue())
        self.assert_private_job_and_history(run_id)
        export = self.get(f"/api/runs/{run_id}/export")
        status, body = export.send_bytes.call_args.args[:2]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["completed"], 1)
        self.assert_private(body)

    def test_cancel_unknown_id_returns_not_found_without_creating_event(self):
        handler = self.handler("/api/runs/unknown/cancel")
        handler.do_POST()
        handler.json.assert_called_once_with(404, {"error": "实验不存在"})
        self.assertEqual(server.STOP_EVENTS, {})
        self.assertEqual(self.store.list()["total"], 0)

    def test_cancel_existing_id_stops_worker_without_starting_trial(self):
        config = engine.Config(model_config={"api_key": SECRET})
        run_id = server.create_job(config=config.public())
        handler = self.handler(f"/api/runs/{run_id}/cancel")
        handler.do_POST()
        handler.json.assert_called_once_with(200, {"ok": True})
        self.assertTrue(server.STOP_EVENTS[run_id].is_set())
        with redirect_stderr(io.StringIO()), patch.object(engine, "run_trial") as trial:
            server.worker(run_id, config)
        trial.assert_not_called()
        self.assertEqual(server.JOBS[run_id]["status"], "error")
        self.assertEqual(server.JOBS[run_id]["error"], server.STOP_MESSAGES["cancelled"])
        self.assertEqual(config.model_config, {})


if __name__ == "__main__":
    unittest.main()
