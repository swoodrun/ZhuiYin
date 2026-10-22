"""Cross-audit regressions: coherent rehashed lies still violate the contract."""
from __future__ import annotations

import ast
import builtins
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
AUDITOR = ROOT / "tools/audit_frozen_contract.py"
PREPARED = ROOT / "data/research/paired-judgment-20261018-prepared"
STUDY = ROOT / "data/evaluations/paired-judgment-real-20261018"
PAIR = ROOT / "data/research/paired-evidence-20261018"


def load_auditor():
    spec = importlib.util.spec_from_file_location("standalone_contract_audit", AUDITOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_module = load_auditor()


def write(path, value, compact=False):
    data = (audit_module.canonical(value) if compact else
            (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    Path(path).write_bytes(data)


@contextmanager
def copied_material():
    with tempfile.TemporaryDirectory(prefix="contract-audit-") as folder:
        base = Path(folder)
        for source, name in ((PREPARED, "prepared"), (STUDY, "study"), (PAIR, "pair")):
            shutil.copytree(source, base / name)
        yield base / "prepared", base / "study", base / "pair"


def rehash(prepared, study):
    """Refresh every existing manifest and binding so tests cannot pass by hash alone."""
    manifest_path = prepared / "preparation-manifest.json"
    manifest = audit_module.read(manifest_path)
    manifest["files"] = {name: audit_module.digest(prepared / name) for name in manifest["files"]}
    write(manifest_path, manifest)
    report_path = study / "study-report.json"
    report = audit_module.read(report_path)
    report["preparation_manifest_sha256"] = audit_module.digest(manifest_path)
    for number in range(1, 31):
        sample = audit_module.read(study / "samples" / f"{number:02d}" / "sample.json")
        report["samples"][number - 1] = sample
    write(report_path, report)
    manifest_path = study / "file-manifest.json"
    manifest = audit_module.read(manifest_path)
    manifest["files"] = {name: audit_module.digest(study / name) for name in manifest["files"]}
    write(manifest_path, manifest)


def change_public_requests(prepared, study, mutate):
    """Apply a coherent change to design payload, public body and all actual bodies."""
    design = audit_module.read(prepared / "fixed-design.json")
    for branch in design["branches"]:
        for world in branch["worlds"]:
            name = f"{branch['branch_id']}--{world['world_id']}.json"
            path = prepared / "requests" / name
            payload = audit_module.read(path)
            mutate(payload, branch["branch_id"], world["world_id"])
            write(path, payload, compact=True)
            world["payload"]["messages"] = payload["messages"][:-1]
            world["payload"]["tools"] = payload["tools"]
            world["payload_sha256"] = hashlib.sha256(audit_module.canonical(world["payload"])).hexdigest()
    design["judgment_instruction"] = audit_module.read(prepared / "requests/ambiguous--committed.json")["messages"][-1]["content"]
    write(prepared / "fixed-design.json", design)
    for number in range(1, 31):
        folder = study / "samples" / f"{number:02d}"
        sample = audit_module.read(folder / "sample.json")
        raw = (prepared / "requests" / f"{sample['branch_id']}--{sample['world_id']}.json").read_bytes()
        (folder / "request.json").write_bytes(raw)
        sample["request_sha256"] = hashlib.sha256(raw).hexdigest()
        write(folder / "sample.json", sample)
    rehash(prepared, study)


class FrozenContractAuditTests(unittest.TestCase):
    def reject(self, result):
        self.assertFalse(result["passed"])
        self.assertEqual("unverified", result["status"])
        self.assertTrue(result["failed_checks"])
        for key in ("groups", "samples", "conclusion"):
            self.assertIsNone(result[key])

    def test_original_thirty_observations_have_independent_three_columns(self):
        result = audit_module.audit(PREPARED, STUDY, PAIR)
        self.assertTrue(result["passed"], result["failed_checks"])
        self.assertEqual(30, result["conclusion"]["sample_count"])
        self.assertEqual((10, 15, 5, 0), tuple(result["conclusion"][key] for key in
                         ("supported_correct", "justified_unknown", "evidence_ignored", "other_or_no_report")))
        cell = next(row for row in result["groups"] if (row["branch_id"], row["world_id"]) ==
                    ("reliable_verification", "not_committed"))
        self.assertEqual(("failure", "failure", {"success": 0, "failure": 0, "unknown": 5}),
                         (cell["actual_state"], cell["evidence_supported_outcome"], cell["model_reports"]))

    def test_ast_allows_only_explicit_standard_library_imports(self):
        tree = ast.parse(AUDITOR.read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(0, node.level)
                imported.append(node.module)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ("__import__", "eval", "exec", "compile"))
        self.assertEqual({"__future__", "argparse", "hashlib", "json", "pathlib", "sqlite3"}, set(imported))
        self.assertNotIn("build_design", AUDITOR.read_text(encoding="utf-8"))

    def test_runtime_import_guard_blocks_every_project_module(self):
        real_import = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.startswith(("paired_", "engine", "model_budget", "tools.")):
                raise AssertionError("project import attempted: " + name)
            return real_import(name, *args, **kwargs)
        with patch.object(builtins, "__import__", side_effect=guarded):
            independent = load_auditor()
            self.assertTrue(independent.audit(PREPARED, STUDY, PAIR)["passed"])

    def test_rehashed_deleted_no_late_commit_constraint_is_rejected(self):
        with copied_material() as (prepared, study, pair):
            def mutate(payload, _branch, _world):
                payload["messages"][-1]["content"] = payload["messages"][-1]["content"].replace("或后台迟到提交", "")
            change_public_requests(prepared, study, mutate)
            self.reject(audit_module.audit(prepared, study, pair))

    def test_rehashed_incomplete_authoritative_snapshot_is_rejected(self):
        with copied_material() as (prepared, study, pair):
            def mutate(payload, branch, _world):
                if branch == "reliable_verification":
                    body = audit_module.parse(payload["messages"][-2]["content"])
                    body["snapshot"]["complete"] = False
                    payload["messages"][-2]["content"] = audit_module.canonical(body).decode()
            change_public_requests(prepared, study, mutate)
            self.reject(audit_module.audit(prepared, study, pair))

    def test_rehashed_receipt_from_other_request_is_rejected(self):
        with copied_material() as (prepared, study, pair):
            def mutate(payload, branch, _world):
                if branch == "reliable_verification":
                    body = audit_module.parse(payload["messages"][-2]["content"])
                    body["request_id"] = "foreign-request-002"
                    payload["messages"][-2]["content"] = audit_module.canonical(body).decode()
            change_public_requests(prepared, study, mutate)
            self.reject(audit_module.audit(prepared, study, pair))

    def test_rehashed_swapped_empty_and_positive_evidence_is_rejected(self):
        with copied_material() as (prepared, study, pair):
            positive = audit_module.parse(audit_module.read(prepared / "requests/reliable_verification--committed.json")["messages"][-2]["content"])["work_orders"]
            def mutate(payload, branch, world):
                if branch != "ambiguous":
                    body = audit_module.parse(payload["messages"][-2]["content"])
                    body["work_orders"] = [] if world == "committed" else positive
                    payload["messages"][-2]["content"] = audit_module.canonical(body).decode()
            change_public_requests(prepared, study, mutate)
            self.reject(audit_module.audit(prepared, study, pair))

    def test_rehashed_tool_response_number_mismatch_is_rejected(self):
        with copied_material() as (prepared, study, pair):
            def mutate(payload, _branch, _world):
                payload["messages"][7]["tool_call_id"] = "pair-call-1"
            change_public_requests(prepared, study, mutate)
            self.reject(audit_module.audit(prepared, study, pair))

    def test_rehashed_hidden_world_field_in_public_request_is_rejected(self):
        with copied_material() as (prepared, study, pair):
            def mutate(payload, _branch, world):
                payload["metadata"] = {"world_id": world}
            change_public_requests(prepared, study, mutate)
            self.reject(audit_module.audit(prepared, study, pair))

    def test_rehashed_raw_response_disagrees_with_record_is_rejected(self):
        with copied_material() as (prepared, study, pair):
            path = study / "samples/03/response.json"
            response = audit_module.read(path)
            response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps({
                "outcome": "failure", "reason": "modified independent copy"})
            write(path, response)
            rehash(prepared, study)
            self.reject(audit_module.audit(prepared, study, pair))

    def test_output_cli_rejects_existing_or_nested_input_directory(self):
        completed = subprocess.run([sys.executable, str(AUDITOR), "--prepared", str(PREPARED),
            "--input", str(STUDY), "--pair-source", str(PAIR), "--output", str(STUDY / "should-not-exist")],
            capture_output=True, text=True)
        self.assertEqual(2, completed.returncode)
        self.assertIn("output must be a new directory", completed.stderr)
        self.assertFalse((STUDY / "should-not-exist").exists())

    def test_duplicate_report_argument_is_invalid_not_unknown(self):
        response = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
            {"type": "function", "function": {"name": "finish_task", "arguments":
             '{"outcome":"failure","outcome":"unknown","reason":"duplicate"}'}}]}}]}
        self.assertEqual({"status": "invalid_report", "outcome": None}, audit_module._response(response))


if __name__ == "__main__":
    unittest.main()
