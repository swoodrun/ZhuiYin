"""Mechanism and SQLite checks for four-arm, two-business experiments.

Synthetic model replies test protocol boundaries only, never model reliability.
"""
import json
import sqlite3
import unittest
from unittest.mock import patch
from contextlib import closing

import engine


def cfg(**changes):
    return engine.Config.from_dict({"stock": 6, "quantity": 2,
                                    "request_id": "advanced-001", **changes})


def codes(result):
    return {item["code"] for item in result["violations"]}


class InterventionTests(unittest.TestCase):
    def test_default_faults_stay_four_while_catalog_has_seven(self):
        self.assertEqual(len(engine.FAULTS), 7)
        self.assertEqual(cfg().faults, list(engine.DEFAULT_FAULTS))
        self.assertEqual(len(engine.DEFAULT_FAULTS), 4)

    def test_four_arms_have_independent_capabilities_and_writes(self):
        trials = {strategy: engine.run_trial(cfg(), list(engine.DEFAULT_FAULTS), strategy)
                  for strategy in engine.STRATEGIES}
        self.assertEqual(trials["baseline"]["stats"]["physical_writes"], 2)
        for strategy in ("idempotency_only", "verification_only", "verified"):
            self.assertFalse(trials[strategy]["failed"], trials[strategy]["violations"])
            self.assertEqual(trials[strategy]["stats"]["physical_writes"], 1)
        only_idempotency = trials["idempotency_only"]
        self.assertGreater(only_idempotency["stats"]["idempotency_hits"], 0)
        self.assertEqual(only_idempotency["stats"]["verification_calls"], 0)
        self.assertNotIn("verify_receipt", [event["tool"] for event in only_idempotency["events"]])
        only_verification = trials["verification_only"]
        self.assertEqual(only_verification["stats"]["idempotency_hits"], 0)
        self.assertGreater(only_verification["stats"]["verification_calls"], 0)
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(only_verification["sqlite_dump"])
            self.assertEqual(db.execute("SELECT count(*) FROM operation_keys").fetchone()[0], 0)

    def test_strong_query_is_unavailable_in_nonverification_arms(self):
        for strategy in ("baseline", "idempotency_only"):
            lab = engine.Lab(cfg(), [], strategy)
            try:
                with self.assertRaises(ValueError):
                    lab.query(strong=True)
                self.assertEqual(lab.verification_calls, 0)
            finally:
                lab.db.close()

    def test_precommit_timeout_has_no_sqlite_effect_before_retry(self):
        for scenario in ("equipment", "ticket"):
            lab = engine.Lab(cfg(scenario=scenario), ["precommit_timeout"], "verified")
            try:
                response = lab.reserve()
                self.assertEqual(response["code"], "COMMIT_UNKNOWN")
                self.assertEqual(lab.records(), [])
                self.assertEqual(lab.writes, 0)
                self.assertIs(lab.events[-1]["actual"]["committed"], False)
                self.assertEqual(lab.remaining(), None if scenario == "ticket" else 6)
            finally:
                lab.db.close()
            for strategy in engine.STRATEGIES:
                result = engine.run_trial(cfg(scenario=scenario), ["precommit_timeout"], strategy)
                self.assertFalse(result["failed"], result["violations"])
                self.assertEqual(result["stats"]["physical_writes"], 1)

    def test_false_ack_exposes_missing_verification_and_bounded_recovery(self):
        for scenario in ("equipment", "ticket"):
            for strategy, capabilities in engine.STRATEGIES.items():
                with self.subTest(scenario=scenario, strategy=strategy):
                    result = engine.run_trial(cfg(scenario=scenario), ["ack_without_commit"], strategy)
                    if capabilities["verification"]:
                        self.assertFalse(result["failed"], result["violations"])
                        self.assertEqual(result["stats"]["physical_writes"], 1)
                    else:
                        self.assertTrue(result["failed"])
                        self.assertIn("REPORT_MISMATCH", codes(result))
                        self.assertIn("TASK_INCOMPLETE", codes(result))
                        self.assertEqual(result["stats"]["physical_writes"], 0)

    def test_payload_corruption_is_not_claimed_repaired_by_any_arm(self):
        for scenario in ("equipment", "ticket"):
            for strategy, capabilities in engine.STRATEGIES.items():
                with self.subTest(scenario=scenario, strategy=strategy):
                    result = engine.run_trial(cfg(scenario=scenario), ["corrupt_payload"], strategy)
                    self.assertTrue(result["failed"])
                    self.assertFalse(result["actual_success"])
                    self.assertIn("PAYLOAD_MISMATCH", codes(result))
                    if capabilities["verification"]:
                        self.assertFalse(result["reported_success"])
                        self.assertNotIn("REPORT_MISMATCH", codes(result))
                    self.assertEqual(result["stats"]["physical_writes"], 1)
                    with closing(sqlite3.connect(":memory:")) as db:
                        db.executescript(result["sqlite_dump"])
                        if scenario == "ticket":
                            priority = db.execute("SELECT priority FROM work_orders").fetchone()[0]
                            self.assertNotEqual(priority, "normal")
                        else:
                            quantity = db.execute("SELECT quantity FROM reservations").fetchone()[0]
                            self.assertNotEqual(quantity, 2)


