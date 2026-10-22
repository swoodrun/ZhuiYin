"""Data-only persistence, versioning, and supplementary rule checks."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from engine import Config, run_trial
from task_templates import (TemplateConflictError, TemplateStore, default_contract,
                            evaluate_contract, normalize_contract, normalize_template,
                            validate_snapshot)


class TemplateTests(unittest.TestCase):
    def body(self, **changes):
        body = {"name": "预约回归测试", "description": "由使用者保存的有限业务规则",
                "config": {"scenario": "equipment", "equipment": "投影仪", "stock": 6,
                           "quantity": 2, "request_id": "template-001", "agent_mode": "llm",
                           "prompt_policy": "recovery", "max_requests": 30, "faults": []}}
        body.update(changes)
        return body

    def test_llm_template_does_not_need_or_save_connection(self):
        normalized = normalize_template(self.body())
        self.assertEqual(normalized["config"]["agent_mode"], "llm")
        self.assertEqual(normalized["contract"]["stock_delta"], -2)
        self.assertNotIn("model_config", normalized["config"])
        self.assertEqual(normalized["contract"]["expected_fields"]["equipment"], "投影仪")

    def test_unknown_or_credential_configuration_is_rejected(self):
        for key in ("model_config", "api_key", "base_url", "model", "sql", "expression"):
            body = self.body()
            body["config"][key] = "unsafe"
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_template(body)
        with self.assertRaises(ValueError):
            normalize_template({**self.body(), "api_key": "unsafe"})

    def test_three_prices_and_cost_limit_round_trip_without_mutating_input(self):
        body = self.body()
        body["config"]["billing"] = {"max_cost_cny": "1.2", "currency": "CNY",
            "input_cache_hit_price_per_million": "0.3", "input_cache_miss_price_per_million": "3",
            "output_price_per_million": "12"}
        before = copy.deepcopy(body)
        normalized = normalize_template(body)
        self.assertEqual(normalized["config"]["billing"]["max_cost_cny"], 1.2)
        self.assertEqual(normalized["config"]["billing"]["input_cache_hit_price_per_million"], .3)
        self.assertEqual(body, before)
        body["config"]["billing"]["max_cost_cny"] = True
        with self.assertRaises(ValueError):
            normalize_template(body)

    def test_rule_bounds_and_executable_rule_keys_are_rejected(self):
        for rule in ({"sql": "SELECT 1"}, {"expected_fields": {"expression": "1==1"}},
                     {"stock_delta": True}, {"record_count": {"mode": "exactly", "value": -1}},
                     {"expected_fields": {"quantity": True}}, {"expected_fields": {"status": "deleted"}}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                normalize_contract(rule)

    def test_store_restart_versions_conflict_and_deleted_snapshot_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "templates.sqlite"
            first = TemplateStore(path).create(self.body())
            old = validate_snapshot(first)
            store = TemplateStore(path)
            self.assertEqual(store.list(), [first])
            self.assertEqual(store.get(first["id"]), first)
            body = self.body(name="第二版")
            body["expected_version"] = 1
            second = store.update(first["id"], body)
            self.assertEqual(second["version"], 2)
            self.assertEqual(second["created_at"], first["created_at"])
            with self.assertRaises(TemplateConflictError):
                store.update(first["id"], body)
            with self.assertRaises(TemplateConflictError):
                store.delete(first["id"], 1)
            self.assertEqual(store.get(first["id"], 1), old)
            self.assertTrue(store.delete(first["id"], 2)["deleted"])
            self.assertEqual(store.list(), [])
            self.assertIsNone(store.get(first["id"]))
            self.assertEqual(store.get(first["id"], 1), old)
            self.assertEqual(validate_snapshot(old), old)

    def test_persisted_snapshot_rejects_extra_connections_and_remains_detached(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TemplateStore(Path(tmp) / "templates.sqlite")
            saved = store.create(self.body())
            detached = validate_snapshot(saved)
            detached["config"]["quantity"] = 99
            self.assertEqual(store.get(saved["id"])["config"]["quantity"], 2)
            for extra in ("api_key", "model_config", "sql_dump"):
                with self.assertRaises(ValueError):
                    validate_snapshot({**saved, extra: "unsafe"})
            saved["config"]["api_key"] = "unsafe"
            with self.assertRaises(ValueError):
                validate_snapshot(saved)
            with self.assertRaises(ValueError):
                validate_snapshot({"id": "123456789abc", "name": "缺少配置", "version": 1})

    def test_invalid_identifiers_do_not_reach_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TemplateStore(Path(tmp) / "templates.sqlite")
            for value in ("../unsafe", "a' OR 1=1", "", 1):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store.get(value)

    def test_real_trial_contract_pass_and_payload_damage_fail_remain_separate(self):
        cfg = Config.from_dict({**self.body()["config"], "agent_mode": "rules"})
        contract = default_contract(cfg.public())
        clean = run_trial(cfg, [], "baseline", "rules")
        assessment = evaluate_contract(clean, contract)
        self.assertEqual(assessment["status"], "pass")
        damaged = run_trial(cfg, ["corrupt_payload"], "verification_only", "rules")
        before = copy.deepcopy(damaged)
        assessment = evaluate_contract(damaged, contract)
        self.assertEqual(assessment["status"], "fail")
        self.assertEqual(damaged, before)
        self.assertTrue(damaged["business_failed"])

    def test_missing_partial_or_unrun_data_never_passes(self):
        contract = {"record_count": {"mode": "at_most", "value": 1}, "expected_fields": {"quantity": 1}}
        for trial in ({}, {"business_records": []}, {"business_records": [{}]},
                      {"business_records": [{"status": "active"}]},
                      {"business_records": [{"status": "active", "quantity": 1}], "completed": False},
                      {"business_records": [], "skipped": True}):
            with self.subTest(trial=trial):
                self.assertEqual(evaluate_contract(trial, contract)["status"], "unknown")

    def test_record_count_zero_and_at_most_are_explicit(self):
        trial = {"business_records": [], "completed": True}
        self.assertEqual(evaluate_contract(trial, {"record_count": {"mode": "exactly", "value": 0}})["status"], "pass")
        self.assertEqual(evaluate_contract(trial, {"record_count": {"mode": "exactly", "value": 1}})["status"], "fail")
        self.assertEqual(evaluate_contract({"business_records": [{"status": "active"}]}, {
            "record_count": {"mode": "at_most", "value": 2}})["status"], "pass")

    def test_missing_stock_is_unknown_and_valid_config_can_supply_initial(self):
        self.assertEqual(evaluate_contract({"stock_remaining": 4}, {"stock_delta": -2})["status"], "unknown")
        self.assertEqual(evaluate_contract({"stock_remaining": 4}, {"stock_delta": -2}, {"stock": 6})["status"], "pass")
        self.assertEqual(evaluate_contract({"stock_initial": 6, "stock_remaining": 4}, {"stock_delta": 2})["status"], "fail")

    def test_no_rules_are_not_a_pass_and_missing_fields_not_invented(self):
        self.assertEqual(evaluate_contract({}, {})["status"], "unknown")
        result = evaluate_contract({"business_records": [{"status": "active", "quantity": True}]}, {"expected_fields": {"quantity": 1}})
        self.assertEqual(result["status"], "fail")

    def test_json_snapshot_is_strict_and_ticket_has_no_stock_rule(self):
        body = self.body()
        body["config"]["scenario"] = "ticket"
        template = normalize_template(body)
        self.assertIsNone(template["contract"]["stock_delta"])
        self.assertEqual(template["contract"]["expected_fields"]["title"], "教学楼门禁故障")
        json.dumps(template, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
