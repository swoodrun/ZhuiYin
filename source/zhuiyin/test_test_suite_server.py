"""HTTP, durable history and cancellation contracts, using synthetic inputs only."""
import base64
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import launch
import server
import test_suite


def upload():
    return {"filename": "synthetic-workflow.json", "content": json.dumps({
        "kind": "scenario", "name": "Workflow fixture", "config": {
            "request_id": "SYNTHETIC-WORKFLOW-ONLY", "equipment": "Workflow fixture",
            "stock": 5, "quantity": 1, "faults": ["commit_response_lost"]}})}


class TestSuiteServerTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / "data" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-http-", dir=base)
        self.data = Path(self.temp.name) / "runs"
        self.patches = [patch.object(server, "DATA", self.data), patch.object(server, "JOBS", {}),
                        patch.object(server, "STOP_EVENTS", {}), patch.object(server, "STORES", {})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def handler(self, path, body=None, host="127.0.0.1"):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {"Host": host}
        handler.path = path
        handler.server = SimpleNamespace(server_port=8766)
        handler.json, handler.send_bytes = Mock(), Mock()
        if body is not None:
            handler.read_json = Mock(return_value=body)
        return handler

    def preview(self):
        handler = self.handler("/api/test-suites/preview", {"files": [upload()]})
        handler.do_POST()
        code, value = handler.json.call_args.args
        self.assertEqual(code, 200, value)
        return value

    def test_preview_persists_only_prepared_data_and_never_executes(self):
        with patch.object(server, "execute_suite", side_effect=AssertionError("preview executed")):
            plan = self.preview()
        self.assertFalse(server.JOBS)
        self.assertTrue((server.suite_root() / plan["suite_id"] / "prepared.json").is_file())
        self.assertFalse(self.data.exists())
        server.STORES.clear()
        restored = server.load_prepared(server.suite_root(), plan["suite_id"])
        self.assertEqual(restored["cases"][0]["config"]["request_id"], "SYNTHETIC-WORKFLOW-ONLY")

    def test_zip_preview_uses_base64_and_ignores_answers(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("inputs/fixture.json", upload()["content"])
            archive.writestr("answers/answer.json", "This must never be parsed")
        handler = self.handler("/api/test-suites/preview", {"files": [{"filename": "fixture.zip", "content_base64": base64.b64encode(buffer.getvalue()).decode()}]})
        handler.do_POST()
        code, result = handler.json.call_args.args
        self.assertEqual(code, 200, result)
        self.assertEqual(len(result["cases"]), 1)
        self.assertNotIn("This must never", json.dumps(result))

    def test_validation_rejects_extra_controls_invalid_encoding_and_unknown_id(self):
        bodies = [("preview", {"files": [upload()], "model_config": {"api_key": "SYNTHETIC-SECRET"}}),
                  ("preview", {"files": [{"filename": "x.zip", "content_base64": "!!!"}]}),
                  ("start", {"suite_id": "../outside", "profile": "quick"}),
                  ("start", {"suite_id": "missing", "profile": "llm"})]
        for endpoint, body in bodies:
            with self.subTest(endpoint=endpoint), patch.object(server.threading, "Thread") as thread:
                handler = self.handler("/api/test-suites/" + endpoint, body)
                handler.do_POST()
                self.assertEqual(handler.json.call_args.args[0], 400)
                thread.assert_not_called()
                self.assertNotIn("SYNTHETIC-SECRET", json.dumps(handler.json.call_args.args))

    def test_start_then_history_and_exports_survive_memory_loss_without_rerun(self):
        plan = self.preview()
        handler = self.handler("/api/test-suites/start", {"suite_id": plan["suite_id"], "profile": "quick"})
        with patch.object(server.threading, "Thread") as thread:
            handler.do_POST()
        code, body = handler.json.call_args.args
        self.assertEqual(code, 202)
        run_id = body["run_id"]
        self.assertIs(thread.call_args.kwargs["target"], server.suite_worker)
        server.suite_worker(*thread.call_args.kwargs["args"])
        current = server.get_job(run_id)
        self.assertEqual(current["status"], "done", current.get("error"))
        self.assertEqual(current["result"]["kind"], "test_suite")
        self.assertTrue((self.data / run_id / "summary.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        server.JOBS.clear()
        with patch.object(server, "execute_suite", side_effect=AssertionError("history reran")):
            restored = server.get_job(run_id)
            self.assertEqual(restored["result"]["run_id"], run_id)
            self.assertEqual(server.store().list()["items"][0]["kind"], "test_suite")
            for name in ("suite-export.zip", "summary.csv", "export"):
                download = self.handler(f"/api/runs/{run_id}/{name}")
                download.do_GET()
                self.assertEqual(download.send_bytes.call_args.args[0], 200)
        other = self.handler(f"/api/runs/{run_id}/prepared.json")
        other.do_GET()
        self.assertEqual(other.json.call_args.args[0], 404)

    def test_agent_batch_credentials_stay_in_worker_and_are_cleared_after_execution(self):
        from test_experiment_budget import MODEL, SyntheticModel
        import engine
        preview = self.preview()
        handler = self.handler('/api/test-suites/start', {'suite_id': preview['suite_id'], 'profile': 'agent',
            'agent_config': {'model_config': MODEL, 'max_requests': 5, 'prompt_policy': 'recovery'}})
        with patch.object(server.threading, 'Thread') as thread:
            handler.do_POST()
        code, body = handler.json.call_args.args
        self.assertEqual(code, 202)
        run_id = body['run_id']
        self.assertNotIn(MODEL['api_key'], json.dumps(server.get_job(run_id)))
        args = thread.call_args.kwargs['args']
        fake = SyntheticModel()
        with patch.object(engine.urllib.request, 'build_opener', return_value=fake):
            server.suite_worker(*args)
        self.assertEqual(args[2].model_config, {})
        job = server.get_job(run_id)
        self.assertEqual(job['result']['profile'], 'agent')
        self.assertEqual(job['result']['stopped_reason'], 'request_budget_exhausted')
        self.assertEqual(len(fake.requests), 5)
        self.assertEqual(job['status'], 'error')
        self.assertNotIn(MODEL['api_key'], json.dumps(job))
        self.assertTrue((self.data/run_id/'suite-export.zip').is_file())

    def test_agent_start_requires_valid_model_but_rules_reject_model_settings(self):
        preview = self.preview()
        for profile, extras in [('agent', {}), ('agent', {'agent_config': {}}),
                                ('quick', {'agent_config': {}})]:
            handler = self.handler('/api/test-suites/start', {'suite_id': preview['suite_id'],
                                                             'profile': profile, **extras})
            with patch.object(server.threading, 'Thread') as thread:
                handler.do_POST()
            self.assertEqual(handler.json.call_args.args[0], 400)
            thread.assert_not_called()

    def test_cancellation_is_durable_and_exports_unrun_positions(self):
        plan = server.load_prepared(server.suite_root(), self.preview()["suite_id"])
        run_id = server.create_job("test_suite")
        cancel = self.handler(f"/api/runs/{run_id}/cancel", {})
        cancel.do_POST()
        server.suite_worker(run_id, plan)
        server.JOBS.clear()
        job = server.get_job(run_id)
        self.assertEqual(job["status"], "error")
        self.assertTrue(job["result"]["cancelled"])
        self.assertTrue((self.data / run_id / "suite-export.zip").is_file())

    def test_hard_restart_exports_saved_arm_without_execution_or_model_calls(self):
        plan = server.load_prepared(server.suite_root(), self.preview()["suite_id"])
        run_id = server.create_job("test_suite", {"suite_id": plan["suite_id"]})
        out = self.data / run_id
        class SimulatedPowerLoss(BaseException):
            pass
        def crash_after_arm(report):
            server.JOBS[run_id]["partial_result"] = report
            server.checkpoint(run_id)
            if report.get("cases", [{}])[0].get("strategies"):
                raise SimulatedPowerLoss()
        with self.assertRaises(SimulatedPowerLoss):
            test_suite.execute_suite(plan, out, progress_callback=crash_after_arm)
        database = next(out.glob("cases/*/*.sqlite"))
        original_bytes = database.read_bytes()
        server.JOBS.clear()
        server.store().recover_interrupted()
        with patch.object(test_suite, "run_trial", side_effect=AssertionError("recovery ran a trial")), \
             patch.object(test_suite, "run_experiment", side_effect=AssertionError("recovery ran experiment")), \
             patch.object(test_suite, "parse_upload", side_effect=AssertionError("recovery parsed inputs")), \
             patch.object(test_suite.sqlite3, "connect", wraps=test_suite.sqlite3.connect):
            self.assertEqual(server.recover_test_suites(), 1)
            self.assertEqual(server.recover_test_suites(), 0)
        job = server.get_job(run_id)
        self.assertEqual(job["status"], "interrupted")
        report = job["partial_result"]
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["evaluation"]["attempted_trials"], 1)
        self.assertEqual(report["evaluation"]["execution_invalid_trials"], 0)
        self.assertEqual(report["evaluation"]["not_run_trials"], 3)
        self.assertEqual(database.read_bytes(), original_bytes)
        self.assertTrue((out / "suite-export.zip").is_file())
        self.assertTrue(report["artifacts"]["summary_csv"].endswith("/summary.csv"))

    def test_restart_before_first_arm_preserves_all_unrun_positions(self):
        preview = self.preview()
        run_id = server.create_job("test_suite", {"suite_id": preview["suite_id"]})
        server.JOBS.clear()
        server.store().recover_interrupted()
        with patch.object(server, "execute_suite", side_effect=AssertionError("restart executed")):
            self.assertEqual(server.recover_test_suites(), 1)
        report = server.get_job(run_id)["partial_result"]
        self.assertEqual(report["evaluation"]["attempted_trials"], 0)
        self.assertEqual(report["evaluation"]["not_run_trials"], 4)

    def test_500_case_preview_is_supported_but_501_rejected_before_execution(self):
        scenarios = [{"name": "Synthetic scale " + str(i), "config": {"request_id": "SYNTHETIC-SCALE-" + str(i), "faults": []}} for i in range(501)]
        for count in (500, 501):
            handler = self.handler("/api/test-suites/preview", {"files": [{"filename": "synthetic-scale.json", "content": json.dumps({"kind": "scenarios", "scenarios": scenarios[:count]})}]})
            with patch.object(server, "execute_suite", side_effect=AssertionError("scale preview executed")):
                handler.do_POST()
            code, body = handler.json.call_args.args
            self.assertEqual(code, 200 if count == 500 else 400, body)
            if count == 500:
                self.assertEqual(body["counts"]["ready"], 500)

    def test_new_route_uses_bounded_larger_body_and_does_not_change_old_limits(self):
        for path, expected in (("/api/test-suites/preview", server.SUITE_REQUEST_LIMIT), ("/api/import", 4 * 1024 * 1024)):
            handler = self.handler(path, {})
            handler.do_POST()
            handler.read_json.assert_called_once_with(limit=expected)
        handler = self.handler("/api/test-suites/preview")
        handler.headers.update({"Content-Type": "application/json", "Content-Length": str(server.SUITE_REQUEST_LIMIT + 1)})
        handler.rfile = Mock()
        handler.do_POST()
        self.assertEqual(handler.json.call_args.args[0], 400)
        handler.rfile.read.assert_not_called()

    def test_cross_site_rejected_before_upload_read(self):
        handler = self.handler("/api/test-suites/preview", {"files": [upload()]}, "attacker.example")
        handler.do_POST()
        self.assertEqual(handler.json.call_args.args[0], 403)
        handler.read_json.assert_not_called()

    def test_internal_errors_do_not_echo_user_content(self):
        handler = self.handler("/api/test-suites/preview", {"files": [upload()]})
        with patch.object(server, "prepare_uploads", side_effect=RuntimeError("SYNTHETIC-SECRET")), redirect_stderr(io.StringIO()):
            handler.do_POST()
        self.assertEqual(handler.json.call_args.args[0], 502)
        self.assertNotIn("SYNTHETIC-SECRET", str(handler.json.call_args.args))

    def test_health_and_assets_expose_new_workflow(self):
        health = self.handler("/api/health")
        health.do_GET()
        value = health.json.call_args.args[1]
        self.assertTrue(value["test_suites"])
        self.assertEqual(value["test_workflow_version"], server.WORKFLOW_VERSION)
        for name in ("testcenter.js", "testcenter.css"):
            handler = self.handler("/" + name)
            handler.do_GET()
            self.assertEqual(handler.send_bytes.call_args.args[0], 200)

    def test_launcher_requires_workflow_capability_before_reusing_service(self):
        value = {"version": launch.APP_VERSION, "concurrency": True, "default_mode": "rules", "history": True,
                 "faults": {"corrupt_payload": "synthetic"}}
        def responds():
            return io.StringIO(json.dumps(value))
        with patch.object(launch.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = lambda *args, **kwargs: responds()
            self.assertFalse(launch.healthy(8766))
            value.update(test_suites=True, test_workflow_version=launch.WORKFLOW_VERSION,
                         agent_workbench="agent-ui-v1", workbench_billing="run-billing-v1")
            self.assertTrue(launch.healthy(8766))


if __name__ == "__main__":
    unittest.main()
