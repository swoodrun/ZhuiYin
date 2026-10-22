"""Structured HTTP header privacy at upload, storage and download boundaries.

Only synthetic credentials are used. Ordinary business records and free text
are deliberately retained: this policy does not guess secrets from content.
"""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from credentials import sensitive_fields
import ingest
import job_store
import server


SECRET = "STRUCTURED_SYNTHETIC_CREDENTIAL"


def header_evidence():
    return {
        "headers": [
            {"name": "Authorization", "value": "Bearer " + SECRET},
            {"name": "Cookie", "values": ["session=" + SECRET, "other=" + SECRET]},
            {"key": "Proxy-Authorization", "value": SECRET},
            {"Name": "X-API-Key", "Value": SECRET},
            {"name": "Content-Type", "value": "application/json"},
        ],
        "response_headers": [{"name": "Set-Cookie", "value": "session=" + SECRET}],
        "request_id": "keep-request",
        "business": [{"name": "Authorization", "value": "public-business-value"}],
    }


class StructuredCredentialPolicyTests(unittest.TestCase):
    def test_sensitive_records_require_explicit_header_context(self):
        for context in ("headers", "request_headers", "Response-Headers", "HTTP.Headers",
                        "http_request_headers", "httpResponseHeaders", "inbound_headers", "outbound_headers"):
            for descriptor in ("name", "Name", "key", "KEY"):
                with self.subTest(context=context, descriptor=descriptor):
                    record = {descriptor: "Authorization", "value": SECRET, "Values": [SECRET], "request_id": "keep"}
                    expected = {"value", "Values"}
                    if descriptor.lower() == "key":
                        expected.add(descriptor)
                    self.assertEqual(sensitive_fields(record, context), expected)
        for context in (None, "business", "header_notes", "unrelated_headers"):
            with self.subTest(context=context):
                self.assertEqual(sensitive_fields({"name": "Authorization", "value": SECRET}, context), set())

    def test_public_header_values_and_direct_sensitive_policy_are_preserved(self):
        record = {"name": "Content-Type", "value": "application/json", "api_key": SECRET, "idempotency_key": "keep"}
        self.assertEqual(sensitive_fields(record, "headers"), {"api_key"})
        self.assertEqual(sensitive_fields({"key": "Content-Type", "value": "text/plain"}, "headers"), {"key"})
        for name in (None, ["Authorization"], {"name": "Cookie"}, 7, False):
            self.assertEqual(sensitive_fields({"name": name, "value": "ordinary"}, "headers"), set())

    def test_nested_lists_preserve_context_and_business_data_without_mutation(self):
        evidence = {"observed": header_evidence(), "headers": [[{"name": "Cookie", "value": SECRET}]]}
        original = copy.deepcopy(evidence)
        for cleaner in (job_store.sanitize, job_store.compact_snapshot):
            with self.subTest(cleaner=cleaner.__name__):
                result = cleaner(evidence)
                self.assertNotIn(SECRET, json.dumps(result))
                self.assertEqual(result["observed"]["business"], original["observed"]["business"])
                self.assertEqual(result["observed"]["headers"][-1]["value"], "application/json")
                self.assertNotIn("value", result["headers"][0][0])
        self.assertEqual(evidence, original)

    def test_nearest_dictionary_key_ends_header_record_context(self):
        # A business payload nested below a differently named dictionary is not
        # itself a header record, even if an ancestor was named headers.
        value = {"headers": {"business": {"name": "Authorization", "value": "public"}}}
        self.assertEqual(job_store.sanitize(value), value)
        self.assertEqual(job_store.compact_snapshot(value), value)
        free_text = {"message": "Authorization: " + SECRET}
        self.assertEqual(job_store.sanitize(free_text), free_text)

    def test_upload_masks_structured_values_and_keeps_raw_file_immutable(self):
        evidence = header_evidence()
        original = copy.deepcopy(evidence)
        warnings = []
        redacted = ingest._redact({"observed": evidence}, warnings)
        self.assertNotIn(SECRET, json.dumps(redacted))
        self.assertEqual(evidence, original)
        self.assertEqual(redacted["observed"]["headers"][0]["value"], ingest.REDACTED)
        self.assertEqual(redacted["observed"]["headers"][1]["values"], ingest.REDACTED)
        self.assertEqual(redacted["observed"]["business"], evidence["business"])
        self.assertEqual(len(warnings), 1)


class StructuredCredentialFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-header-privacy-", dir=Path(__file__).resolve().parent)
        self.data = Path(self.temporary.name)
        self.patches = [patch.object(server, "DATA", self.data), patch.object(server, "JOBS", {}),
                        patch.object(server, "STOP_EVENTS", {}), patch.object(server, "STORES", {})]
        for replacement in self.patches:
            replacement.start()
        self.store = server.store()

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
        return handler

    def get(self, path):
        handler = self.handler(path)
        handler.do_GET()
        return handler

    def assert_private(self, value):
        rendered = value.decode("utf-8") if isinstance(value, bytes) else json.dumps(value)
        self.assertNotIn(SECRET, rendered)

    def test_upload_report_snapshot_and_exports_strip_header_credentials(self):
        evidence = {
            "kind": "trace", "name": "结构化凭据边界检查",
            "task": {"request_id": "keep-request"},
            "events": [{"step": 1, "tool": "create_ticket", "request_id": "keep-request",
                        "observed": header_evidence(), "actual": {"committed": True, "ticket_id": "safe-ticket"}}],
            "final_state": {"effects": [{"effect_id": "safe-ticket", "request_id": "keep-request", "status": "active"}]},
        }
        content = json.dumps(evidence)
        post = self.handler("/api/import")
        post.read_json = lambda **kwargs: {"filename": "synthetic-headers.json", "content": content}
        post.do_POST()
        status, response = post.json.call_args.args
        self.assertEqual(status, 200, response)
        self.assert_private(response)
        run_id = response["import_id"]
        report_file = self.data / run_id / "report.json"
        self.assert_private(report_file.read_bytes())
        self.assert_private(server.JOBS[run_id])
        self.assert_private(self.store.get(run_id))
        with closing(sqlite3.connect(self.store.path)) as database:
            row = database.execute("SELECT snapshot,metadata FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        self.assert_private("".join(row))
        for restarted in (False, True):
            if restarted:
                server.JOBS.clear()
            with self.subTest(restarted=restarted):
                for path in (f"/api/runs/{run_id}", f"/api/runs/{run_id}?lite=1", "/api/history"):
                    request = self.get(path)
                    self.assertEqual(request.json.call_args.args[0], 200)
                    self.assert_private(request.json.call_args.args[1])
                export = self.get(f"/api/runs/{run_id}/export")
                self.assertEqual(export.send_bytes.call_args.args[0], 200)
                self.assert_private(export.send_bytes.call_args.args[1])
        self.assertIn(SECRET, content)
        self.assertEqual(json.loads(content), evidence)

    def test_old_snapshot_metadata_and_report_are_sanitized_on_read_not_rewritten(self):
        run_id = "old-header-format"
        self.store.save({"run_id": run_id, "status": "done", "result": {"visible": True}})
        unsafe_report = {"kind": "trace", "observed": header_evidence()}
        report_dir = self.data / run_id
        report_dir.mkdir()
        report_file = report_dir / "report.json"
        raw_report = json.dumps(unsafe_report)
        report_file.write_text(raw_report, encoding="utf-8")
        unsafe_snapshot = {"run_id": run_id, "status": "done", "result": unsafe_report}
        unsafe_metadata = {"run_id": run_id, "status": "done", "has_result": True, "events": [],
                           "config": {"request_headers": [{"name": "Authorization", "value": SECRET}]}}
        snapshot, metadata = json.dumps(unsafe_snapshot), json.dumps(unsafe_metadata)
        with closing(self.store.connect()) as database:
            database.execute("UPDATE jobs SET snapshot=?,metadata=? WHERE run_id=?", (snapshot, metadata, run_id))
            database.execute("UPDATE job_status SET metadata=? WHERE run_id=?", (metadata, run_id))
            database.commit()
        self.assert_private(self.store.get(run_id))
        self.assert_private(self.store.get(run_id, lite=True))
        self.assert_private(self.store.list())
        self.assert_private(self.get(f"/api/runs/{run_id}").json.call_args.args[1])
        export = self.get(f"/api/runs/{run_id}/export")
        self.assertEqual(export.send_bytes.call_args.args[0], 200)
        self.assert_private(export.send_bytes.call_args.args[1])
        self.assertEqual(report_file.read_text(encoding="utf-8"), raw_report)
        with closing(self.store.connect()) as database:
            row = database.execute("SELECT snapshot,metadata FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual((row["snapshot"], row["metadata"]), (snapshot, metadata))


if __name__ == "__main__":
    unittest.main()
