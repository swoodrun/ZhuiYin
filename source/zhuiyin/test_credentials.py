"""Credential policy regression checks use synthetic markers only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from credentials import is_sensitive_key
import ingest
import job_store


SENSITIVE_FIELDS = (
    "api_key", "key", "Authorization", "Proxy-Authorization", "password", "passwd",
    "access_token", "refresh_token", "app_token", "auth_token", "token", "secret",
    "client_secret", "model_config",
    "Cookie", "Set-Cookie", "private_key", "credential", "credentials",
    "provider_api_key", "database_password", "oauth_access_token", "oauth_refresh_token",
    "oauth_client_secret",
)
BUSINESS_FIELDS = (
    "request_id", "model_identity", "idempotency_key", "operation_key", "business_key",
    "ticket_id", "model", "equipment", "stock", "quantity", "token_count", "total_tokens",
    "completion_tokens", "prompt_tokens", "result_revision",
)


class CredentialPolicyTests(unittest.TestCase):
    def test_upload_and_persistence_share_one_classifier(self):
        self.assertIs(ingest._sensitive, is_sensitive_key)
        self.assertIs(job_store.is_sensitive_key, is_sensitive_key)

    def test_case_and_separator_variants_keep_all_established_sensitive_names(self):
        for field in SENSITIVE_FIELDS:
            for variant in (field, field.upper(), field.replace("_", ".- "), field.replace("-", "_")):
                with self.subTest(field=variant):
                    self.assertTrue(is_sensitive_key(variant))
                    self.assertEqual(ingest._redact({variant: "synthetic-secret"}, []),
                                     {variant: ingest.REDACTED})
                    self.assertEqual(job_store.sanitize({variant: "synthetic-secret"}), {})

    def test_business_identifiers_and_measurements_survive_both_boundaries(self):
        payload = {field: f"public-{field}" for field in BUSINESS_FIELDS}
        for field in BUSINESS_FIELDS:
            with self.subTest(field=field):
                self.assertFalse(is_sensitive_key(field))
        self.assertEqual(ingest._redact(payload, []), payload)
        self.assertEqual(job_store.sanitize(payload), payload)

    def test_app_and_auth_tokens_are_explicit_names_not_wildcard_token_suffixes(self):
        sensitive = ("app_token", "APP-TOKEN", "App.Token", "auth_token", "AUTH-TOKEN", "Auth.Token")
        ordinary = ("my_secret", "secret_key", "prompt_token", "custom_token", "token_count")
        payload = {field: "synthetic-secret" for field in sensitive}
        payload.update({field: f"public-{field}" for field in ordinary})
        untouched = dict(payload)

        redacted = ingest._redact(payload, [])
        sanitized = job_store.sanitize(payload)

        self.assertEqual(payload, untouched)
        for field in sensitive:
            with self.subTest(field=field):
                self.assertTrue(is_sensitive_key(field))
                self.assertEqual(redacted[field], ingest.REDACTED)
                self.assertNotIn(field, sanitized)
        for field in ordinary:
            with self.subTest(field=field):
                self.assertFalse(is_sensitive_key(field))
                self.assertEqual(redacted[field], payload[field])
                self.assertEqual(sanitized[field], payload[field])

    def test_non_string_keys_do_not_break_internal_snapshot_sanitization(self):
        for field in (None, 3, False, ("api_key",)):
            with self.subTest(field=field):
                self.assertFalse(is_sensitive_key(field))

    def test_nested_lists_are_scrubbed_without_mutating_source_evidence(self):
        payload = {field: f"synthetic-secret-{index}" for index, field in enumerate(SENSITIVE_FIELDS)}
        payload["request_id"] = "keep-request"
        original = {"events": [{"actual": {"nested": [payload]}}]}
        for _ in range(12):
            original = {"layer": [original]}
        untouched = json.dumps(original)
        warnings = []
        redacted = ingest._redact(original, warnings)
        sanitized = job_store.sanitize(original)
        self.assertEqual(json.dumps(original), untouched)
        for result in (redacted, sanitized):
            rendered = json.dumps(result)
            self.assertNotIn("synthetic-secret-", rendered)
            self.assertIn("keep-request", rendered)
        self.assertEqual(len(warnings), 1)
        self.assertIn(ingest.REDACTED, json.dumps(redacted, ensure_ascii=False))
        self.assertNotIn("private_key", json.dumps(sanitized))

    def test_trace_upload_then_persistence_preserves_evidence_but_never_credentials(self):
        evidence = {
            "kind": "trace", "name": "合成凭据边界测试",
            "task": {"request_id": "safe-request", "goal": "检查上传与落盘边界"},
            "events": [{"step": 1, "tool": "create_ticket", "request_id": "safe-request",
                        "observed": {"ok": True, "headers": {"Cookie": "synthetic-secret-cookie"}},
                        "actual": {"committed": True, "ticket_id": "ticket-safe",
                                   "details": [{"key": "synthetic-secret-key", "model_identity": "model-safe"}]}}],
            "final_state": {"effects": [{"effect_id": "ticket-safe", "request_id": "safe-request", "status": "active"}]},
        }
        parsed = ingest.parse_upload("synthetic.json", json.dumps(evidence))
        self.assertNotIn("synthetic-secret", json.dumps(parsed))
        self.assertEqual(parsed["analyses"][0]["events"][0]["actual"]["details"][0]["model_identity"], "model-safe")
        with tempfile.TemporaryDirectory(prefix="test-credentials-", dir=Path(__file__).resolve().parent) as folder:
            store = job_store.JobStore(Path(folder) / "history.sqlite")
            store.save({"run_id": "credential-check", "kind": "import", "status": "done", "result": parsed,
                        "config": {"key": "synthetic-secret-persist", "request_id": "safe-request"}})
            restored = store.get("credential-check")
            self.assertEqual(restored["config"], {"request_id": "safe-request"})
            with closing(sqlite3.connect(store.path)) as database:
                row = database.execute("SELECT snapshot,metadata FROM jobs WHERE run_id=?", ("credential-check",)).fetchone()
            self.assertNotIn("synthetic-secret", "".join(row))
            self.assertNotIn('"Cookie"', row[0])
            self.assertNotIn('"key"', row[0])
            self.assertIn("model-safe", row[0])


if __name__ == "__main__":
    unittest.main()
