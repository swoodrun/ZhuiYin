"""Regression cases for truthful deletion outcomes and credential-free provenance."""
import hashlib
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import engine
import study


class DeletionOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(vars(engine.MODEL_CONTEXT))
        vars(engine.MODEL_CONTEXT).clear()

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.previous)

    def test_three_fault_example_reports_different_business_errors(self):
        faults = ["commit_response_lost", "stale_read", "corrupt_payload"]
        config = engine.Config(faults=faults)
        reduced = engine.minimize(config, faults)
        removed_corruption = next(step for step in reduced["steps"] if step["removed_fault"] == "corrupt_payload")
        self.assertFalse(removed_corruption["same_error_reproduced"])
        self.assertFalse(removed_corruption["failed"])  # Historical target-match semantics.
        self.assertEqual(removed_corruption["other_error_count"], 2)
        self.assertEqual(removed_corruption["no_error_count"], 0)
        self.assertTrue(all(removed_corruption["fingerprints"]))
        for step in reduced["steps"]:
            self.assertEqual(sum(step["outcome_counts"].values()), step["attempted"])
            self.assertEqual(step["same_error_count"], step["reproduced"])
            self.assertEqual(step["same_error_reproduced"], step["failed"])
            for fingerprint in step["fingerprints"]:
                if fingerprint == reduced["failure_fingerprint"]:
                    self.assertTrue(step["same_error_reproduced"])
                else:
                    self.assertGreater(step["other_error_count"] + step["no_error_count"] + step["infra_error_count"], 0)

    def test_no_error_and_incomplete_are_not_combined(self):
        target = {"failed": True, "failure_fingerprint": "BUSINESS_ERROR"}
        clean = {"failed": False, "failure_fingerprint": "", "reported_success": True}
        incomplete = {"failed": True, "failure_fingerprint": "AGENT_DID_NOT_FINISH", "reported_success": None}
        with patch.object(engine, "run_trial", side_effect=[target, target, clean, incomplete]):
            reduced = engine.minimize(engine.Config(faults=["latency"]), ["latency"])
        step = reduced["steps"][0]
        self.assertEqual(step["outcome_counts"], {"same_error": 0, "other_error": 0, "no_error": 1, "infra_error": 1})
        self.assertEqual(step["attempted"], 2)
        self.assertFalse(step["completed"])
        self.assertFalse(step["accepted"])
        self.assertEqual(reduced["faults"], ["latency"])

    def test_mixed_target_and_other_error_retains_both_counts(self):
        target = {"failed": True, "failure_fingerprint": "BUSINESS_ERROR"}
        other = {"failed": True, "failure_fingerprint": "DIFFERENT_ERROR"}
        with patch.object(engine, "run_trial", side_effect=[target, target, target, other, target, target]):
            reduced = engine.minimize(engine.Config(faults=["latency"]), ["latency"])
        step = reduced["steps"][0]
        self.assertTrue(step["same_error_reproduced"])
        self.assertEqual((step["same_error_count"], step["other_error_count"]), (1, 1))
        self.assertFalse(step["accepted"])
        self.assertEqual(step["fingerprints"], ["BUSINESS_ERROR", "DIFFERENT_ERROR"])

    def test_explicit_missing_report_is_an_incomplete_trial(self):
        self.assertTrue(engine._trial_incomplete({"reported_success": None}))
        self.assertTrue(engine._trial_incomplete({"status": "not_run"}))
        self.assertFalse(engine._trial_incomplete({"failed": True, "failure_fingerprint": "CUSTOM_MODEL_EXECUTION_ERROR_TEXT"}))


class ReportProvenanceTests(unittest.TestCase):
    def test_single_and_study_share_recheckable_safe_provenance(self):
        config = engine.Config(faults=[], model_config={"api_key": "FAKE_PRIVATE_KEY", "base_url": "https://example.test/private"})
        experiment = engine.run_experiment(config)
        repeated = study.run_study(config, {"repetitions": 2, "strategies": ["baseline"]})
        self.assertEqual(experiment["mode"], "rules")
        self.assertIsNone(experiment["model_identity"])
        self.assertEqual(experiment["provenance"], repeated["provenance"])
        provenance = experiment["provenance"]
        self.assertEqual(provenance["app_version"], engine.APP_VERSION)
        self.assertEqual(provenance["prompt_version"], engine.PROMPT_VERSION)
        source_root = Path(engine.__file__).parent
        for name, digest in provenance["source_sha256"].items():
            self.assertEqual(digest, hashlib.sha256((source_root / name).read_bytes()).hexdigest())
        canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(provenance["source_tree_sha256"], hashlib.sha256(canonical(provenance["source_sha256"])).hexdigest())
        self.assertEqual(provenance["public_config_sha256"], hashlib.sha256(canonical(config.public())).hexdigest())
        definitions = inspect.getsource(engine.agent_spec) + inspect.getsource(engine.llm_agent)
        self.assertEqual(provenance["prompt_definition_sha256"], hashlib.sha256(definitions.encode("utf-8")).hexdigest())
        self.assertNotIn("FAKE_PRIVATE_KEY", json.dumps(experiment))
        self.assertNotIn("example.test/private", json.dumps(experiment))

    def test_single_model_report_keeps_only_safe_name_and_origin(self):
        config = engine.Config(agent_mode="llm", faults=[], model_config={"model": "synthetic-only",
            "base_url": "https://account:FAKE_PASSWORD@example.test:8443/private/v1?key=FAKE_QUERY#fragment",
            "api_key": "FAKE_PRIVATE_KEY"})
        actual = engine.run_trial
        def offline_trial(config, faults, strategy="baseline", agent_mode="rules", model_config=None):
            return actual(config, faults, strategy, agent_mode="rules")
        with patch.object(engine, "run_trial", side_effect=offline_trial):
            report = engine.run_experiment(config)
        self.assertEqual(report["model_identity"], {"model": "synthetic-only", "api_origin": "https://example.test:8443"})
        serialized = json.dumps(report)
        for private in ("FAKE_PASSWORD", "FAKE_QUERY", "FAKE_PRIVATE_KEY", "/private/v1", "fragment"):
            self.assertNotIn(private, serialized)
        self.assertIs(study._model_identity, engine.model_identity)
        self.assertFalse(report["model_usage"]["external_model_verified"])


if __name__ == "__main__":
    unittest.main()
