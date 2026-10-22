"""Summary assertions must agree with immutable, offline trial evidence.

Every mutation is made in a temporary archive and its manifest hash is refreshed.
These are internal-consistency tests, not claims about archive authorship.  They
never execute archived code, alter historical packages, or contact a model.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.verify_evidence import verify


EVIDENCE = Path(__file__).parent / "data/evaluations"
COUNTEREXAMPLE = EVIDENCE / "追因-v0.6升级与真实同错精简-20261017.zip"
NO_COUNTEREXAMPLE = EVIDENCE / "追因-v0.6.1评价修正与交互案例-20261017.zip"
REPORT_NAME = "case-001-report.json"


def changed_report(archive, change):
    """Refresh the content hash, so a rejection must cover semantic consistency."""
    with zipfile.ZipFile(archive) as source:
        files = {name: source.read(name) for name in source.namelist()}
    report = json.loads(files[REPORT_NAME])
    change(report)
    files[REPORT_NAME] = json.dumps(report, ensure_ascii=False).encode()
    manifest = json.loads(files["file-manifest.json"])
    manifest[REPORT_NAME] = hashlib.sha256(files[REPORT_NAME]).hexdigest()
    files["file-manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    with tempfile.TemporaryDirectory(prefix="zhuiyin-summary-test-") as directory:
        path = Path(directory) / "changed.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
            for name, value in files.items():
                output.writestr(name, value)
        return verify(path)


def partial_report(report):
    """Construct an attempted, interrupted run using valid saved business rows.

    This is a synthetic boundary fixture.  The eight saved accounting records
    are assigned to the attempted baseline; no model calls or ledger edits occur.
    An execution interruption can follow a successful database write, so actual
    business completion remains True while the overall failure claim is unknown.
    """
    reason = "model_execution_error"
    execution = [{"code": "AGENT_DID_NOT_FINISH", "message": "Synthetic unfinished report"},
                 {"code": "MODEL_EXECUTION_ERROR", "message": "Synthetic interrupted execution"}]
    report.update(status="partial", completed=False, stopped_reason=reason)
    baseline = report["baseline"]
    baseline.update(completed=False, status="infra_error", stopped_reason=reason,
                    reported_outcome=None, reported_success=None,
                    report_assessment="unreported", failed=None,
                    failure_fingerprint="AGENT_DID_NOT_FINISH|MODEL_EXECUTION_ERROR",
                    violations=copy.deepcopy(execution),
                    business_failed=False, report_failed=False,
                    business_violations=[], reporting_violations=[],
                    execution_violations=execution)
    baseline["events"] = [event for event in baseline["events"]
                          if event.get("tool") != "finish_task" and event.get("type") != "finish"]
    report["fixed"] = {
        "strategy": "verified", "scenario": report["config"]["scenario"],
        "agent_mode": "llm", "evaluation_protocol": "business-report-v2",
        "prompt_policy": report["config"]["prompt_policy"],
        "status": "not_run", "completed": False, "skipped": True,
        "stopped_reason": reason, "failed": None, "failure_fingerprint": "",
        "violations": [], "reported_outcome": None, "reported_success": None,
        "report_assessment": "unreported", "business_violations": [],
        "reporting_violations": [], "execution_violations": [],
        "business_failed": None, "report_failed": None,
        "reservations": [], "business_records": [], "stock_initial": None,
        "stock_remaining": None, "actual_success": None, "events": [],
        "stats": {"tool_calls": 0, "physical_writes": 0,
                  "idempotency_hits": 0, "verification_calls": 0}}
    faults = report["config"]["faults"][:]
    report["minimized_faults"] = faults[:]
    report["minimization"] = {
        "faults": faults, "steps": [], "initial_runs": [], "final_runs": [],
        "original_count": len(faults), "final_count": len(faults), "trials": 0,
        "one_minimal": False, "stable": False, "failure_fingerprint": "",
        "completed": False, "stopped_reason": reason, "status": "not_run",
        "reason": "Synthetic unfinished baseline; downstream work was not run."}
    usage = report["model_usage"]
    usage["requests_by_phase"] = {"baseline": usage["requests"], "minimization": 0, "fixed": 0}


@unittest.skipUnless(COUNTEREXAMPLE.is_file() and NO_COUNTEREXAMPLE.is_file(),
                     "Both saved v0.6 and v0.6.1 evidence fixtures are required")
class EvidenceVerifierSummaryTests(unittest.TestCase):
    def assert_rejected(self, archive, change):
        result = changed_report(archive, change)
        self.assertFalse(result["passed"], "Contradictory source summaries were accepted")
        self.assertTrue(any(not check["passed"] for check in result["checks"]))
        # The new checks must not fail merely because the changed report hash was
        # left stale; every temporary package has a matching report declaration.
        report_hashes = [check for check in result["checks"]
                         if check["check"] == "文件哈希: " + REPORT_NAME]
        self.assertEqual(len(report_hashes), 1)
        self.assertTrue(report_hashes[0]["passed"])
        self.assertTrue(result["reports"], "Keep the inspected report's safe derived conclusion")
        for report in result["reports"]:
            self.assertEqual(report["conclusion_status"], "unverified")
            for field in ("observed_counterexample", "minimization_completed", "same_error_reduction"):
                self.assertIsNone(report[field], "A rejected package cannot publish a verified " + field)
        return result

    def test_both_original_archives_pass_without_mutation(self):
        for archive, requests, observed in ((COUNTEREXAMPLE, 38, True),
                                            (NO_COUNTEREXAMPLE, 8, False)):
            with self.subTest(archive=archive.name):
                before = hashlib.sha256(archive.read_bytes()).hexdigest()
                result = verify(archive)
                self.assertTrue(result["passed"], [c for c in result["checks"] if not c["passed"]])
                self.assertEqual(result["reports"][0]["requests"], requests)
                self.assertIs(result["reports"][0]["observed_counterexample"], observed)
                self.assertIs(result["reports"][0]["minimization_completed"], True)
                self.assertIs(result["reports"][0]["same_error_reduction"], observed)
                self.assertEqual(result["reports"][0]["conclusion_status"], "verified_within_scope")
                self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), before)

    def assert_coverage_matches_checks(self, result):
        categories = {"archive", "trial", "minimization", "database", "provenance", "ledger"}
        self.assertEqual({entry["id"] for entry in result["coverage"]}, categories)
        self.assertEqual(len(result["coverage"]), 6)
        self.assertEqual({check["category"] for check in result["checks"]}, categories)
        self.assertEqual(sum(entry["checks"] for entry in result["coverage"]), len(result["checks"]))
        self.assertEqual(sum(entry["passed"] for entry in result["coverage"]),
                         sum(check["passed"] for check in result["checks"]))
        for entry in result["coverage"]:
            selected = [check for check in result["checks"] if check["category"] == entry["id"]]
            self.assertEqual(entry["checks"], len(selected))
            self.assertEqual(entry["passed"], sum(check["passed"] for check in selected))
            self.assertEqual(entry["status"], "not_checked" if not selected else
                             "passed" if all(check["passed"] for check in selected) else "failed")
            self.assertTrue(entry["description"].strip())
        not_covered = result["not_covered"]
        self.assertIsInstance(not_covered, list)
        self.assertTrue(all(isinstance(item, str) and item.strip() for item in not_covered))
        for boundary in ("来源", "账单", "身份", "自由文本", "因果", "八组"):
            self.assertTrue(any(boundary in item for item in not_covered),
                            "The external or untested boundary must be explicit: " + boundary)

    def test_coverage_counts_describe_actual_checks_on_a_valid_package(self):
        self.assert_coverage_matches_checks(verify(COUNTEREXAMPLE))

    def test_coverage_marks_rejected_summary_and_does_not_hide_other_checks(self):
        result = self.assert_rejected(NO_COUNTEREXAMPLE,
                                      lambda report: report["baseline"].update(failed=True))
        self.assert_coverage_matches_checks(result)
        trial = next(entry for entry in result["coverage"] if entry["id"] == "trial")
        self.assertEqual(trial["status"], "failed")
        self.assertLess(trial["passed"], trial["checks"])

    def test_review_reproduction_a_false_failed_flag_is_not_a_counterexample(self):
        result = self.assert_rejected(NO_COUNTEREXAMPLE,
                                      lambda report: report["baseline"].update(failed=True))
        self.assertTrue(result["reports"], "Keep the inspected report's safe derived conclusion")
        self.assertIsNot(result["reports"][0].get("observed_counterexample"), True)

    def test_review_reproduction_b_terminal_summary_cannot_deny_saved_runs(self):
        self.assert_rejected(COUNTEREXAMPLE, lambda report:
                             report["minimization"]["final_revalidation"].update(
                                 same_error_count=0, completed=False, status="not_run"))

    def test_each_terminal_revalidation_summary_field_is_cross_checked(self):
        mutations = (("attempted", 0), ("repeats", 3), ("same_error_count", 0),
                     ("fingerprints", ["", ""]), ("status", "not_run"),
                     ("completed", False))
        for field, value in mutations:
            with self.subTest(field=field):
                self.assert_rejected(COUNTEREXAMPLE, lambda report, field=field, value=value:
                                     report["minimization"]["final_revalidation"].update({field: value}))

    def test_boolean_counts_do_not_satisfy_integer_repeat_contract(self):
        for field in ("attempted", "repeats", "same_error_count"):
            with self.subTest(field=field):
                self.assert_rejected(COUNTEREXAMPLE, lambda report, field=field:
                                     report["minimization"]["final_revalidation"].update({field: True}))

    def test_unstable_flag_cannot_bypass_terminal_summary_checks(self):
        def change(report):
            report["minimization"]["stable"] = False
            report["minimization"]["final_revalidation"].update(
                same_error_count=0, attempted=0, completed=False, status="not_run", fingerprints=[])
        self.assert_rejected(COUNTEREXAMPLE, change)

    def test_stability_and_minimization_completion_are_derived_from_the_chain(self):
        for field, value in (("stable", False), ("completed", False)):
            with self.subTest(field=field):
                self.assert_rejected(COUNTEREXAMPLE, lambda report, field=field, value=value:
                                     report["minimization"].update({field: value}))

    def test_declared_cancellation_cannot_undo_a_complete_terminal_group(self):
        self.assert_rejected(COUNTEREXAMPLE, lambda report:
                             report["minimization"].update(completed=False, stopped_reason="cancelled"))

    def test_unstable_flag_cannot_bypass_deletion_group_checks(self):
        def change(report):
            report["minimization"]["stable"] = False
            report["minimization"]["steps"][0]["attempted"] = 0
        self.assert_rejected(COUNTEREXAMPLE, change)

    def test_candidate_summary_counts_and_fingerprints_match_its_runs(self):
        mutations = (("attempted", 0), ("repeats", 3), ("fingerprints", ["forged", "forged"]),
                     ("same_error_count", 1), ("no_error_count", 0), ("other_error_count", 1),
                     ("infra_error_count", 1), ("reproduced", 1), ("failed", True),
                     ("same_error_reproduced", True), ("completed", False),
                     ("outcome_counts", {"same_error": 1, "other_error": 0,
                                         "no_error": 1, "infra_error": 0}))
        for field, value in mutations:
            with self.subTest(field=field):
                self.assert_rejected(COUNTEREXAMPLE, lambda report, field=field, value=value:
                                     report["minimization"]["steps"][0].update({field: value}))

    def test_valid_completed_trial_failed_flag_matches_recomputed_codes(self):
        for arm in ("baseline", "fixed"):
            with self.subTest(arm=arm):
                self.assert_rejected(COUNTEREXAMPLE,
                                     lambda report, arm=arm: report[arm].update(failed=False))

    def test_valid_completed_trial_cannot_be_marked_unfinished_without_execution_error(self):
        self.assert_rejected(NO_COUNTEREXAMPLE,
                             lambda report: report["baseline"].update(completed=False))

    def test_completed_trial_aggregate_violations_and_fingerprint_are_consistent(self):
        for field, value in (("violations", []), ("failure_fingerprint", "")):
            with self.subTest(field=field):
                self.assert_rejected(COUNTEREXAMPLE, lambda report, field=field, value=value:
                                     report["baseline"].update({field: value}))

    def test_failed_flag_is_checked_in_repeated_evidence_not_only_primary_arms(self):
        for location in ("initial", "candidate", "final"):
            def change(report):
                mini = report["minimization"]
                trial = {"initial": mini["initial_runs"][0],
                         "candidate": mini["steps"][1]["trial_evidence"][0],
                         "final": mini["final_runs"][0]}[location]
                trial["failed"] = False
            with self.subTest(location=location):
                self.assert_rejected(COUNTEREXAMPLE, change)

    def test_top_level_finished_status_cannot_disagree_with_completed(self):
        self.assert_rejected(NO_COUNTEREXAMPLE, lambda report: report.update(completed=False))

    def test_observed_unknown_is_a_completed_non_counterexample(self):
        result = verify(NO_COUNTEREXAMPLE)
        self.assertTrue(result["passed"])
        self.assertIs(result["reports"][0]["observed_counterexample"], False)

    def test_consistent_partial_and_not_run_null_values_are_not_false_success(self):
        result = changed_report(NO_COUNTEREXAMPLE, partial_report)
        self.assertTrue(result["passed"], [check for check in result["checks"] if not check["passed"]])
        self.assertIsNot(result["reports"][0].get("observed_counterexample"), True)

    def test_incomplete_failure_summary_preserves_null_instead_of_false(self):
        def change(report):
            partial_report(report)
            report["baseline"]["failed"] = False
        self.assert_rejected(NO_COUNTEREXAMPLE, change)

    def test_not_run_result_preserves_null_assessments(self):
        for field in ("failed", "actual_success", "business_failed", "report_failed"):
            def change(report):
                partial_report(report)
                report["fixed"][field] = False
            with self.subTest(field=field):
                self.assert_rejected(NO_COUNTEREXAMPLE, change)

    def test_not_run_marker_cannot_hide_already_executed_evidence(self):
        self.assert_rejected(NO_COUNTEREXAMPLE,
                             lambda report: report["fixed"].update(status="not_run", skipped=True))


if __name__ == "__main__":
    unittest.main()
