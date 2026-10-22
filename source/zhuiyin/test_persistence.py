"""Durable job lifecycle and usage checks using local synthetic transports only."""
from contextlib import closing, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

import engine
from job_store import JobStore
import server
import study


SECRET = "SYNTHETIC_PRIVATE_VALUE"


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="test-persistence-", dir=Path(__file__).resolve().parent)
        self.data = Path(self.folder.name)
        self.store = JobStore(self.data / "history.sqlite")
        self.patches = [patch.object(server, "DATA", self.data), patch.object(server, "JOBS", {}),
                        patch.object(server, "STOP_EVENTS", {})]
        for replacement in self.patches:
            replacement.start()

    def tearDown(self):
        for replacement in reversed(self.patches):
            replacement.stop()
        self.folder.cleanup()

    def handler(self, path):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {"Host": "127.0.0.1"}
        handler.path = path
        handler.server = SimpleNamespace(server_port=8765)
        handler.json = Mock()
        handler.send_bytes = Mock()
        return handler

    def get(self, path):
        handler = self.handler(path)
        handler.do_GET()
        return handler

    def test_new_job_has_durable_id_and_timestamps_before_execution(self):
        run_id = server.create_job("study", {"agent_mode": "rules"})
        saved = self.store.get(run_id)
        self.assertEqual(saved["run_id"], run_id)
        self.assertEqual(saved["status"], "running")
        self.assertIn("created_at", saved)
        self.assertIn("updated_at", saved)
        self.assertIn(run_id, server.STOP_EVENTS)

    def test_save_update_keeps_creation_and_reopens_from_disk(self):
        first = self.store.save({"run_id": "stable-001", "status": "running", "kind": "study"})
        self.store.save({"run_id": "stable-001", "status": "done", "kind": "study", "result": {"completed": 2}})
        restored = JobStore(self.data / "history.sqlite").get("stable-001")
        self.assertEqual(restored["created_at"], first["created_at"])
        self.assertEqual(restored["result"]["completed"], 2)
        self.assertEqual(self.store.list()["total"], 1)

    def test_recovery_marks_only_running_as_interrupted_without_reexecution(self):
        self.store.save({"run_id": "running", "kind": "study", "status": "running",
                         "partial_result": {"samples": [{"trial_index": 1}]}})
        self.store.save({"run_id": "completed", "status": "done", "result": {"ok": True}})
        with patch.object(engine, "model_request") as model, patch.object(engine, "run_trial") as trial:
            self.assertEqual(self.store.recover_interrupted(), 1)
            self.assertEqual(self.store.recover_interrupted(), 0)
        model.assert_not_called()
        trial.assert_not_called()
        restored = self.store.get("running")
        self.assertEqual(restored["status"], "interrupted")
        self.assertEqual(restored["partial_result"]["samples"][0]["trial_index"], 1)
        self.assertEqual(self.store.get("completed")["status"], "done")

    def test_credentials_are_removed_from_history_nested_snapshots_and_lists(self):
        self.store.save({"run_id": "private", "status": "done", "model_config": {"api_key": SECRET},
                         "config": {"equipment": "相机", "API-Key": SECRET},
                         "result": {"nested": [{"Authorization": SECRET, "refresh_token": SECRET,
                                               "visible": "keep"}]}})
        restored = self.store.get("private")
        self.assertNotIn(SECRET, json.dumps(restored))
        self.assertNotIn(SECRET, json.dumps(self.store.list()))
        self.assertEqual(restored["result"]["nested"][0]["visible"], "keep")

    def test_invalid_job_ids_cannot_read_or_write_paths(self):
        for run_id in ("../outside", "a/b", "a\\b", "", None, "x" * 81):
            self.assertIsNone(self.store.get(run_id))
            with self.assertRaises(ValueError):
                self.store.save({"run_id": run_id})

    def test_legacy_import_is_inert_sanitized_and_idempotent(self):
        folder = self.data / "legacy-demo"
        folder.mkdir()
        report = {"kind": "experiment", "model_config": {"api_key": SECRET},
                  "baseline": {"sqlite_dump": "CREATE TABLE MUST_NOT_EXECUTE(value TEXT);"}}
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(self.store.import_legacy_reports(self.data), 1)
        self.assertEqual(self.store.import_legacy_reports(self.data), 0)
        restored = self.store.get("legacy-demo")
        self.assertEqual(restored["status"], "done")
        self.assertNotIn(SECRET, json.dumps(restored))
        with closing(sqlite3.connect(self.data / "history.sqlite")) as database:
            self.assertIsNone(database.execute("SELECT name FROM sqlite_master WHERE name='MUST_NOT_EXECUTE'").fetchone())

    def test_partial_study_legacy_report_is_not_restored_as_complete(self):
        folder = self.data / "partial-study"
        folder.mkdir()
        report = {"kind": "study", "status": "partial", "stopped_reason": "request_budget_exhausted",
                  "completed": 1, "planned": 8, "samples": [{"trial_index": 1}]}
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(self.store.import_legacy_reports(self.data), 1)
        restored = self.store.get("partial-study")
        self.assertNotEqual(restored["status"], "done")
        self.assertEqual(restored["partial_result"]["completed"], 1)

    def test_history_and_get_api_restore_durable_job_after_memory_is_cleared(self):
        run_id = server.create_job("study")
        server.JOBS[run_id].update(status="done", result={"kind": "study", "status": "finished", "samples": []})
        server.checkpoint(run_id)
        server.JOBS.clear()
        server.STOP_EVENTS.clear()
        response = self.get(f"/api/runs/{run_id}")
        status, body = response.json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["kind"], "study")
        history = self.get("/api/history?limit=3").json.call_args.args[1]
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["run_id"], run_id)

    def seed_history(self, count=61):
        for index in range(count):
            self.store.save({"run_id": f"history-{index:03d}", "status": "done",
                             "created_at": "2026-10-16T00:00:00+00:00",
                             "label": f"记录 {index}"})

    def test_history_offset_pagination_reaches_51st_and_final_record_without_duplicates(self):
        self.seed_history()
        expected = [f"history-{index:03d}" for index in reversed(range(61))]
        visited = []
        for offset in range(0, 70, 10):
            status, page = self.get(f"/api/history?limit=10&offset={offset}").json.call_args.args
            self.assertEqual(status, 200)
            self.assertEqual(page["total"], 61)
            self.assertEqual([item["run_id"] for item in page["items"]], expected[offset:offset + 10])
            visited.extend(item["run_id"] for item in page["items"])
        self.assertEqual(visited, expected)
        self.assertEqual(len(set(visited)), 61)
        self.assertEqual(visited[50], "history-010")
        self.assertEqual(visited[-1], "history-000")

    def test_history_default_offset_preserves_existing_limit_and_order(self):
        self.seed_history()
        default = self.get("/api/history?limit=10").json.call_args.args
        explicit = self.get("/api/history?limit=10&offset=0").json.call_args.args
        self.assertEqual(default, explicit)
        self.assertEqual(len(self.get("/api/history").json.call_args.args[1]["items"]), 50)

    def test_history_offset_boundaries_reject_invalid_and_return_empty_past_end(self):
        self.seed_history()
        for value in ("-1", "1.5", "word", "", "NaN", "Infinity"):
            with self.subTest(offset=value):
                self.assertEqual(self.get(f"/api/history?offset={value}").json.call_args.args[0], 400)
        for value in ("61", "62", "99999999999999999999999999999999999999"):
            with self.subTest(offset=value):
                status, page = self.get(f"/api/history?limit=10&offset={value}").json.call_args.args
                self.assertEqual(status, 200)
                self.assertEqual(page, {"items": [], "total": 61})
        self.assertEqual(len(self.get("/api/history?limit=10&offset=60").json.call_args.args[1]["items"]), 1)
        for value in (-1, True, 0.5, "1", None):
            with self.subTest(store_offset=value):
                with self.assertRaises(ValueError):
                    self.store.list(10, value)

    def test_real_rules_study_worker_exposes_result_and_clears_credentials(self):
        cfg = engine.Config(model_config={"api_key": SECRET})
        run_id = server.create_job("study", cfg.public())
        server.study_worker(run_id, cfg, {"repetitions": 2, "strategies": ["baseline", "verified"]})
        body = self.get(f"/api/runs/{run_id}").json.call_args.args[1]
        self.assertEqual(body["status"], "done", body.get("error"))
        self.assertEqual(body["result"]["status"], "finished")
        self.assertEqual(len(body["result"]["samples"]), 4)
        self.assertEqual(cfg.model_config, {})
        self.assertNotIn(SECRET, (self.data / run_id / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(self.store.get(run_id)["result"]["completed"], 4)

    def test_cancelled_study_worker_exports_partial_report_in_api_and_history(self):
        cfg = engine.Config(model_config={"api_key": SECRET})
        run_id = server.create_job("study", cfg.public())
        server.STOP_EVENTS[run_id].set()
        server.study_worker(run_id, cfg, {"repetitions": 2, "strategies": ["baseline"]})
        body = self.get(f"/api/runs/{run_id}").json.call_args.args[1]
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["result"]["status"], "partial")
        self.assertEqual(body["partial_result"]["stopped_reason"], "cancelled")
        self.assertEqual(body["result"]["completed"], 0)
        self.assertEqual(cfg.model_config, {})
        server.JOBS.clear()
        handler = self.get(f"/api/runs/{run_id}/export")
        status, raw = handler.send_bytes.call_args.args[:2]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["stopped_reason"], "cancelled")

    def test_partial_worker_exception_keeps_last_checkpoint_without_secret_error(self):
        cfg = engine.Config(model_config={"api_key": SECRET})
        run_id = server.create_job("study", cfg.public())
        def interrupted(cfg, options, **callbacks):
            callbacks["on_checkpoint"]({"kind": "study", "status": "partial", "completed": 1,
                                        "samples": [{"trial_index": 1}]})
            raise RuntimeError(SECRET)
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(server, "run_study", side_effect=interrupted):
            server.study_worker(run_id, cfg, {})
        restored = self.store.get(run_id)
        self.assertEqual(restored["partial_result"]["completed"], 1)
        self.assertNotIn(SECRET, json.dumps(restored))
        self.assertEqual(cfg.model_config, {})
        self.assertIn("RuntimeError: " + SECRET, stderr.getvalue())

    def test_study_post_validates_before_dispatch_and_returns_running_job_id(self):
        handler = self.handler("/api/study")
        handler.read_json = lambda **kwargs: {"config": {}, "options": {"repetitions": 2}}
        with patch.object(server.threading, "Thread") as thread:
            handler.do_POST()
        status, body = handler.json.call_args.args
        self.assertEqual(status, 202)
        self.assertEqual(self.store.get(body["run_id"])["status"], "running")
        self.assertIs(thread.call_args.kwargs["target"], server.study_worker)
        thread.return_value.start.assert_called_once()


