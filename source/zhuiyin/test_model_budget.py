"""Offline budget-guard tests: no provider or loopback network traffic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import model_budget as guard


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class ModelBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy_path = self.root / "public-policy.json"
        self.ledger = self.root / "ledger.sqlite"
        self.raw = {"allowed_origin": "https://api.deepseek.com", "allowed_model": "deepseek-flash",
                    "max_requests": 360, "max_cost_cny": "10", "input_price_per_million": "3",
                    "output_price_per_million": "12", "ledger_path": str(self.ledger)}
        self.config = {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash",
                       "api_key": "TEST_SECRET_MUST_NOT_ENTER_LEDGER"}
        self.payload = {"model": "deepseek-flash", "max_tokens": 1200,
                        "messages": [{"role": "user", "content": "TEST_PRIVATE_TEXT 中文"}]}
        self.write_policy()
        self.env = patch.dict(os.environ, {guard.POLICY_ENV: str(self.policy_path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_policy(self, **changes):
        self.raw.update(changes)
        self.policy_path.write_text(json.dumps(self.raw), encoding="utf-8")

    def settle(self, handle, prompt=100, completion=20, **changes):
        record = {"status": "ok", "usage_available": True,
                  "prompt_tokens": prompt, "completion_tokens": completion}
        record.update(changes)
        return guard.settle_request(handle, record)

    def test_disabled_does_not_touch_disk(self):
        with patch.dict(os.environ):
            os.environ.pop(guard.POLICY_ENV)
            self.assertIsNone(guard.reserve_request(None, None))
            self.assertIsNone(guard.public_summary())
            guard.settle_request(None, None)
            self.assertFalse(self.ledger.exists())

    def test_persistent_accounting_and_no_private_content(self):
        handle = guard.reserve_request(self.config, self.payload)
        before = guard.public_summary()
        self.assertEqual(before["requests_made"], 1)
        self.assertEqual(before["unsettled_requests"], 1)
        bound = len(json.dumps(self.payload, ensure_ascii=True).encode("utf-8")) + 8192
        expected = Decimal(bound * 3 + 1200 * 12) / 1_000_000
        self.assertEqual(Decimal(before["accounted_cost_cny"]), expected)
        self.settle(handle)
        after = guard.public_summary()
        self.assertEqual(Decimal(after["accounted_cost_cny"]), Decimal("0.000540"))
        self.assertEqual(after["unsettled_requests"], 0)
        self.assertEqual(after["unknown_usage_requests"], 0)
        importlib.reload(guard)
        self.assertEqual(guard.public_summary(), after)
        with database(self.ledger) as db:
            stored = "\n".join(db.iterdump())
        self.assertNotIn(self.config["api_key"], stored)
        self.assertNotIn("TEST_PRIVATE_TEXT", stored)
        self.assertNotIn(str(self.root), stored)
        self.assertNotIn(str(self.root), json.dumps(after))

    def test_request_limit_rejection_not_counted_and_settle_idempotent(self):
        self.write_policy(max_requests=1)
        handle = guard.reserve_request(self.config, self.payload)
        self.settle(handle)
        self.settle(handle, prompt=999, completion=999)
        with self.assertRaisesRegex(guard.CampaignBudgetError, "request_limit_reached"):
            guard.reserve_request(self.config, self.payload)
        self.assertEqual(guard.public_summary()["requests_made"], 1)
        self.assertEqual(guard.public_summary()["accounted_cost_cny"], "0.000540")

    def test_exact_cost_boundary_and_unknown_no_refund(self):
        initial = guard.reserve_request(self.config, self.payload)
        reserved = guard.public_summary()["accounted_cost_cny"]
        # A fresh policy and ledger with exactly enough money for one reservation.
        self.write_policy(ledger_path=str(self.root / "boundary.sqlite"), max_cost_cny=reserved)
        handle = guard.reserve_request(self.config, self.payload)
        self.settle(handle, status="http_error")
        with self.assertRaisesRegex(guard.CampaignBudgetError, "cost_limit_reached"):
            guard.reserve_request(self.config, self.payload)
        summary = guard.public_summary()
        self.assertEqual(summary["requests_made"], 1)
        self.assertEqual(summary["accounted_cost_cny"], reserved)
        self.assertEqual(summary["unknown_usage_requests"], 1)
        self.assertEqual(summary["remaining_cost_cny"], "0.000000")
        self.assertIsNotNone(initial)

    def test_concurrent_reservations_cannot_cross_request_limit(self):
        self.write_policy(max_requests=7)
        def attempt(_):
            try:
                return guard.reserve_request(self.config, self.payload)
            except guard.CampaignBudgetError:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            handles = list(pool.map(attempt, range(30)))
        self.assertEqual(sum(handle is not None for handle in handles), 7)
        self.assertEqual(guard.public_summary()["requests_made"], 7)

    def test_concurrent_reservations_cannot_cross_cost_limit(self):
        bound = len(json.dumps(self.payload, ensure_ascii=True).encode("utf-8")) + 8192
        one = Decimal(bound * 3 + 1200 * 12) / 1_000_000
        self.write_policy(max_cost_cny=str(one * 3))
        def attempt(_):
            try:
                return guard.reserve_request(self.config, self.payload)
            except guard.CampaignBudgetError:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            handles = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(handle is not None for handle in handles), 3)
        self.assertEqual(Decimal(guard.public_summary()["accounted_cost_cny"]), one * 3)

    def test_missing_invalid_or_failed_usage_keeps_reserve(self):
        records = [None, {}, {"status": "ok", "usage_available": False, "prompt_tokens": 1, "completion_tokens": 1},
                   {"status": "ok", "usage_available": True, "prompt_tokens": True, "completion_tokens": 1},
                   {"status": "ok", "usage_available": True, "prompt_tokens": 1, "completion_tokens": -1},
                   {"status": "network_error", "usage_available": True, "prompt_tokens": 1, "completion_tokens": 1}]
        for record in records:
            handle = guard.reserve_request(self.config, self.payload)
            before = guard.public_summary()["accounted_cost_cny"]
            guard.settle_request(handle, record)
            self.assertEqual(guard.public_summary()["accounted_cost_cny"], before)
        self.assertEqual(guard.public_summary()["unknown_usage_requests"], len(records))

    def test_overrun_actual_recorded_then_permanent_stop(self):
        handle = guard.reserve_request(self.config, self.payload)
        with self.assertRaisesRegex(guard.CampaignBudgetError, "usage_bound_exceeded"):
            self.settle(handle, prompt=20_000, completion=1500)
        summary = guard.public_summary()
        self.assertEqual(summary["overrun_requests"], 1)
        self.assertEqual(summary["accounted_cost_cny"], "0.078000")
        with database(self.ledger) as db:
            self.assertEqual(db.execute("SELECT actual_prompt_tokens,actual_completion_tokens FROM requests").fetchone(),
                             (20_000, 1500))
        with self.assertRaisesRegex(guard.CampaignBudgetError, "usage_bound_exceeded"):
            guard.reserve_request(self.config, self.payload)
        self.assertEqual(guard.public_summary()["requests_made"], 1)

    def test_official_https_origin_model_and_output_bound_only(self):
        invalid_urls = ["http://api.deepseek.com", "https://api.deepseek.com.evil.test", "https://evil.test/api.deepseek.com",
                        "https://api.deepseek.com:444", "https://user:secret@api.deepseek.com", "https://api.deepseek.com/?key=x",
                        "https://api.deepseek.com/#secret", "https://api.deepseek.com:invalid"]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(guard.CampaignBudgetError):
                guard.reserve_request({**self.config, "base_url": url}, self.payload)
        for invalid in [{**self.config, "model": "wrong"}]:
            with self.assertRaises(guard.CampaignBudgetError):
                guard.reserve_request(invalid, self.payload)
        for field, value in [("model", "wrong"), ("max_tokens", 1201), ("max_tokens", True)]:
            with self.assertRaises(guard.CampaignBudgetError):
                guard.reserve_request(self.config, {**self.payload, field: value})
        self.assertFalse(self.ledger.exists())
        self.assertIsNotNone(guard.reserve_request({**self.config, "base_url": "https://api.deepseek.com:443/v1"}, self.payload))

    def test_enormous_provider_usage_is_preserved_exactly_and_stops(self):
        handle = guard.reserve_request(self.config, self.payload)
        enormous = 10**40 + 123
        with self.assertRaisesRegex(guard.CampaignBudgetError, "usage_bound_exceeded"):
            self.settle(handle, prompt=enormous, completion=0)
        summary = guard.public_summary()
        self.assertEqual(summary["known_prompt_tokens"], enormous)
        self.assertEqual(summary["accounted_cost_cny"], guard._money(enormous * 3))
        self.assertEqual(summary["halted_reason"], "usage_bound_exceeded")
        with self.assertRaises(guard.CampaignBudgetError):
            guard.reserve_request(self.config, self.payload)

    def test_invalid_policy_is_safe_and_closed(self):
        for field, bad in [("allowed_origin", "https://evil.test"), ("allowed_model", "wrong"), ("max_requests", True),
                           ("max_requests", 375), ("max_cost_cny", 11), ("max_cost_cny", "NaN"),
                           ("max_cost_cny", "0.0000001"), ("input_price_per_million", 0),
                           ("output_price_per_million", "Infinity"), ("ledger_path", "relative.sqlite")]:
            original = self.raw[field]
            self.write_policy(**{field: bad})
            with self.subTest(field=field), self.assertRaises(guard.CampaignBudgetError) as caught:
                guard.reserve_request(self.config, self.payload)
            self.assertNotIn(str(self.root), str(caught.exception))
            self.write_policy(**{field: original})
        self.assertFalse(self.ledger.exists())

    def test_corrupt_foreign_unwritable_and_policy_changed_ledgers_stop(self):
        self.ledger.write_bytes(b"not a sqlite database")
        with self.assertRaisesRegex(guard.CampaignBudgetError, "ledger_unavailable"):
            guard.reserve_request(self.config, self.payload)
        self.ledger.unlink()
        with database(self.ledger) as db:
            db.execute("CREATE TABLE foreign_table(x)")
        with self.assertRaises(guard.CampaignBudgetError):
            guard.public_summary()
        self.ledger.unlink()
        self.write_policy(ledger_path=str(self.root / "missing-parent" / "ledger.sqlite"))
        with self.assertRaises(guard.CampaignBudgetError):
            guard.reserve_request(self.config, self.payload)
        self.write_policy(ledger_path=str(self.ledger))
        guard.reserve_request(self.config, self.payload)
        self.write_policy(max_requests=359)
        with self.assertRaises(guard.CampaignBudgetError):
            guard.reserve_request(self.config, self.payload)

    def test_ledger_tampering_fails_closed(self):
        guard.reserve_request(self.config, self.payload)
        with database(self.ledger) as db:
            db.execute("UPDATE requests SET charged_micros=0")
        with self.assertRaises(guard.CampaignBudgetError):
            guard.reserve_request(self.config, self.payload)

    def test_fractional_price_rounds_up_one_micro_yuan(self):
        self.write_policy(input_price_per_million="0.1", output_price_per_million="0.1")
        handle = guard.reserve_request(self.config, self.payload)
        self.settle(handle, prompt=1, completion=1)
        self.assertEqual(guard.public_summary()["accounted_cost_cny"], "0.000001")


if __name__ == "__main__":
    unittest.main()
