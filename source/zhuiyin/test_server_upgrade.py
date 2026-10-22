"""Integration contracts for local concurrency jobs and completed artifacts."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import engine
from ingest import parse_upload
from job_store import JobStore
import server


class UpgradeServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-upgrade-", dir=Path(__file__).resolve().parent)
        self.data = Path(self.temp.name)
        self.patches = [patch.object(server, "DATA", self.data), patch.object(server, "JOBS", {}),
                        patch.object(server, "STOP_EVENTS", {}), patch.object(server, "STORES", {})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def handler(self, path, body=None):
        value = server.Handler.__new__(server.Handler)
        value.headers = {"Host": "127.0.0.1"}
        value.path = path
        value.server = SimpleNamespace(server_port=8766)
        value.json, value.send_bytes = Mock(), Mock()
        value.read_json = lambda **kwargs: body or {}
        return value

    def get(self, path):
        value = self.handler(path)
        value.do_GET()
        return value

    def test_health_and_new_scripts_are_available(self):
        status, health = self.get("/api/health").json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(health["version"], engine.APP_VERSION)
        self.assertTrue(health["concurrency"])
        for path in ("/concurrency.js", "/outcome.js", "/history.js"):
            self.assertEqual(self.get(path).send_bytes.call_args.args[0], 200)

    def test_batch_tutorial_downloads_are_parseable_public_practice_templates(self):
        bundle = self.get("/api/templates/batch-example.zip").send_bytes.call_args.args
        self.assertEqual(bundle[0], 200)
        self.assertEqual(bundle[2:], ("application/zip", "batch-example.zip"))
        with zipfile.ZipFile(io.BytesIO(bundle[1])) as archive:
            self.assertEqual(set(archive.namelist()), {"设备场景.csv", "工单场景.csv", "制作说明.md"})
            self.assertTrue(archive.read("制作说明.md").decode("utf-8-sig").strip())
            total = 0
            for download_name, member_name, expected in (
                    ("batch-equipment.csv", "设备场景.csv", 3),
                    ("batch-tickets.csv", "工单场景.csv", 2)):
                with self.subTest(template=download_name):
                    content = self.get("/api/templates/" + download_name).send_bytes.call_args.args
                    self.assertEqual(content[0], 200)
                    self.assertEqual(content[2:], ("text/csv; charset=utf-8", download_name))
                    self.assertEqual(content[1], archive.read(member_name))
                    parsed = parse_upload(download_name, content[1].decode("utf-8-sig"))
                    self.assertEqual(parsed["kind"], "scenarios")
                    self.assertEqual(len(parsed["scenarios"]), expected)
                    total += len(parsed["scenarios"])
            self.assertEqual(total, 5)
        self.assertEqual(self.get("/api/templates/unknown-batch-template.zip").json.call_args.args[0], 404)

    def test_concurrency_post_uses_effective_rules_config_and_validates_before_dispatch(self):
        handler = self.handler("/api/concurrency", {"config": {"faults": list(engine.DEFAULT_FAULTS)}})
        with patch.object(server.threading, "Thread") as thread:
            handler.do_POST()
        status, body = handler.json.call_args.args
        self.assertEqual(status, 202)
        self.assertEqual(server.get_job(body["run_id"], lite=True)["config"]["faults"], [])
        self.assertIs(thread.call_args.kwargs["target"], server.concurrency_worker)
        self.assertEqual(thread.call_args.kwargs["args"][1].agent_mode, "rules")
        for config in ({"agent_mode": "llm"}, {"model_config": {"api_key": "synthetic-secret"}}):
            invalid = self.handler("/api/concurrency", {"config": config})
            with patch.object(server.threading, "Thread") as other:
                invalid.do_POST()
            self.assertEqual(invalid.json.call_args.args[0], 400)
            other.assert_not_called()

    def test_completed_concurrency_survives_restart_and_downloads_only_actual_databases(self):
        cfg = engine.Config(model_config={"api_key": "synthetic-secret"})
        run_id = server.create_job("concurrency", cfg.public())
        server.concurrency_worker(run_id, cfg)
        job = server.get_job(run_id)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["result"]["completed"], 8)
        self.assertEqual(cfg.model_config, {})
        self.assertNotIn("synthetic-secret", json.dumps(job))
        self.assertNotIn("result", server.get_job(run_id, lite=True))
        server.JOBS.clear()
        restored = server.get_job(run_id)
        self.assertEqual(restored["result"]["kind"], "concurrency")
        for case in restored["result"]["cases"]:
            self.assertEqual(case["database_url"], f"/api/runs/{run_id}/{case['database_filename']}")
            download = self.get(case["database_url"])
            status, content = download.send_bytes.call_args.args[:2]
            self.assertEqual(status, 200)
            self.assertTrue(content.startswith(b"SQLite format 3"))
        exported = self.get(f"/api/runs/{run_id}/export").send_bytes.call_args.args
        self.assertEqual(json.loads(exported[1])["completed"], 8)

    def test_interrupted_job_keeps_completed_downloads_but_blocks_unfinished_database(self):
        cfg = engine.Config()
        run_id = server.create_job("concurrency", cfg.public())
        checkpoint = {"kind": "concurrency", "status": "partial", "cases": [
            {"schedule": "sequential", "strategy": "baseline", "database_filename": "sequential-baseline.sqlite"}],
            "completed": 1, "planned": 8, "limitations": []}
        def interrupted(config, out, **callbacks):
            (out / "sequential-baseline.sqlite").write_bytes(b"completed fixture")
            (out / "concurrent-verified.sqlite").write_bytes(b"unfinished fixture")
            callbacks["on_checkpoint"](checkpoint)
            raise OSError("private-synthetic-path")
        with redirect_stderr(io.StringIO()), patch.object(server, "run_concurrency_suite", side_effect=interrupted):
            server.concurrency_worker(run_id, cfg)
        self.assertNotIn("database_url", checkpoint["cases"][0])
        server.JOBS.clear()
        job = server.get_job(run_id)
        self.assertEqual(job["status"], "error")
        self.assertNotIn("private-synthetic-path", json.dumps(job))
        link = job["partial_result"]["cases"][0]["database_url"]
        self.assertEqual(self.get(link).send_bytes.call_args.args[0], 200)
        blocked = self.get(f"/api/runs/{run_id}/concurrent-verified.sqlite")
        self.assertEqual(blocked.json.call_args.args[0], 404)

    def test_cancelled_concurrency_exports_zero_completed_not_fake_databases(self):
        cfg = engine.Config()
        run_id = server.create_job("concurrency", cfg.public())
        server.STOP_EVENTS[run_id].set()
        server.concurrency_worker(run_id, cfg)
        job = server.get_job(run_id)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["result"]["cases"], [])
        self.assertEqual(job["result"]["stopped_reason"], "cancelled")
        self.assertEqual(list((self.data / run_id).glob("*.sqlite")), [])

    def test_legacy_concurrency_report_retains_kind(self):
        directory = self.data / "legacy-concurrency"
        directory.mkdir()
        (directory / "report.json").write_text(json.dumps({"kind": "concurrency", "status": "finished", "cases": []}), encoding="utf-8")
        store = JobStore(self.data / "history.sqlite")
        self.assertEqual(store.import_legacy_reports(self.data), 1)
        self.assertEqual(store.get(directory.name)["kind"], "concurrency")


if __name__ == "__main__":
    unittest.main()