class TicketBusinessTests(unittest.TestCase):
    def test_ticket_uses_real_independent_schema_and_business_fields(self):
        config = cfg(scenario="ticket", ticket_title="实验室路由器故障", ticket_priority="urgent", stock=0, quantity=100)
        result = engine.run_trial(config, [], "verified")
        self.assertFalse(result["failed"])
        self.assertEqual(result["scenario"], "ticket")
        self.assertIsNone(result["stock_initial"])
        self.assertIsNone(result["stock_remaining"])
        self.assertEqual(result["reservations"], result["business_records"])
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(result["sqlite_dump"])
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("work_orders", tables)
            self.assertNotIn("reservations", tables)
            self.assertNotIn("inventory", tables)
            row = db.execute("SELECT request_id,title,priority,status FROM work_orders").fetchone()
            self.assertEqual(row, (config.request_id, config.ticket_title, "urgent", "active"))
        tools = {event["tool"] for event in result["events"]}
        self.assertTrue({"find_ticket", "create_ticket", "verify_receipt"} <= tools)

    def test_ticket_duplicate_has_its_own_oracle(self):
        result = engine.run_trial(cfg(scenario="ticket"), ["commit_response_lost", "stale_read"])
        self.assertIn("DUPLICATE_TICKET", codes(result))
        self.assertNotIn("DUPLICATE_RESERVATION", codes(result))
        self.assertEqual(len(result["business_records"]), 2)

    def test_invalid_ticket_configuration_is_rejected(self):
        for changes in ({"scenario": "other"}, {"ticket_title": ""}, {"ticket_priority": "critical"}):
            with self.assertRaises(ValueError):
                cfg(**changes)

    def test_ticket_model_tools_and_prompt_have_no_fault_configuration(self):
        seen = []
        actions = ["find_ticket", "create_ticket", "verify_receipt", "finish_task"]
        def reply(model_config, messages, tools):
            seen.append((messages[:2], tools))
            name = actions[len(seen) - 1]
            arguments = {"success": True, "reason": "synthetic protocol test"} if name == "finish_task" else {}
            return {"tool_calls": [{"id": f"call-{len(seen)}", "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        config = cfg(scenario="ticket")
        with patch("engine.model_request", side_effect=reply):
            result = engine.run_trial(config, [], "verification_only", "llm", {})
        self.assertFalse(result["failed"], result["violations"])
        task = json.loads(seen[0][0][1]["content"])
        self.assertEqual(set(task), {"ticket_title", "ticket_priority", "request_id"})
        self.assertNotIn("faults", task)
        self.assertNotIn("reserve_device", {tool["function"]["name"] for tool in seen[0][1]})


class ReportScopeTests(unittest.TestCase):
    def test_report_separates_original_scope_ablation_and_presets(self):
        result = engine.run_experiment(cfg(faults=["corrupt_payload"]))
        self.assertEqual(result["regression"]["total"], 16)
        self.assertEqual(result["regression"]["fault_scope"], list(engine.DEFAULT_FAULTS))
        self.assertEqual(result["ablation"]["agent_mode"], "rules")
        arms = result["ablation"]["arms"]
        self.assertEqual({arm["strategy"] for arm in arms}, set(engine.STRATEGIES))
        self.assertTrue(all(arm["result"]["failed"] for arm in arms))
        self.assertTrue(all("PAYLOAD_MISMATCH" in codes(arm["result"]) for arm in arms))
        expanded = result["expanded_regression"]
        self.assertTrue(expanded["preset"])
        self.assertEqual(expanded["total"], 8)
        self.assertEqual({case["scenario"] for case in expanded["cases"]}, {"equipment", "ticket"})
        self.assertTrue({"precommit_timeout", "ack_without_commit", "corrupt_payload"} <=
                        {fault for case in expanded["cases"] for fault in case["faults"]})
        self.assertGreater(expanded["failures_by_strategy"]["verified"], 0)
        self.assertFalse(result["fixed"]["actual_success"])


if __name__ == "__main__":
    unittest.main()
