"""Product APIs over loopback with isolated stores and no external model I/O."""
from contextlib import ExitStack
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import engine
import server


ROOT = Path(__file__).resolve().parent


class QuietHandler(server.Handler):
    def log_message(self, *_):
        pass


class ProductAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="test-product-api-", dir=ROOT))
        self.folder = Path(folder)
        self.data = self.folder / "runs"
        for name, value in (("DATA", self.data), ("JOBS", {}), ("STOP_EVENTS", {}),
                            ("STORES", {}), ("TEMPLATE_STORES", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.model_request = self.stack.enter_context(patch.object(engine, "model_request", side_effect=AssertionError("External model requests are forbidden in API tests")))
        self.stack.enter_context(patch.object(server, "model_request", side_effect=AssertionError("Connection tests are forbidden in API tests")))
        self.worker_threads = set()
        self.worker_errors = []
        self.worker_lock = threading.Lock()
        original_worker = server.worker

        def tracked_worker(*args, **kwargs):
            with self.worker_lock:
                self.worker_threads.add(threading.current_thread())
            try:
                return original_worker(*args, **kwargs)
            except BaseException as exc:
                with self.worker_lock:
                    self.worker_errors.append(type(exc).__name__)
                raise

        self.stack.enter_context(patch.object(server, "worker", new=tracked_worker))
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        for event in server.STOP_EVENTS.values():
            event.set()
        deadline = time.monotonic() + 10
        while any(job.get("status") == "running" for job in server.JOBS.values()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.join_workers()
        self.model_request.assert_not_called()

    def join_workers(self):
        """Wait through final checkpoints before restoring global stores."""
        with self.worker_lock:
            workers = list(self.worker_threads)
        deadline = time.monotonic() + 20
        for worker in workers:
            worker.join(timeout=max(0.01, deadline - time.monotonic()))
        self.assertFalse(any(worker.is_alive() for worker in workers), "API test worker did not finish before isolated stores were released")
        self.assertEqual(self.worker_errors, [], "API test worker raised after publishing its final status")

    def request(self, method, path, body=None, *, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        content = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=content, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def template_body(self, **changes):
        body = {"name": "API合成模板", "description": "仅供本地接口验证",
                "config": {"agent_mode": "rules", "scenario": "equipment", "equipment": "测试相机", "stock": 3,
                           "quantity": 1, "request_id": "product-api-001", "seed": 17, "faults": [],
                           "max_requests": 400, "prompt_policy": "neutral"}}
        body.update(changes)
        return body

    def create_template(self, **changes):
        status, body = self.request("POST", "/api/task-templates", self.template_body(**changes))
        self.assertEqual(status, 201, body)
        return body

    def save_job(self, run_id, *, failed=False):
        cfg = engine.Config(agent_mode="rules", faults=["commit_response_lost", "stale_read"] if failed else [])
        result = {"config": cfg.public(), "mode": "rules", "evaluation_protocol": engine.EVALUATION_PROTOCOL,
                  "baseline": engine.run_trial(cfg, cfg.faults), "status": "finished", "completed": True}
        job = {"run_id": run_id, "kind": "experiment", "status": "done", "phase": "done", "progress": 100,
               "label": "合成接口历史", "config": cfg.public(), "events": [], "result": result}
        server.JOBS[run_id] = job
        server.checkpoint(run_id)
        return copy.deepcopy(job)

    def test_template_create_list_read_update_delete_and_preserved_versions(self):
        created = self.create_template()
        template_id = created["id"]
        self.assertEqual(created["version"], 1)
        self.assertEqual(self.request("GET", "/api/task-templates")[1]["templates"], [created])
        self.assertEqual(self.request("GET", "/api/task-templates/" + template_id), (200, created))
        update = self.template_body(name="修改后的合成模板", expected_version=1)
        status, second = self.request("PUT", "/api/task-templates/" + template_id, update)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["created_at"], created["created_at"])
        self.assertEqual(server.template_store().get(template_id, 1), created)
        self.assertEqual(self.request("DELETE", "/api/task-templates/" + template_id, {"expected_version": 2}), (200, {"ok": True}))
        self.assertEqual(self.request("GET", "/api/task-templates/" + template_id)[0], 404)
        self.assertEqual(self.request("GET", "/api/task-templates")[1]["templates"], [])
        self.assertEqual(server.template_store().get(template_id, 1), created)

    def test_template_optimistic_conflicts_return_409_for_update_and_delete(self):
        created = self.create_template()
        path = "/api/task-templates/" + created["id"]
        self.assertEqual(self.request("PUT", path, self.template_body(expected_version=1))[0], 200)
        for method, body in (("PUT", self.template_body(expected_version=1)), ("DELETE", {"expected_version": 1})):
            with self.subTest(method=method):
                status, response = self.request(method, path, body)
                self.assertEqual(status, 409, response)
        self.assertEqual(self.request("GET", path)[1]["version"], 2)

    def test_template_missing_and_invalid_identifiers_have_json_responses(self):
        for method, body in (("GET", None), ("PUT", self.template_body(expected_version=1)), ("DELETE", {"expected_version": 1})):
            with self.subTest(method=method):
                status, response = self.request(method, "/api/task-templates/012345abcdef", body)
                self.assertEqual(status, 404, response)
        status, response = self.request("GET", "/api/task-templates/invalid-id")
        self.assertEqual(status, 400, response)

    def test_template_credentials_connections_and_executable_fields_are_rejected(self):
        bodies = []
        for key in ("api_key", "model_config", "connection", "base_url", "headers", "sql", "script"):
            body = self.template_body()
            body["config"][key] = {"Authorization": "SYNTHETIC_SECRET"}
            bodies.append(body)
        body = self.template_body(contract={"expected_fields": {"api_key": "SYNTHETIC_SECRET"}})
        bodies.append(body)
        for body in bodies:
            with self.subTest(keys=sorted(body.get("config", {}))):
                status, response = self.request("POST", "/api/task-templates", body)
                self.assertEqual(status, 400, response)
                self.assertNotIn("SYNTHETIC_SECRET", json.dumps(response))
        self.assertEqual(self.request("GET", "/api/task-templates")[1]["templates"], [])

    def test_rejected_template_update_preserves_the_saved_version(self):
        created = self.create_template()
        update = self.template_body(expected_version=1)
        update["config"]["model_config"] = {"api_key": "SYNTHETIC_SECRET"}
        path = "/api/task-templates/" + created["id"]
        status, response = self.request("PUT", path, update)
        self.assertEqual(status, 400, response)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(response))
        self.assertEqual(self.request("GET", path), (200, created))
        self.assertIsNone(server.template_store().get(created["id"], 2))

    def test_template_mutations_reject_cross_origin(self):
        created = self.create_template()
        for method, path, body in (("POST", "/api/task-templates", self.template_body()),
                                    ("PUT", "/api/task-templates/" + created["id"], self.template_body(expected_version=1)),
                                    ("DELETE", "/api/task-templates/" + created["id"], {"expected_version": 1})):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, path, body, origin="https://outside.invalid")[0], 403)
        self.assertEqual(self.request("GET", "/api/task-templates")[1]["templates"], [created])

    def test_compare_only_reads_saved_jobs_and_does_not_start_a_worker(self):
        self.save_job("compare-left", failed=True)
        self.save_job("compare-right", failed=False)
        before = copy.deepcopy(server.JOBS)
        persisted = {key: server.store().get(key) for key in before}
        with patch.object(server, "create_job", side_effect=AssertionError("Read-only comparison cannot start a job")):
            status, compared = self.request("POST", "/api/compare", {"left_run_id": "compare-left", "right_run_id": "compare-right"})
        self.assertEqual(status, 200, compared)
        self.assertTrue(compared["read_only"])
        self.assertEqual(compared["left"]["records"], 1)
        self.assertEqual(compared["right"]["records"], 1)
        self.assertEqual(server.JOBS, before)
        self.assertEqual({key: server.store().get(key) for key in before}, persisted)
        self.assertEqual(self.request("POST", "/api/compare", {"left_run_id": "missing", "right_run_id": "compare-right"})[0], 404)
        self.assertEqual(self.request("POST", "/api/compare", {"left_run_id": "compare-left", "right_run_id": "compare-right", "run": True})[0], 400)

    def test_diagnostics_endpoint_links_saved_evidence_and_keeps_source_unchanged(self):
        original = self.save_job("diagnostic-source", failed=True)
        before = copy.deepcopy(server.JOBS)
        with patch.object(server, "create_job", side_effect=AssertionError("Diagnostics cannot start a job")):
            status, response = self.request("GET", "/api/runs/diagnostic-source/diagnostics")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["schema"], "diagnostics-v1")
        self.assertEqual(response["counts"]["total"], 1)
        trial = response["trials"][0]
        duplicate = next(row for row in trial["findings"] if row["code"] == "DUPLICATE_RESERVATION")
        self.assertTrue(duplicate["event_indexes"])
        self.assertEqual(len(duplicate["record_indexes"]), 2)
        self.assertEqual(server.JOBS, before)
        self.assertEqual(original["result"], server.JOBS["diagnostic-source"]["result"])
        self.assertEqual(self.request("GET", "/api/runs/missing/diagnostics")[0], 404)

    def test_retest_preview_is_new_input_only_without_job_or_source_mutation(self):
        self.save_job("retest-source", failed=True)
        before = copy.deepcopy(server.JOBS)
        status, source = self.request("GET", "/api/runs/retest-source/retest-plan")
        self.assertEqual(status, 200, source)
        candidate = next(item for item in source["candidates"] if item["failed"] and item["eligible"])
        with patch.object(server, "create_job", side_effect=AssertionError("Preview cannot start a job")), patch.object(server, "suite_worker", side_effect=AssertionError("Preview cannot execute tools")):
            status, preview = self.request("POST", "/api/retest/prepare", {"run_id": "retest-source", "candidate_ids": [candidate["candidate_id"]]})
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview["suite_id"])
        self.assertEqual(preview["retest_source"]["unique_tasks"], 1)
        self.assertEqual(preview["retest_source"]["source_run_id"], "retest-source")
        plan = server.load_prepared(server.suite_root(), preview["suite_id"])
        self.assertEqual(len(plan["cases"]), 1)
        self.assertEqual(plan["cases"][0]["status"], "ready")
        self.assertEqual(server.JOBS, before)
        self.assertEqual(self.request("POST", "/api/retest/prepare", {"run_id": "retest-source", "candidate_ids": [candidate["candidate_id"]] * 2})[0], 400)
        self.assertEqual(self.request("POST", "/api/retest/prepare", {"run_id": "retest-source", "candidate_ids": ["not-saved"]})[0], 400)

    def test_template_snapshot_mismatch_is_rejected_before_creating_a_job(self):
        created = self.create_template()
        raw = {**created["config"], "quantity": 2, "template_snapshot": created}
        with patch.object(server, "create_job", side_effect=AssertionError("Mismatched snapshot cannot start work")):
            status, response = self.request("POST", "/api/run", raw)
        self.assertEqual(status, 400, response)
        self.assertIn("模板", response["error"])
        self.assertFalse(server.JOBS)
        malformed = copy.deepcopy(created)
        malformed["config"]["api_key"] = "SYNTHETIC_SECRET"
        self.assertEqual(self.request("POST", "/api/run", {**created["config"], "template_snapshot": malformed})[0], 400)

    def test_correct_snapshot_runs_rules_persists_and_adds_separate_contract(self):
        created = self.create_template(contract={"record_count": {"mode": "exactly", "value": 0}, "expected_fields": {}, "stock_delta": None})
        raw = {**created["config"], "template_snapshot": created}
        status, launched = self.request("POST", "/api/run", raw)
        self.assertEqual(status, 202, launched)
        run_id = launched["run_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, job = self.request("GET", "/api/runs/" + run_id)
            if job["status"] != "running":
                break
            time.sleep(0.025)
        self.assertEqual(job["status"], "done", job)
        report = job["result"]
        self.assertEqual(report["template_snapshot"], created)
        self.assertEqual(job["config"]["template_snapshot"], created)
        self.assertFalse(report["baseline"]["failed"])
        self.assertTrue(report["baseline"]["actual_success"])
        self.assertEqual(report["baseline"]["violations"], [])
        self.assertEqual(report["baseline"]["template_contract"]["status"], "fail")
        self.assertEqual(report["fixed"]["template_contract"]["status"], "fail")
        path = "/api/task-templates/" + created["id"]
        self.assertEqual(self.request("PUT", path, self.template_body(name="后续版本", expected_version=1))[0], 200)
        self.join_workers()
        with server.LOCK:
            server.JOBS.pop(run_id)
        status, restored = self.request("GET", "/api/runs/" + run_id)
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored["result"]["template_snapshot"], created)
        self.assertEqual(restored["result"]["baseline"]["template_contract"]["status"], "fail")
        status, exported = self.request("GET", "/api/runs/" + run_id + "/export")
        self.assertEqual(status, 200)
        self.assertEqual(exported["template_snapshot"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
