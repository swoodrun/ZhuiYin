"""Loopback-only one-shot credential intake; synthetic keys, no provider calls."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tools import eight_arm_launcher as launcher


class EightArmLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="eight-arm-launcher-test-", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.prepared = self.root / "prepared"
        self.prepared.mkdir()
        self.output = self.root / "output"
        self.policy_path = self.root / "policy.json"
        self.ledger = self.root / "ledger.sqlite"
        policy = {"ledger_path": str(self.ledger), "max_requests": 374, "max_cost_cny": "10"}
        policy_json = json.dumps(policy, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(policy_json.encode()).hexdigest()
        self.policy_path.write_text(policy_json, encoding="utf-8")
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO metadata VALUES(?,?)", [("policy_json", policy_json),
                                                               ("policy_sha256", checksum)])
            db.execute("CREATE TABLE requests(sequence INTEGER PRIMARY KEY, charged_micros INTEGER)")
            db.executemany("INSERT INTO requests VALUES(?,?)", [(i, 612582 if i == 1 else 0) for i in range(1, 231)])
            db.commit()
        self.design = {"worst_case_requests": 144, "fixture": "synthetic_plan_not_model_evidence"}
        (self.prepared / "fixed-design.json").write_text(json.dumps(self.design), encoding="utf-8")
        (self.prepared / "design-sha256.json").write_text(json.dumps({
            "sha256": hashlib.sha256(launcher.experiment.canonical(self.design)).hexdigest()}), encoding="utf-8")
        self.budget = {"policy_sha256": checksum, "policy_consistent": True, "max_requests": 374,
                       "requests_made": 230, "requests_remaining": 144, "remaining_cost_cny": "9.387418",
                       "halted_reason": None}
        with patch.object(launcher.experiment, "fixed_design", return_value=self.design), \
                patch.object(launcher.experiment, "readonly_budget", return_value=self.budget):
            self.state = launcher.LaunchState(self.prepared, self.output, self.policy_path)
        self.server = launcher.ThreadingHTTPServer(("127.0.0.1", 0), launcher.Handler)
        self.server.state = self.state
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = self.server.server_port
        self.origin = "http://127.0.0.1:" + str(self.port)
        self.release = threading.Event()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        if self.state.started:
            self.wait_finished()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        self.temporary.cleanup()

    def wait_finished(self):
        deadline = time.monotonic() + 5
        while not self.state.finished and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.state.finished, "synthetic executor did not finish")

    def request(self, method, path, body=None, headers=None):
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        supplied = {"Content-Type": "application/json", "Origin": self.origin} if method == "POST" else {}
        supplied.update(headers or {})
        for key in [name for name, value in supplied.items() if value is None]:
            del supplied[key]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def payload(self, key):
        return {"api_key": key, "nonce": self.state.nonce}

    def assert_secret_absent(self, secret, *values):
        for value in values:
            self.assertNotIn(secret, str(value))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_concurrent_posts_start_executor_once_and_public_state_never_contains_key(self):
        secret = "synthetic-launcher-key-never-send-to-provider"
        entered = threading.Event()
        responses, failures = [], []
        barrier = threading.Barrier(2)
        stream = io.StringIO()

        def fake_execute(prepared, output, mode, **kwargs):
            self.assertEqual((prepared, output, mode), (self.prepared, self.output, "llm"))
            self.assertEqual(kwargs, {"policy_path": self.policy_path, "execute_real_model": True})
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], secret)
            self.assertEqual(os.environ["ZHUIYIN_CAMPAIGN_POLICY"], "worker-policy-sentinel")
            output.mkdir()
            (output / "study-report.json").write_text(json.dumps({
                "samples": [{"status": "valid"}, {"status": "infra_error"}]}), encoding="utf-8")
            entered.set()
            self.assertTrue(self.release.wait(4))
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], secret)
            return {"status": "finished"}

        def post():
            try:
                barrier.wait(timeout=3)
                responses.append(self.request("POST", "/api/start", self.payload(secret)))
            except BaseException as error:
                failures.append(type(error).__name__)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "prior-runtime-key",
                                     "ZHUIYIN_CAMPAIGN_POLICY": "worker-policy-sentinel"}), \
                patch.object(launcher.experiment, "execute", side_effect=fake_execute) as execute, \
                redirect_stdout(stream), redirect_stderr(stream):
            posters = [threading.Thread(target=post) for _ in range(2)]
            for poster in posters:
                poster.start()
            for poster in posters:
                poster.join(timeout=4)
                self.assertFalse(poster.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(sorted(response[0] for response in responses), [202, 409])
            self.assertTrue(entered.wait(2))
            page = self.request("GET", "/")
            status = self.request("GET", "/api/status")
            self.assertEqual((page[0], status[0]), (200, 200))
            public = json.loads(status[2])
            self.assertEqual((public["attempted"], public["valid"], public["infra_errors"]), (2, 1, 1))
            self.assertEqual(public["budget"], {"requests_made": 230, "max_requests": 374,
                                                 "accounted_cost_cny": "0.612582"})
            self.assertEqual(page[1]["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", page[1]["Content-Security-Policy"])
            self.assertEqual(os.environ["ZHUIYIN_CAMPAIGN_POLICY"], "worker-policy-sentinel")
            self.release.set()
            self.wait_finished()
            execute.assert_called_once()
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "prior-runtime-key")
            self.assertEqual(os.environ["ZHUIYIN_CAMPAIGN_POLICY"], "worker-policy-sentinel")
            self.assertEqual(self.state.phase, "finished")
        self.assert_secret_absent(secret, responses, page, status, stream.getvalue(), vars(self.state))

    def test_executor_exception_cannot_reopen_run_and_environment_is_restored(self):
        secret = "synthetic-error-key-must-never-appear"
        stream = io.StringIO()
        with patch.dict(os.environ, {"ZHUIYIN_CAMPAIGN_POLICY": "prior-policy"}), \
                patch.object(launcher.experiment, "execute", side_effect=RuntimeError(secret)) as execute, \
                redirect_stdout(stream), redirect_stderr(stream):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            first = self.request("POST", "/api/start", self.payload(secret))
            self.wait_finished()
            second = self.request("POST", "/api/start", self.payload(secret))
            status = self.request("GET", "/api/status")
            self.assertEqual((first[0], second[0], status[0]), (202, 409, 200))
            self.assertEqual(json.loads(status[2])["phase"], "interrupted")
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
            self.assertEqual(os.environ["ZHUIYIN_CAMPAIGN_POLICY"], "prior-policy")
            execute.assert_called_once()
        self.assert_secret_absent(secret, first, second, status, stream.getvalue(), vars(self.state))

    def test_invalid_origin_host_nonce_body_or_size_is_rejected_before_executor(self):
        secret = "synthetic-rejected-key"
        cases = [
            (self.payload(secret), {"Host": "outside.invalid"}, "/api/start", 403),
            (self.payload(secret), {"Origin": "https://outside.invalid"}, "/api/start", 403),
            (self.payload(secret), {"Origin": None}, "/api/start", 403),
            ({"api_key": secret, "nonce": "wrong"}, {}, "/api/start", 403),
            ({"api_key": secret, "nonce": 42}, {}, "/api/start", 400),
            ({**self.payload(secret), "extra": secret}, {}, "/api/start", 400),
            ([self.payload(secret)], {}, "/api/start", 400),
            (self.payload(secret), {"Content-Type": "text/plain"}, "/api/start", 400),
            ("x" * 4097, {}, "/api/start", 400),
            ({"api_key": " ", "nonce": self.state.nonce}, {}, "/api/start", 400),
            ({"api_key": "x" * 2049, "nonce": self.state.nonce}, {}, "/api/start", 400),
            (self.payload(secret), {}, "/api/unknown", 404),
            ("{invalid-json", {}, "/api/start", 400),
            (self.payload(secret), {"Content-Length": "0"}, "/api/start", 400),
            (self.payload(secret), {"Content-Length": "-1"}, "/api/start", 400),
        ]
        with patch.object(launcher.experiment, "execute") as execute:
            for body, headers, path, expected in cases:
                with self.subTest(path=path, expected=expected, headers=headers):
                    result = self.request("POST", path, body, headers)
                    self.assertEqual(result[0], expected)
                    self.assertNotIn(secret, result[2])
            self.assertEqual(self.request("GET", "/", headers={"Host": "outside.invalid"})[0], 403)
            self.assertEqual(self.request("GET", "/api/status", headers={"Origin": "https://outside.invalid"})[0], 403)
            self.assertEqual(self.request("GET", "/unknown")[0], 404)
            execute.assert_not_called()
        self.assertFalse(self.state.started)
        self.assertFalse(self.output.exists())
        self.assert_secret_absent(secret, self.request("GET", "/"), self.request("GET", "/api/status"))

    def test_public_polling_reads_ledger_without_using_environment_or_budget_loader(self):
        before = self.ledger.read_bytes()
        with patch.dict(os.environ, {"ZHUIYIN_CAMPAIGN_POLICY": "live-worker-policy"}), \
                patch.object(launcher.experiment, "campaign_environment", side_effect=AssertionError("must not switch policy")), \
                patch.object(launcher.experiment, "readonly_budget", side_effect=AssertionError("must not invoke loader")):
            for _ in range(3):
                response = self.request("GET", "/api/status")
                self.assertEqual(response[0], 200)
                self.assertEqual(json.loads(response[2])["budget"]["requests_made"], 230)
                self.assertEqual(os.environ["ZHUIYIN_CAMPAIGN_POLICY"], "live-worker-policy")
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_ledger_policy_change_returns_safe_unavailable_response(self):
        secret = "synthetic-injected-policy-error-text"
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute("UPDATE metadata SET value=? WHERE key='policy_json'", (secret,))
            db.commit()
        response = self.request("GET", "/api/status")
        self.assertEqual(response[0], 503)
        self.assertNotIn(secret, response[2])
        self.assertFalse(self.state.started)


if __name__ == "__main__":
    unittest.main()
