"""Boundary tests for ordinary-job pricing and pre-network amount limits."""
import copy
from decimal import Decimal
import json
import unittest

from run_billing import INPUT_PADDING, RunBudget, RunBudgetError, summarize_usage, validate_billing


def prices(**changes):
    value = {"input_cache_hit_price_per_million": 0.2,
             "input_cache_miss_price_per_million": 2,
             "output_price_per_million": 3, "currency": "CNY"}
    value.update(changes)
    return value


def record(prompt=100, completion=10, hit=60, miss=40, **changes):
    value = {"status": "ok", "prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": prompt + completion, "usage_available": True,
             "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}
    value.update(changes)
    return value


class BillingValidationTests(unittest.TestCase):
    def test_unconfigured_stays_empty_for_historical_designs(self):
        for value in (None, {}, {"currency": "CNY"},
                      {"currency": "CNY", "max_cost_cny": None, "output_price_per_million": ""}):
            with self.subTest(value=value):
                self.assertEqual(validate_billing(value), {})

    def test_zero_prices_and_positive_decimal_limit_are_valid(self):
        value = validate_billing(prices(input_cache_hit_price_per_million=0,
                                       input_cache_miss_price_per_million="0.000000",
                                       output_price_per_million=Decimal("0"), max_cost_cny="0.000001"))
        self.assertEqual(value["max_cost_cny"], 0.000001)
        self.assertEqual(value["output_price_per_million"], 0)
        json.dumps(value, allow_nan=False)

    def test_invalid_numbers_do_not_bypass_budget(self):
        for value in (True, False, "NaN", float("nan"), float("inf"), "Infinity", -1,
                      "0.0000001", 1_000_001, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_billing(prices(output_price_per_million=value))
        for value in (0, False, -1, "NaN"):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                validate_billing(prices(max_cost_cny=value))

    def test_limit_needs_noncache_and_output_prices_but_hit_optional(self):
        self.assertIsNone(validate_billing(prices(input_cache_hit_price_per_million=None, max_cost_cny=1))["input_cache_hit_price_per_million"])
        for missing in ("input_cache_miss_price_per_million", "output_price_per_million"):
            with self.subTest(field=missing), self.assertRaises(ValueError):
                validate_billing(prices(max_cost_cny=1, **{missing: None}))

    def test_unknown_currency_fields_and_nonobject_are_rejected(self):
        for value in ([], 1, "1", {"currency": "USD"}, {"api_key": "do-not-store"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_billing(value)


class UsageBillingTests(unittest.TestCase):
    def test_complete_cache_partition_exact_decimal_arithmetic(self):
        values = [record(), record(prompt=20, completion=3, hit=5, miss=15)]
        before = copy.deepcopy(values)
        result = summarize_usage(values, prices())
        # (65*.2 + 55*2 + 13*3) / 1e6 = .000162
        self.assertEqual(result["estimated_cost_cny"], 0.000162)
        self.assertEqual(result["estimated_cost"], 0.000162)
        self.assertEqual(result["accounted_cost_cny"], 0.000162)
        self.assertEqual(result["prompt_tokens"], 120)
        self.assertEqual(result["completion_tokens"], 13)
        self.assertEqual(result["total_tokens"], 133)
        self.assertEqual(result["prompt_cache_hit_tokens"], 65)
        self.assertTrue(result["cost_complete"])
        self.assertTrue(result["cache_usage_complete"])
        self.assertEqual(values, before)

    def test_openai_cached_detail_legitimately_derives_miss(self):
        value = {"prompt_tokens": 100, "completion_tokens": 10,
                 "prompt_tokens_details": {"cached_tokens": 60}}
        result = summarize_usage([value], prices())
        self.assertEqual(result["prompt_cache_miss_tokens"], 40)
        self.assertEqual(result["estimated_cost_cny"], 0.000122)

    def test_missing_cache_is_unknown_not_zero_and_accounted_conservatively(self):
        value = {"prompt_tokens": 100, "completion_tokens": 10, "usage_available": True}
        result = summarize_usage([value], prices())
        self.assertEqual(result["total_tokens"], 110)
        self.assertIsNone(result["estimated_cost_cny"])
        self.assertIsNone(result["prompt_cache_hit_tokens"])
        self.assertFalse(result["cache_usage_complete"])
        self.assertEqual(result["known_cost_cny"], 0.00003)
        self.assertEqual(result["accounted_cost_cny"], 0.00023)

    def test_partial_direct_cache_not_completed_with_zero_or_subtraction(self):
        value = record()
        del value["prompt_cache_hit_tokens"]
        result = summarize_usage([value], prices())
        self.assertIsNone(result["estimated_cost_cny"])
        self.assertIsNone(result["prompt_cache_miss_tokens"])
        self.assertEqual(result["known_prompt_cache_miss_tokens"], 40)
        self.assertEqual(result["accounted_cost_cny"], 0.00023)

    def test_equal_prices_allow_cost_without_cache_partition(self):
        value = {"prompt_tokens": 100, "completion_tokens": 10}
        result = summarize_usage([value], prices(input_cache_hit_price_per_million=2))
        self.assertEqual(result["estimated_cost_cny"], 0.00023)
        self.assertTrue(result["cost_complete"])
        self.assertFalse(result["cache_usage_complete"])

    def test_optional_hit_price_falls_back_to_miss_without_inventing_usage(self):
        result = summarize_usage([record()], prices(input_cache_hit_price_per_million=None))
        self.assertEqual(result["estimated_cost_cny"], 0.00023)
        self.assertEqual(result["prompt_cache_hit_tokens"], 60)

    def test_invalid_or_inconsistent_cache_is_not_precisely_priced(self):
        for hit, miss in ((True, 40), (60, -1), (60, 50), (101, 0), ("60", 40)):
            with self.subTest(hit=hit, miss=miss):
                result = summarize_usage([record(hit=hit, miss=miss)], prices())
                self.assertIsNone(result["estimated_cost_cny"])
                self.assertFalse(result["cache_usage_complete"])

    def test_partial_token_usage_preserves_known_components_and_unknown_cost(self):
        value = record(completion_tokens=None, usage_available=False)
        result = summarize_usage([value], prices())
        self.assertIsNone(result["estimated_cost_cny"])
        self.assertIsNone(result["completion_tokens"])
        self.assertEqual(result["known_prompt_tokens"], 100)
        self.assertEqual(result["known_cost_cny"], 0.000092)
        self.assertEqual(result["usage_unknown_requests"], 1)

    def test_unpriced_usage_never_claims_free_calls(self):
        result = summarize_usage([record()], {})
        self.assertIsNone(result["estimated_cost_cny"])
        self.assertIsNone(result["accounted_cost_cny"])
        self.assertEqual(result["known_cost_cny"], 0)
        self.assertFalse(result["cost_complete"])

    def test_mixed_complete_and_missing_preserves_partial_known_amount(self):
        values = [record(), {"status": "network_error", "prompt_tokens": None, "completion_tokens": None,
                             "usage_available": False, "budget_reserved_cost_cny": 0.02,
                             "budget_accounted_cost_cny": 0.02}]
        result = summarize_usage(values, prices())
        self.assertEqual(result["known_cost_cny"], 0.000122)
        self.assertIsNone(result["estimated_cost_cny"])
        self.assertEqual(result["accounted_cost_cny"], 0.020122)
        self.assertEqual(result["reserved_cost_cny"], 0.02)
        self.assertEqual(result["usage_unknown_requests"], 1)


class RunBudgetTests(unittest.TestCase):
    payload = {"model": "example", "messages": [{"role": "user", "content": "借一台"}], "max_tokens": 1200}

    def test_insufficient_budget_stops_before_simulated_network(self):
        guard = RunBudget(prices(max_cost_cny=0.000001))
        network_calls = []
        with self.assertRaises(RunBudgetError) as exc:
            reservation = guard.reserve(self.payload)
            network_calls.append(reservation)
        self.assertEqual(exc.exception.reason, "cost_budget_exhausted")
        self.assertEqual(network_calls, [])
        self.assertEqual(guard.summary()["pending_requests"], 0)

    def test_reserve_prices_high_hit_at_max_not_miss(self):
        guard = RunBudget(prices(input_cache_hit_price_per_million=10, max_cost_cny=1))
        handle = guard.reserve(self.payload)
        prompt = len(json.dumps(self.payload, ensure_ascii=True).encode("utf-8")) + INPUT_PADDING
        expected = (Decimal(prompt) * 10 + Decimal(1200) * 3) / 1_000_000
        self.assertEqual(guard.summary()["reserved_cost_cny"], float(expected))
        value = record()
        guard.settle(handle, value)
        self.assertEqual(value["budget_cost_basis"], "actual_usage")
        self.assertEqual(value["budget_accounted_cost_cny"], 0.00071)
        self.assertEqual(guard.summary()["reserved_cost_cny"], 0)

    def test_unknown_usage_retains_full_reservation(self):
        guard = RunBudget(prices(max_cost_cny=1))
        handle = guard.reserve(self.payload)
        reserved = guard.summary()["reserved_cost_cny"]
        value = {"status": "network_error", "usage_available": False}
        guard.settle(handle, value)
        self.assertEqual(value["budget_accounted_cost_cny"], reserved)
        self.assertEqual(value["budget_cost_basis"], "reserved_unknown_usage")
        self.assertEqual(guard.summary()["accounted_cost_cny"], reserved)
        self.assertAlmostEqual(guard.summary()["remaining_cost_cny"], 1 - reserved)

    def test_missing_cache_uses_actual_total_at_max_price_for_protection(self):
        guard = RunBudget(prices(max_cost_cny=1))
        handle = guard.reserve(self.payload)
        value = {"prompt_tokens": 100, "completion_tokens": 10, "usage_available": True}
        before = copy.deepcopy(value)
        guard.settle(handle, value)
        self.assertEqual(value["budget_cost_basis"], "conservative_missing_cache_usage")
        self.assertEqual(value["budget_accounted_cost_cny"], 0.00023)
        self.assertEqual({key: value[key] for key in before}, before)
        self.assertNotIn("prompt_cache_hit_tokens", value)

    def test_pending_reservations_are_counted_until_cancelled(self):
        guard = RunBudget(prices(max_cost_cny=0.03))
        first = guard.reserve(self.payload)
        with self.assertRaises(RunBudgetError):
            guard.reserve(self.payload)
        guard.cancel(first)
        self.assertEqual(guard.summary()["accounted_cost_cny"], 0)
        self.assertEqual(guard.summary()["reserved_cost_cny"], 0)
        guard.reserve(self.payload)

    def test_zero_prices_allow_requests_under_tiny_positive_amount(self):
        guard = RunBudget(prices(input_cache_hit_price_per_million=0,
                                 input_cache_miss_price_per_million=0, output_price_per_million=0,
                                 max_cost_cny="0.000001"))
        for _ in range(4):
            handle = guard.reserve(self.payload)
            guard.settle(handle, record())
        self.assertEqual(guard.summary()["accounted_cost_cny"], 0)
        self.assertEqual(guard.summary()["remaining_cost_cny"], 0.000001)

    def test_no_prices_no_amount_does_not_block_but_is_unpriced(self):
        guard = RunBudget()
        handle = guard.reserve(self.payload)
        value = record()
        guard.settle(handle, value)
        self.assertEqual(value["budget_cost_basis"], "unpriced")
        self.assertIsNone(guard.summary()["accounted_cost_cny"])
        self.assertFalse(guard.summary()["enabled"])

    def test_missing_output_limit_rejected_only_if_amount_enabled(self):
        guard = RunBudget(prices(max_cost_cny=1))
        with self.assertRaises(RunBudgetError) as exc:
            guard.reserve({"messages": []})
        self.assertEqual(exc.exception.reason, "run_budget_unavailable")
        unlimited = RunBudget(prices())
        unlimited.reserve({"messages": []})

    def test_receipt_ownership_and_single_settlement(self):
        guard = RunBudget(prices(max_cost_cny=1))
        handle = guard.reserve(self.payload)
        with self.assertRaises(RunBudgetError):
            RunBudget(prices(max_cost_cny=1)).cancel(handle)
        guard.settle(handle, record())
        with self.assertRaises(RunBudgetError):
            guard.settle(handle, record())
        self.assertEqual(guard.summary()["settled_requests"], 1)

    def test_usage_exceeding_assumption_is_accounted_and_stops_next_request(self):
        guard = RunBudget(prices(max_cost_cny=1))
        handle = guard.reserve(self.payload)
        value = record(prompt=1_000_000, hit=0, miss=1_000_000)
        guard.settle(handle, value)
        self.assertGreater(guard.summary()["accounted_cost_cny"], 1)
        self.assertEqual(guard.summary()["remaining_cost_cny"], 0)
        with self.assertRaises(RunBudgetError) as exc:
            guard.reserve(self.payload)
        self.assertEqual(exc.exception.reason, "run_budget_unavailable")


if __name__ == "__main__":
    unittest.main()
