"""Real SQLite two-client controls; no network or model transport is used."""
from contextlib import closing, redirect_stderr
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import concurrency_lab as concurrency
from engine import Config, EVALUATION_PROTOCOL, Lab, STRATEGIES


class ConcurrencyLabTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-concurrency-", dir=Path(__file__).resolve().parent)
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_suite(self, config=None, **kwargs):
        return concurrency.run_concurrency_suite(config or Config(), self.directory, **kwargs)

    def test_both_business_scenarios_use_database_outcomes_for_eight_controls(self):
        for scenario in ("equipment", "ticket"):
            folder = self.directory / scenario
            with self.subTest(scenario=scenario):
                report = concurrency.run_concurrency_suite(Config(scenario=scenario), folder)
                self.assertEqual(report["status"], "finished")
                self.assertEqual((report["completed"], report["planned"]), (8, 8))
                self.assertEqual(report["mode"], "rules")
                self.assertEqual(report["evaluation_protocol"], EVALUATION_PROTOCOL)
                self.assertIn("source_tree_sha256", report["provenance"])
                for case in report["cases"]:
                    with self.subTest(schedule=case["schedule"], strategy=case["strategy"]):
                        duplicate = case["schedule"] == "concurrent" and not STRATEGIES[case["strategy"]]["idempotency"]
                        self.assertEqual(case["failed"], duplicate)
                        self.assertEqual(case["actual_success"], not duplicate)
                        self.assertEqual(case["business_failed"], duplicate)
                        self.assertEqual(case["report_failed"], duplicate and not STRATEGIES[case["strategy"]]["verification"])
                        self.assertEqual(case["execution_violations"], [])
                        self.assertNotIn("AGENT_DID_NOT_FINISH", case["failure_fingerprint"])
                        self.assertTrue(all(client["completed"] for client in case["client_results"]))
                        self.assertEqual(len(case["business_records"]), 2 if duplicate else 1)
                        self.assertEqual(case["stats"]["physical_writes"], len(case["business_records"]))
                        self.assertEqual(case["stats"]["verification_calls"], 2 if STRATEGIES[case["strategy"]]["verification"] else 0)
                        self.assertEqual(case["stats"]["idempotency_hits"],
                                         int(case["schedule"] == "concurrent" and STRATEGIES[case["strategy"]]["idempotency"]))
                        if duplicate:
                            expected = "DUPLICATE_TICKET" if scenario == "ticket" else "DUPLICATE_RESERVATION"
                            self.assertIn(expected, case["failure_fingerprint"])
                        self.assertTrue((folder / case["database_filename"]).is_file())

    def test_each_client_report_is_judged_independently(self):
        original = concurrency._ActorLab.finish

        def conflicting(lab, success=None, reason="", *, outcome=None):
            # The database contains one healthy record. Only the second client
            # falsely claims failure, so its report alone must be inconsistent.
            return original(lab, False if lab.client_id == "client-2" else success, reason,
                            outcome=outcome)

        with patch.object(concurrency._ActorLab, "finish", new=conflicting):
            case = concurrency._run_case(Config(), "sequential", "baseline",
                                         self.directory / "independent-reports.sqlite", None)
        first, second = case["client_results"]
        self.assertTrue(case["actual_success"])
        self.assertFalse(case["business_failed"])
        self.assertTrue(case["report_failed"])
        self.assertFalse(first["failed"])
        self.assertEqual(first["report_assessment"], "consistent")
        self.assertTrue(second["failed"])
        self.assertEqual(second["report_assessment"], "inconsistent")
        self.assertEqual(case["reporting_violations"][0]["client_ids"], ["client-2"])
        self.assertEqual(case["execution_violations"], [])

    def test_explicit_unknown_remains_completed_and_does_not_become_failure(self):
        original = concurrency._ActorLab.finish

        def uncertain(lab, success=None, reason="", *, outcome=None):
            if lab.client_id == "client-1":
                return original(lab, outcome="unknown", reason="无法确认")
            return original(lab, success, reason, outcome=outcome)

        with patch.object(concurrency._ActorLab, "finish", new=uncertain):
            case = concurrency._run_case(Config(), "sequential", "baseline",
                                         self.directory / "unknown-report.sqlite", None)
        first, second = case["client_results"]
        self.assertFalse(case["failed"])
        self.assertTrue(case["actual_success"])
        self.assertIsNone(case["reported_success"])
        self.assertTrue(first["completed"])
        self.assertEqual(first["reported_outcome"], "unknown")
        self.assertIsNone(first["reported_success"])
        self.assertEqual(first["report_assessment"], "uncertain")
        self.assertEqual(second["reported_outcome"], "success")
        self.assertEqual(second["report_assessment"], "consistent")
        self.assertEqual(case["reporting_violations"], [])
        self.assertEqual(case["execution_violations"], [])

    def test_missing_report_stays_unreported_instead_of_inheriting_other_client(self):
        original = concurrency._ActorLab.finish

        def omit_report(lab, success=None, reason="", *, outcome=None):
            if lab.client_id != "client-1":
                return original(lab, success, reason, outcome=outcome)

        with patch.object(concurrency._ActorLab, "finish", new=omit_report):
            case = concurrency._run_case(Config(), "sequential", "baseline",
                                         self.directory / "missing-report.sqlite", None)
        first, second = case["client_results"]
        self.assertTrue(case["actual_success"])
        self.assertTrue(case["failed"])
        self.assertFalse(first["completed"])
        self.assertIsNone(first["reported_outcome"])
        self.assertEqual(first["report_assessment"], "unreported")
        self.assertFalse(second["failed"])
        self.assertEqual(case["execution_violations"][0]["code"], "AGENT_DID_NOT_FINISH")
        self.assertEqual(case["execution_violations"][0]["client_ids"], ["client-1"])
        self.assertEqual(case["reporting_violations"], [])

    def test_saved_database_matches_business_records_inventory_and_atomic_keys(self):
        for case in self.run_suite()["cases"]:
            with self.subTest(schedule=case["schedule"], strategy=case["strategy"]):
                with closing(sqlite3.connect(self.directory / case["database_filename"])) as database:
                    database.row_factory = sqlite3.Row
                    rows = [dict(row) for row in database.execute("SELECT * FROM reservations ORDER BY id")]
                    stock = database.execute("SELECT available FROM inventory").fetchone()[0]
                    keys = database.execute("SELECT COUNT(*) FROM operation_keys").fetchone()[0]
                    self.assertEqual(database.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(rows, case["business_records"])
                self.assertEqual(stock, case["stock_remaining"])
                self.assertEqual(stock, case["stock_initial"] - sum(row["quantity"] for row in rows))
                self.assertEqual(keys, 1 if STRATEGIES[case["strategy"]]["idempotency"] else 0)
                self.assertEqual({row["request_id"] for row in rows}, {case["request_id"]})

    def test_arranged_interleaving_really_reads_twice_before_commits_then_verifies(self):
        report = self.run_suite()
        for case in report["cases"]:
            events = case["events"]
            self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
            self.assertEqual({event["client_id"] for event in events}, {"client-1", "client-2"})
            clients = case["client_results"]
            if case["schedule"] == "sequential":
                self.assertEqual([client["initial_record_count"] for client in clients], [0, 1])
                self.assertEqual(case["stats"]["write_attempts"], 1)
            else:
                self.assertEqual([client["initial_record_count"] for client in clients], [0, 0])
                self.assertEqual(case["stats"]["write_attempts"], 2)
                initial = [event for event in events if event["tool"] == "query_reservations"]
                writes = [event for event in events if event["tool"] == "reserve_device"]
                self.assertEqual(len(initial), 2)
                self.assertEqual(len(writes), 2)
                self.assertLess(max(event["sequence"] for event in initial), min(event["sequence"] for event in writes))
                post_reads = [event for event in events if event["tool"] in ("verify_receipt", "finish_task")]
                self.assertLess(max(event["sequence"] for event in writes), min(event["sequence"] for event in post_reads))

    def test_concurrent_actors_have_distinct_connections_and_real_immediate_transactions(self):
        connections, statements = {}, {}
        original = concurrency._connect

        def observed(filename):
            database = original(filename)
            if threading.current_thread().name.startswith("zhuiyin-concurrent-"):
                name = Path(filename).name
                connections.setdefault(name, []).append((threading.get_ident(), database))
                database.set_trace_callback(lambda sql: statements.setdefault(name, []).append(sql))
            return database

        with patch.object(concurrency, "_connect", side_effect=observed):
            report = self.run_suite()
        for case in report["cases"]:
            if case["schedule"] != "concurrent":
                continue
            name = case["database_filename"]
            self.assertEqual(len(connections[name]), 2)
            self.assertEqual(len({thread_id for thread_id, _ in connections[name]}), 2)
            self.assertIsNot(connections[name][0][1], connections[name][1][1])
            self.assertEqual(sum(sql.upper() == "BEGIN IMMEDIATE" for sql in statements[name]), 2)

    def test_independent_reads_stay_inside_enabled_arms_and_no_model_is_called(self):
        strong_calls = []
        original = Lab.query

        def observed(lab, strong=False):
            if strong:
                strong_calls.append(lab.strategy)
                self.assertTrue(lab.verification)
            return original(lab, strong)

        config = Config(agent_mode="llm", model_config={"api_key": "SYNTHETIC_DO_NOT_USE", "model": "unused"})
        before = copy.deepcopy(config)
        with patch.object(Lab, "query", new=observed), patch("engine.model_request", side_effect=AssertionError("external model called")) as model:
            report = self.run_suite(config)
        model.assert_not_called()
        self.assertEqual(set(strong_calls), {"verification_only", "verified"})
        self.assertEqual(config, before)
        self.assertNotIn("SYNTHETIC_DO_NOT_USE", json.dumps(report))
        self.assertEqual(report["config"]["agent_mode"], "rules")
        self.assertEqual(report["config"]["faults"], [])
        self.assertEqual(report["model_usage"]["requests"], 0)
        self.assertIsNone(report["model_identity"])

    def test_insufficient_inventory_is_honest_rejection_in_all_controls(self):
        report = self.run_suite(Config(stock=0, quantity=1))
        for case in report["cases"]:
            self.assertFalse(case["failed"], case)
            self.assertFalse(case["actual_success"])
            self.assertEqual(case["business_records"], [])
            self.assertEqual(case["stats"]["physical_writes"], 0)
            self.assertTrue(all(not client["reported_success"] for client in case["client_results"]))

    def test_inventory_constraint_can_prevent_duplicate_without_idempotency(self):
        # The result is not a hard-coded winner table: capacity for only one
        # record stops the second write even in verification-only mode.
        report = self.run_suite(Config(stock=1, quantity=1))
        case = next(case for case in report["cases"] if case["schedule"] == "concurrent" and case["strategy"] == "verification_only")
        self.assertFalse(case["failed"])
        self.assertTrue(case["actual_success"])
        self.assertEqual(len(case["business_records"]), 1)
        self.assertEqual(case["stats"]["idempotency_hits"], 0)
        self.assertEqual(case["stats"]["write_attempts"], 2)

    def test_cancellation_between_cases_keeps_only_complete_immutable_checkpoints(self):
        cancelled = threading.Event()
        checkpoints = []

        def checkpoint(report):
            checkpoints.append(report)
            if report["completed"] == 1:
                cancelled.set()

        report = self.run_suite(cancel_check=cancelled.is_set, on_checkpoint=checkpoint)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stopped_reason"], "cancelled")
        self.assertEqual((report["completed"], report["planned"]), (1, 8))
        self.assertEqual(len(report["cases"][0]["client_results"]), 2)
        self.assertEqual(checkpoints[0]["completed"], 1)
        self.assertEqual(checkpoints[-1]["stopped_reason"], "cancelled")

    def test_cancelled_before_start_has_no_successful_or_attempted_cases(self):
        report = self.run_suite(cancel_check=lambda: True)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stopped_reason"], "cancelled")
        self.assertEqual(report["completed"], 0)
        self.assertEqual(report["cases"], [])
        self.assertEqual(list(self.directory.glob("*.sqlite")), [])

    def test_cancellation_after_one_commit_releases_barriers_and_omits_unfinished_case(self):
        cancelled = threading.Event()
        original = concurrency._ActorLab.reserve

        def write_then_cancel(lab):
            result = original(lab)
            if lab.schedule == "concurrent":
                cancelled.set()
            return result

        started = time.monotonic()
        with patch.object(concurrency._ActorLab, "reserve", new=write_then_cancel):
            report = self.run_suite(cancel_check=cancelled.is_set)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["completed"], 4)
        self.assertTrue(all(case["schedule"] == "sequential" for case in report["cases"]))

    def test_internal_error_is_bounded_and_private_while_original_is_logged(self):
        secret = "SYNTHETIC_INTERNAL_PATH_AND_SECRET"
        diagnostics = io.StringIO()
        started = time.monotonic()
        with redirect_stderr(diagnostics), patch.object(concurrency._ActorLab, "reserve", side_effect=RuntimeError(secret)):
            with self.assertRaises(RuntimeError) as raised:
                self.run_suite()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(str(raised.exception), concurrency.INTERNAL_ERROR)
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn(secret, diagnostics.getvalue())

    def test_thread_timeout_aborts_waiters_and_returns_fixed_error(self):
        original = concurrency._ActorLab.query

        def delayed(lab, strong=False):
            if lab.schedule == "concurrent":
                time.sleep(0.15)
            return original(lab, strong)

        started = time.monotonic()
        with redirect_stderr(io.StringIO()), patch.object(concurrency, "CASE_TIMEOUT", 0.03), \
                patch.object(concurrency._ActorLab, "query", new=delayed):
            with self.assertRaisesRegex(RuntimeError, concurrency.INTERNAL_ERROR):
                self.run_suite()
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
