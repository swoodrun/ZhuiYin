"""Real saved evidence, deliberate tampering, and inert source text."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.verify_evidence import verify

ARCHIVE = Path(__file__).parent / "data/evaluations/追因-v0.6升级与真实同错精简-20261017.zip"


@unittest.skipUnless(ARCHIVE.is_file(), "Recorded v0.6 fixture is unavailable")
class EvidenceVerifierTests(unittest.TestCase):
    def test_saved_real_record_passes_without_mutating_archive(self):
        before = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
        result = verify(ARCHIVE)
        self.assertTrue(result["passed"], [c for c in result["checks"] if not c["passed"]])
        self.assertEqual(result["reports"][0]["requests"], 38)
        self.assertEqual(hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(), before)

    def changed_package(self, change):
        with zipfile.ZipFile(ARCHIVE) as source:
            files = {name: source.read(name) for name in source.namelist()}
        change(files)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
                for name, value in files.items():
                    output.writestr(name, value)
            return verify(path)

    def changed_report(self, change):
        def modify(files):
            report = json.loads(files["case-001-report.json"])
            change(report)
            files["case-001-report.json"] = json.dumps(report, ensure_ascii=False).encode()
            manifest = json.loads(files["file-manifest.json"])
            manifest["case-001-report.json"] = hashlib.sha256(files["case-001-report.json"]).hexdigest()
            files["file-manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
        return self.changed_package(modify)

    def test_declared_trial_conditions_cannot_disagree_with_the_deletion_group(self):
        def change(report):
            report["minimization"]["final_runs"][0]["evaluated_faults"] = report["config"]["faults"]
        result = self.changed_report(change)
        self.assertFalse(result["passed"])
        self.assertTrue(any(not c["passed"] and "末轮执行故障集" in c["check"] for c in result["checks"]))

    def test_contradictory_assessment_and_fault_count_are_detected(self):
        for change in (lambda r: r["baseline"].update(report_assessment="consistent"),
                       lambda r: r["minimization"].update(final_count=999)):
            with self.subTest(change=change):
                self.assertFalse(self.changed_report(change)["passed"])

    def test_policy_file_cannot_disagree_with_the_embedded_ledger_policy(self):
        def change(files):
            policy = json.loads(files["campaign-policy.json"])
            policy["max_requests"] = 1
            files["campaign-policy.json"] = json.dumps(policy).encode()
            manifest = json.loads(files["file-manifest.json"])
            manifest["campaign-policy.json"] = hashlib.sha256(files["campaign-policy.json"]).hexdigest()
            files["file-manifest.json"] = json.dumps(manifest).encode()
        self.assertFalse(self.changed_package(change)["passed"])

    def test_missing_database_declarations_make_package_incomplete(self):
        def change(files):
            manifest = json.loads(files["file-manifest.json"])
            for name in ("case-001-baseline.sqlite", "case-001-fixed.sqlite"):
                manifest.pop(name)
            files["file-manifest.json"] = json.dumps(manifest).encode()
        result = self.changed_package(change)
        self.assertFalse(result["passed"])
        self.assertEqual(sum(not c["passed"] and "数据库快照声明" in c["check"] for c in result["checks"]), 2)

    def test_rewritten_manifest_does_not_hide_inconsistent_report_claim(self):
        def change(report):
            report["baseline"].update(reported_outcome="failure", reported_success=False)
        result = self.changed_report(change)
        self.assertFalse(result["passed"])
        self.assertTrue(any(not c["passed"] and "指纹" in c["check"] for c in result["checks"]))

    def test_sql_dump_remains_inert_and_is_not_executed_during_verification(self):
        def change(report):
            # The verifier deliberately uses the saved SQLite database and rows;
            # it neither executes nor claims to validate SQL text in a report.
            for run in report["minimization"]["initial_runs"]:
                run["sqlite_dump"] = "NOT_SQL; DROP TABLE work_orders;"
        result = self.changed_report(change)
        self.assertTrue(result["passed"], [c for c in result["checks"] if not c["passed"]])


if __name__ == "__main__":
    unittest.main()
