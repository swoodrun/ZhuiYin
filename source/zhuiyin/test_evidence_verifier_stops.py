"""Synthetic rule execution boundaries, not real model experimental evidence.

The engine produces the summaries and real Lab database rows. These tests ask
the independent verifier to accept valid stops and drift without reconstructing
its decision algorithm in the fixture. No model or network transport is used.
"""
import unittest
from unittest.mock import patch

import engine
from tools.verify_evidence import minimization_facts, trial_facts


class EvidenceVerifierStopTests(unittest.TestCase):
    def setUp(self):
        self.previous_context = vars(engine.MODEL_CONTEXT).copy()
        vars(engine.MODEL_CONTEXT).clear()
        self.config = engine.Config(scenario="ticket", faults=["corrupt_payload"])
        self.original_trial = engine.run_trial
        self.baseline = self.original_trial(self.config, self.config.faults)
        self.assertTrue(self.baseline["completed"])
        self.assertTrue(self.baseline["failed"])

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.previous_context)

    def assert_verified(self, report):
        failed_checks = []

        def check(label, passed, category="archive"):
            if not passed:
                failed_checks.append(label)

        config, mini = report["config"], report["minimization"]
        baseline = trial_facts(report["baseline"], config, "baseline", "baseline", check)
        initial = [trial_facts(run, config, "initial", "repeated", check)
                   for run in mini.get("initial_runs", [])]
        groups = [[trial_facts(run, config, "candidate", "repeated", check)
                   for run in step.get("trial_evidence", [])] for step in mini.get("steps", [])]
        final = [trial_facts(run, config, "final", "repeated", check)
                 for run in mini.get("final_runs", [])]
        summary = minimization_facts(report, baseline, initial, groups, final, "experiment", check)
        if "fixed" in report:
            trial_facts(report["fixed"], config, "fixed", "fixed", check)
        self.assertEqual(failed_checks, [])
        self.assertTrue(summary["consistent"])
        return summary

    def minimized_case(self, *, initially_stopped=False, stop_after=None,
                       stop_before_final=False, drift_at=None, incomplete_at=None):
        state = {"calls": 0, "stopped": initially_stopped}

        def run_trial(config, faults, **kwargs):
            state["calls"] += 1
            if state["calls"] == incomplete_at:
                # A real Lab result with no terminal report, not a handmade
                # summary. This models an unfinished local execution boundary.
                with patch.object(engine, "rules_agent", lambda lab: None):
                    result = self.original_trial(config, faults)
            else:
                # Select another valid rule execution to simulate observed
                # outcome drift; this is not evidence about model randomness.
                observed_faults = [] if state["calls"] == drift_at else faults
                result = self.original_trial(config, observed_faults)
            if state["calls"] == stop_after:
                state["stopped"] = True
            return result

        def on_step(step):
            if stop_before_final:
                state["stopped"] = True

        engine.MODEL_CONTEXT.cancel_check = lambda: state["stopped"]
        with patch.object(engine, "run_trial", side_effect=run_trial):
            mini = engine.minimize(self.config, self.config.faults, on_step=on_step,
                                   expected_fingerprint=self.baseline["failure_fingerprint"])
        report = {"config": self.config.public(), "mode": "rules", "baseline": self.baseline,
                  "minimization": mini, "minimized_faults": mini["faults"]}
        summary = self.assert_verified(report)
        return mini, summary

    def test_complete_rule_chain_is_accepted(self):
        mini, summary = self.minimized_case()
        self.assertTrue(mini["completed"])
        self.assertTrue(summary["completed"])

    def test_cancel_before_initial_runs_is_accepted(self):
        mini, _ = self.minimized_case(initially_stopped=True)
        self.assertEqual(mini["initial_runs"], [])
        self.assertFalse(mini["completed"])

    def test_cancel_after_first_initial_run_is_accepted(self):
        mini, _ = self.minimized_case(stop_after=1)
        self.assertEqual(len(mini["initial_runs"]), 1)
        self.assertFalse(mini["stable"])

    def test_cancel_after_last_initial_run_does_not_require_stable_flag(self):
        mini, _ = self.minimized_case(stop_after=2)
        self.assertEqual(len(mini["initial_runs"]), 2)
        self.assertFalse(mini["stable"])
        self.assertFalse(mini["completed"])

    def test_cancel_after_first_candidate_run_is_accepted(self):
        mini, _ = self.minimized_case(stop_after=3)
        self.assertTrue(mini["stable"])
        self.assertFalse(mini["steps"][-1]["completed"])

    def test_cancel_after_last_candidate_run_is_accepted(self):
        mini, _ = self.minimized_case(stop_after=4)
        self.assertEqual(len(mini["steps"][-1]["trial_evidence"]), 2)
        self.assertFalse(mini["steps"][-1]["completed"])

    def test_cancel_after_first_final_run_is_accepted(self):
        mini, _ = self.minimized_case(stop_after=5)
        self.assertEqual(len(mini["final_runs"]), 1)
        self.assertEqual(mini["final_revalidation"]["status"], "partial")

    def test_cancel_after_last_final_run_still_has_partial_revalidation(self):
        mini, summary = self.minimized_case(stop_after=6)
        self.assertEqual(len(mini["final_runs"]), 2)
        self.assertTrue(all(run["completed"] for run in mini["final_runs"]))
        self.assertEqual(mini["final_revalidation"]["status"], "partial")
        self.assertFalse(mini["final_revalidation"]["completed"])
        self.assertFalse(summary["completed"])

    def test_cancel_before_final_group_with_zero_attempts_is_accepted(self):
        mini, _ = self.minimized_case(stop_before_final=True)
        self.assertEqual(mini["final_runs"], [])
        self.assertEqual(mini["final_revalidation"]["status"], "partial")

    def test_initial_drift_is_accepted_as_unfinished_minimization(self):
        mini, _ = self.minimized_case(drift_at=2)
        self.assertFalse(mini["stable"])
        self.assertEqual(mini["stopped_reason"], "unstable_initial_failure")

    def test_final_drift_completes_revalidation_but_not_minimization(self):
        mini, summary = self.minimized_case(drift_at=5)
        self.assertTrue(mini["final_revalidation"]["completed"])
        self.assertEqual(mini["final_revalidation"]["status"], "unstable")
        self.assertFalse(mini["stable"])
        self.assertFalse(mini["completed"])
        self.assertFalse(summary["completed"])

    def test_unfinished_initial_execution_is_accepted(self):
        mini, _ = self.minimized_case(incomplete_at=1)
        self.assertFalse(mini["initial_runs"][-1]["completed"])
        self.assertFalse(mini["completed"])

    def test_unfinished_candidate_execution_is_accepted(self):
        mini, _ = self.minimized_case(incomplete_at=3)
        self.assertFalse(mini["steps"][-1]["trial_evidence"][-1]["completed"])
        self.assertFalse(mini["steps"][-1]["accepted"])

    def test_unfinished_final_execution_is_accepted(self):
        mini, _ = self.minimized_case(incomplete_at=5)
        self.assertFalse(mini["final_runs"][-1]["completed"])
        self.assertEqual(mini["final_revalidation"]["status"], "partial")

    def test_unfinished_baseline_and_unrun_fixed_preserve_null_results(self):
        with patch.object(engine, "rules_agent", lambda lab: None):
            with self.assertRaises(engine.IncompleteBaselineError) as stopped:
                engine.run_experiment(self.config)
        report = stopped.exception.partial_result
        summary = self.assert_verified(report)
        self.assertIsNone(report["baseline"]["failed"])
        self.assertIsNone(report["fixed"]["failed"])
        self.assertEqual(report["fixed"]["status"], "not_run")
        self.assertFalse(summary["completed"])


if __name__ == "__main__":
    unittest.main()
