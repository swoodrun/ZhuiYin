"""Protocol distinctions and the independent final minimization check."""
import json
import unittest
from unittest.mock import patch

import engine


class EvaluationProtocolTests(unittest.TestCase):
    def trial(self, outcome, write=True, faults=()):
        lab = engine.Lab(engine.Config(), list(faults), "baseline")
        try:
            if write:
                lab.reserve()
            if outcome is not None:
                lab.finish(outcome=outcome, reason="protocol fixture")
            return lab.result("llm")
        finally:
            lab.db.close()

    def test_uncertain_report_is_completed_and_neither_false_nor_mismatch(self):
        result = self.trial("unknown")
        self.assertTrue(result["completed"])
        self.assertTrue(result["actual_success"])
        self.assertIsNone(result["reported_success"])
        self.assertEqual(result["reported_outcome"], "unknown")
        self.assertEqual(result["report_assessment"], "uncertain")
        self.assertFalse(result["failed"])
        self.assertFalse(engine._trial_incomplete(result))

    def test_unknown_does_not_erase_business_failure(self):
        result = self.trial("unknown", write=False)
        self.assertTrue(result["business_failed"])
        self.assertFalse(result["report_failed"])
        self.assertEqual(result["failure_fingerprint"], "TASK_INCOMPLETE")
        self.assertTrue(result["completed"])

    def test_asserted_failure_on_completed_task_is_a_report_error_only(self):
        result = self.trial("failure")
        self.assertTrue(result["actual_success"])
        self.assertFalse(result["business_failed"])
        self.assertTrue(result["report_failed"])
        self.assertEqual(result["failure_fingerprint"], "REPORT_MISMATCH")

    def test_absent_report_is_still_incomplete_and_legacy_null_is_not_unknown(self):
        result = self.trial(None)
        self.assertFalse(result["completed"])
        self.assertEqual(result["report_assessment"], "unreported")
        self.assertTrue(engine._trial_incomplete(result))
        self.assertTrue(engine._trial_incomplete({"reported_success": None}))

    def test_bad_payload_and_honest_failure_have_no_reporting_error(self):
        result = self.trial("failure", faults=["corrupt_payload"])
        self.assertTrue(result["business_failed"])
        self.assertFalse(result["report_failed"])
        self.assertEqual(result["report_assessment"], "consistent")
        self.assertIn("PAYLOAD_MISMATCH", result["failure_fingerprint"])

    def test_invalid_or_conflicting_report_rejected_before_mutation(self):
        lab = engine.Lab(engine.Config(), [], "baseline")
        try:
            for args in ({"outcome": "maybe"}, {"success": "false"},
                         {"outcome": "unknown", "success": False},
                         {"outcome": "failure", "success": True}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    lab.finish(**args)
                self.assertIsNone(lab.reported_outcome)
        finally:
            lab.db.close()

    def test_policies_have_identical_system_text_across_capability_arms(self):
        texts = {}
        for scenario in ("equipment", "ticket"):
            for policy in ("neutral", "recovery"):
                cfg = engine.Config(scenario=scenario, prompt_policy=policy)
                specs = [engine.agent_spec(cfg, strategy) for strategy in engine.STRATEGIES]
                self.assertEqual(len({spec["system"] for spec in specs}), 1)
                self.assertEqual(len({spec["manifest"]["system_prompt_sha256"] for spec in specs}), 1)
                texts[scenario, policy] = specs[0]["system"]
                for strategy, spec in zip(engine.STRATEGIES, specs):
                    names = [tool["function"]["name"] for tool in spec["tools"]]
                    self.assertEqual("verify_receipt" in names, engine.STRATEGIES[strategy]["verification"])
                    finish = next(t["function"] for t in spec["tools"] if t["function"]["name"] == "finish_task")
                    self.assertEqual(finish["parameters"]["properties"]["outcome"]["enum"], ["success", "failure", "unknown"])
            self.assertNotEqual(texts[scenario, "neutral"], texts[scenario, "recovery"])

    def test_llm_explicit_unknown_uses_new_finish_schema(self):
        answer = {"content": None, "tool_calls": [{"id": "u", "type": "function", "function": {
            "name": "finish_task", "arguments": json.dumps({"outcome": "unknown", "reason": "need evidence"})}}]}
        with patch.object(engine, "model_request", return_value=answer):
            result = engine.run_trial(engine.Config(), [], agent_mode="llm")
        self.assertTrue(result["completed"])
        self.assertEqual(result["reported_outcome"], "unknown")
        self.assertEqual(result["failure_fingerprint"], "TASK_INCOMPLETE")


class MinimizationEvidenceTests(unittest.TestCase):
    @staticmethod
    def fake(config, faults, **kwargs):
        fingerprint = "PAYLOAD_MISMATCH" if "corrupt_payload" in faults else ""
        return {"failed": bool(fingerprint), "failure_fingerprint": fingerprint,
                "events": [{"tool": "fixture", "observed": {"faults": list(faults)}}],
                "business_records": [{"priority": "wrong"}] if fingerprint else [],
                "completed": True, "reported_success": False}

    def test_complete_chain_keeps_initial_candidate_and_independent_final_evidence(self):
        cfg = engine.Config(faults=["corrupt_payload", "latency"])
        with patch.object(engine, "run_trial", side_effect=self.fake) as trial:
            result = engine.minimize(cfg, cfg.faults, expected_fingerprint="PAYLOAD_MISMATCH")
        self.assertTrue(result["completed"])
        self.assertEqual(result["faults"], ["corrupt_payload"])
        self.assertEqual(len(result["initial_runs"]), 2)
        self.assertEqual(len(result["final_runs"]), 2)
        self.assertTrue(result["final_revalidation"]["completed"])
        self.assertEqual(result["final_revalidation"]["same_error_count"], 2)
        self.assertEqual(trial.call_count, result["trials"])
        self.assertTrue(result["boundary_step_indexes"])
        for step in result["steps"]:
            self.assertEqual(len(step["trial_evidence"]), step["attempted"])
            self.assertTrue(all("events" in run for run in step["trial_evidence"]))

    def test_final_drift_cannot_be_marked_complete_or_minimal(self):
        cfg = engine.Config(faults=["corrupt_payload"])
        outcomes = [self.fake(cfg, cfg.faults), self.fake(cfg, cfg.faults),
                    self.fake(cfg, []), self.fake(cfg, []), self.fake(cfg, []), self.fake(cfg, cfg.faults)]
        with patch.object(engine, "run_trial", side_effect=outcomes):
            result = engine.minimize(cfg, cfg.faults)
        self.assertFalse(result["completed"])
        self.assertFalse(result["stable"])
        self.assertFalse(result["one_minimal"])
        self.assertEqual(result["stopped_reason"], "unstable_final_failure")
        self.assertEqual(result["final_revalidation"]["same_error_count"], 1)

    def test_initial_drift_preserves_the_original_target_identity(self):
        other = {"failed": True, "failure_fingerprint": "DIFFERENT_ERROR"}
        with patch.object(engine, "run_trial", side_effect=[other, other]):
            result = engine.minimize(engine.Config(), ["latency"], expected_fingerprint="ORIGINAL_ERROR")
        self.assertEqual(result["failure_fingerprint"], "ORIGINAL_ERROR")
        self.assertEqual(result["observed_initial_fingerprints"], ["DIFFERENT_ERROR"] * 2)
        self.assertFalse(result["stable"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stopped_reason"], "unstable_initial_failure")


if __name__ == "__main__":
    unittest.main()
