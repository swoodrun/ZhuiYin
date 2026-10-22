"""Offline request-boundary checks for workbench pricing and local cost protection."""
import io
import json
import unittest
from unittest.mock import Mock, patch
import urllib.error

import engine
import server
from run_billing import RunBudget


MODEL = {"base_url": "https://example.invalid/v1", "model": "offline-test", "api_key": "SYNTHETIC_ONLY"}
PRICES = {"input_cache_hit_price_per_million": 0.1, "input_cache_miss_price_per_million": 1,
          "output_price_per_million": 2, "currency": "CNY"}


class Response(io.BytesIO):
    status = 200


def response(usage):
    return Response(json.dumps({"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                               "usage": usage}).encode())


class BillingRequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.sentinel = object()
        self.previous = {key: getattr(engine.MODEL_CONTEXT, key, self.sentinel) for key in engine.MODEL_CONTEXT_FIELDS}
        for key in engine.MODEL_CONTEXT_FIELDS:
            if hasattr(engine.MODEL_CONTEXT, key):
                delattr(engine.MODEL_CONTEXT, key)
        self.campaign = patch.object(engine, "reserve_request", return_value=None)
        self.settlement = patch.object(engine, "settle_request")
        self.campaign_mock = self.campaign.start()
        self.settle_mock = self.settlement.start()

    def tearDown(self):
        self.campaign.stop()
        self.settlement.stop()
        for key, value in self.previous.items():
            if value is self.sentinel:
                if hasattr(engine.MODEL_CONTEXT, key):
                    delattr(engine.MODEL_CONTEXT, key)
            else:
                setattr(engine.MODEL_CONTEXT, key, value)

    def context(self, billing):
        ctx = engine.MODEL_CONTEXT
        ctx.remaining = ctx.request_limit = 10
        ctx.cancel_check = None
        ctx.usage_records = []
        ctx.budget_exhausted = False
        ctx.campaign_stopped_reason = ctx.cost_stopped_reason = None
        ctx.run_budget = RunBudget(billing)
        return ctx

    def test_cost_rejection_precedes_network_campaign_and_request_count(self):
        ctx = self.context({**PRICES, "max_cost_cny": 0.000001})
        with patch.object(engine.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(RuntimeError, "费用保护"):
                engine.model_request(MODEL, [{"role": "user", "content": "offline"}])
        network.assert_not_called()
        self.campaign_mock.assert_not_called()
        self.assertEqual(ctx.remaining, 10)
        self.assertEqual(ctx.usage_records, [])
        self.assertEqual(ctx.cost_stopped_reason, "cost_budget_exhausted")
        self.assertEqual(ctx.run_budget.summary()["accounted_cost_cny"], 0)

    def test_provider_cache_breakdown_is_retained_and_settled(self):
        ctx = self.context(PRICES)
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                 "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
        opener = Mock()
        opener.open.return_value = response(usage)
        with patch.object(engine.urllib.request, "build_opener", return_value=opener):
            engine.model_request(MODEL, [{"role": "user", "content": "offline"}])
        record = ctx.usage_records[0]
        self.assertEqual((record["prompt_cache_hit_tokens"], record["prompt_cache_miss_tokens"]), (80, 20))
        self.assertTrue(record["cache_usage_available"])
        self.assertAlmostEqual(record["budget_accounted_cost_cny"], 0.000048)
        self.assertEqual(ctx.remaining, 9)
        self.settle_mock.assert_called_once()
        self.assertNotIn("SYNTHETIC_ONLY", json.dumps(record))

    def test_openai_cached_tokens_and_total_define_the_remaining_input(self):
        record = {}
        engine._capture_usage(record, {"prompt_tokens": 50, "completion_tokens": 2,
                                      "prompt_tokens_details": {"cached_tokens": 30}})
        self.assertEqual((record["prompt_cache_hit_tokens"], record["prompt_cache_miss_tokens"]), (30, 20))
        self.assertTrue(record["usage_available"])

    def test_missing_or_inconsistent_cache_is_not_assumed_zero(self):
        for usage in ({"prompt_tokens": 50, "completion_tokens": 2},
                      {"prompt_tokens": 50, "completion_tokens": 2, "prompt_cache_hit_tokens": 20},
                      {"prompt_tokens": 50, "completion_tokens": 2, "prompt_cache_hit_tokens": 30,
                       "prompt_cache_miss_tokens": 30},
                      {"prompt_tokens": 50, "completion_tokens": 2, "prompt_cache_hit_tokens": True,
                       "prompt_cache_miss_tokens": 49}):
            with self.subTest(usage=usage):
                record = {}
                engine._capture_usage(record, usage)
                self.assertIsNone(record["prompt_cache_hit_tokens"])
                self.assertIsNone(record["prompt_cache_miss_tokens"])
                self.assertFalse(record["cache_usage_available"])
                self.assertTrue(record["usage_available"])

    def test_network_failure_holds_allowance_and_blocks_the_next_request(self):
        ctx = self.context({**PRICES, "output_price_per_million": 1, "max_cost_cny": 0.015})
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("offline fixture")
        with patch.object(engine.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "连接失败"):
                engine.model_request(MODEL, [{"role": "user", "content": "offline"}])
            with self.assertRaisesRegex(RuntimeError, "费用保护"):
                engine.model_request(MODEL, [{"role": "user", "content": "offline"}])
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(len(ctx.usage_records), 1)
        self.assertGreater(ctx.run_budget.summary()["accounted_cost_cny"], 0)
        self.assertFalse(ctx.usage_records[0]["usage_available"])

    def test_campaign_rejection_releases_unsent_local_reservation(self):
        ctx = self.context({**PRICES, "max_cost_cny": 1})
        self.campaign_mock.side_effect = engine.CampaignBudgetError("fixture")
        with patch.object(engine.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(RuntimeError, "累计请求"):
                engine.model_request(MODEL, [{"role": "user", "content": "offline"}])
        network.assert_not_called()
        self.assertEqual(ctx.run_budget.summary()["reserved_cost_cny"], 0)
        self.assertEqual(ctx.run_budget.summary()["accounted_cost_cny"], 0)
        self.assertEqual(ctx.remaining, 10)

    def test_legacy_config_stays_unchanged_when_prices_are_not_set(self):
        self.assertNotIn("billing", engine.Config.from_dict({}).public())
        self.assertNotIn("billing", engine.Config.from_dict({"billing": {"currency": "CNY"}}).public())
        public = engine.Config.from_dict({"billing": PRICES}).public()
        self.assertEqual(public["billing"]["input_cache_hit_price_per_million"], 0.1)
        self.assertNotIn("api_key", public)

    def test_incomplete_single_run_saves_cost_stop_and_restores_enclosing_context(self):
        enclosing = self.context({**PRICES, "max_cost_cny": 2})
        original_guard, original_records = enclosing.run_budget, enclosing.usage_records
        cfg = engine.Config.from_dict({"agent_mode": "llm", "model_config": MODEL,
                                       "billing": {**PRICES, "max_cost_cny": 0.000001}})
        with patch.object(engine.urllib.request, "build_opener") as network:
            with self.assertRaises(engine.IncompleteBaselineError) as captured:
                engine.run_experiment(cfg, include_rule_checks=False)
        network.assert_not_called()
        report = captured.exception.partial_result
        self.assertEqual(report["stopped_reason"], "cost_budget_exhausted")
        self.assertFalse(report["completed"])
        self.assertEqual(report["model_usage"]["requests"], 0)
        self.assertEqual(report["run_budget"]["accounted_cost_cny"], 0)
        self.assertIs(enclosing.run_budget, original_guard)
        self.assertIs(enclosing.usage_records, original_records)

    def test_connection_check_uses_one_request_context_and_restores_previous_state(self):
        ctx = self.context({**PRICES, "max_cost_cny": 2})
        previous_guard, previous_records = ctx.run_budget, ctx.usage_records
        opener = Mock()
        opener.open.return_value = response({"prompt_tokens": 1, "completion_tokens": 1,
                                            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1})
        with patch.object(engine.urllib.request, "build_opener", return_value=opener):
            result = server.check_model_with_billing(MODEL, PRICES)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model_usage"]["requests"], 1)
        self.assertIs(ctx.run_budget, previous_guard)
        self.assertIs(ctx.usage_records, previous_records)
        self.assertEqual(ctx.remaining, 10)


if __name__ == "__main__":
    unittest.main()