class ModelUsageTransportTests(unittest.TestCase):
    def setUp(self):
        self.old_context = dict(vars(engine.MODEL_CONTEXT))
        vars(engine.MODEL_CONTEXT).clear()
        engine.MODEL_CONTEXT.remaining = 5
        engine.MODEL_CONTEXT.usage_records = []
        self.model = {"base_url": "http://127.0.0.1:23456/v1", "model": "synthetic", "api_key": SECRET}

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.old_context)

    def response(self, body):
        return io.BytesIO(json.dumps(body).encode())

    def test_usage_hook_records_real_transport_attempt_and_valid_token_counts(self):
        body = {"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}
        with patch("engine.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = self.response(body)
            self.assertEqual(engine.model_request(self.model, [])["content"], "OK")
        record = engine.MODEL_CONTEXT.usage_records[0]
        self.assertEqual(record["status"], "ok")
        self.assertEqual((record["prompt_tokens"], record["completion_tokens"], record["total_tokens"]), (12, 4, 16))
        self.assertTrue(record["usage_available"])
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        self.assertNotIn(SECRET, json.dumps(record))

    def test_invalid_reply_preserves_reported_usage_without_counting_as_business_success(self):
        with patch("engine.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = self.response({"usage": {"prompt_tokens": 9, "completion_tokens": 2}})
            with self.assertRaises(RuntimeError):
                engine.model_request(self.model, [])
        record = engine.MODEL_CONTEXT.usage_records[0]
        self.assertEqual(record["status"], "invalid_response")
        self.assertTrue(record["usage_available"])
        self.assertEqual(record["prompt_tokens"], 9)

    def test_http_network_errors_record_unknown_usage_and_do_not_echo_provider_body(self):
        failures = [urllib.error.HTTPError(self.model["base_url"], 429, "rate limited", {}, io.BytesIO(SECRET.encode())),
                    urllib.error.URLError(SECRET)]
        for failure in failures:
            with patch("engine.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(RuntimeError) as raised:
                    engine.model_request(self.model, [])
            self.assertNotIn(SECRET, str(raised.exception))
        records = engine.MODEL_CONTEXT.usage_records
        self.assertEqual([record["status"] for record in records], ["http_error", "network_error"])
        self.assertTrue(all(record["prompt_tokens"] is None for record in records))
        self.assertTrue(all(not record["usage_available"] for record in records))

    def test_budget_cancel_and_invalid_config_do_not_create_phantom_request_records(self):
        engine.MODEL_CONTEXT.remaining = 0
        with patch("engine.urllib.request.build_opener") as opener:
            with self.assertRaises(RuntimeError):
                engine.model_request(self.model, [])
            engine.MODEL_CONTEXT.remaining = 3
            engine.MODEL_CONTEXT.cancel_check = lambda: True
            with self.assertRaises(RuntimeError):
                engine.model_request(self.model, [])
            with self.assertRaises(ValueError):
                engine.model_request({"base_url": "invalid"}, [])
        opener.assert_not_called()
        self.assertEqual(engine.MODEL_CONTEXT.usage_records, [])
        self.assertEqual(engine.MODEL_CONTEXT.remaining, 3)

    def test_boolean_and_negative_token_counts_remain_unknown(self):
        body = {"choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "2"}}
        with patch("engine.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = self.response(body)
            engine.model_request(self.model, [])
        record = engine.MODEL_CONTEXT.usage_records[0]
        self.assertFalse(record["usage_available"])
        self.assertIsNone(record["prompt_tokens"])
        self.assertIsNone(record["completion_tokens"])

    def test_entire_llm_study_uses_synthetic_transport_and_global_budget(self):
        calls = [0]
        def transport(*args, **kwargs):
            calls[0] += 1
            name = "reserve_device" if calls[0] % 2 else "finish_task"
            arguments = "{}" if name == "reserve_device" else '{"success":true,"reason":"synthetic test"}'
            return self.response({"choices": [{"message": {"tool_calls": [
                {"id": str(calls[0]), "type": "function", "function": {"name": name, "arguments": arguments}}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        cfg = engine.Config(agent_mode="llm", faults=[], model_config=self.model)
        with patch("engine.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = transport
            report = study.run_study(cfg, {"repetitions": 2, "strategies": ["baseline"], "max_requests": 3})
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stopped_reason"], "request_budget_exhausted")
        self.assertEqual(calls[0], 3)
        self.assertEqual(report["model_usage"]["requests"], 3)
        self.assertEqual(report["model_usage"]["prompt_tokens"], 300)
        self.assertEqual(report["arms"][0]["valid_trials"], 1)
        self.assertEqual(report["arms"][0]["infra_errors"], 1)
        self.assertNotIn(SECRET, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
