"""Offline checks that campaign refusal prevents paid transport and preserves partial studies."""
import io
import json
import unittest
from unittest.mock import patch

import engine
from model_budget import CampaignBudgetError
import study


class CampaignIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.context = dict(vars(engine.MODEL_CONTEXT))
        vars(engine.MODEL_CONTEXT).clear()
        self.config = engine.Config(agent_mode="llm", model_config={
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": "OFFLINE"})

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.context)

    def test_refused_reservation_never_calls_transport_or_counts_an_unsent_request(self):
        engine.MODEL_CONTEXT.remaining = 9
        engine.MODEL_CONTEXT.usage_records = []
        with patch("engine.reserve_request", side_effect=CampaignBudgetError("request_limit")), \
                patch("engine.urllib.request.build_opener") as transport:
            with self.assertRaises(RuntimeError):
                engine.model_request(self.config.model_config, [{"role": "user", "content": "offline"}])
        transport.assert_not_called()
        self.assertEqual(engine.MODEL_CONTEXT.remaining, 9)
        self.assertEqual(engine.MODEL_CONTEXT.usage_records, [])
        self.assertEqual(engine.MODEL_CONTEXT.campaign_stopped_reason, "campaign_budget_exhausted")

    def test_study_stops_on_campaign_refusal_and_keeps_unrun_groups_unknown(self):
        before = dict(vars(engine.MODEL_CONTEXT))
        with patch("engine.reserve_request", side_effect=CampaignBudgetError("cost_limit")), \
                patch("engine.urllib.request.build_opener") as transport, \
                patch("study.public_summary", return_value={"requests": 360}):
            report = study.run_study(self.config, {"repetitions": 2})
        transport.assert_not_called()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["completed"], 1)
        self.assertEqual(report["stopped_reason"], "campaign_budget_exhausted")
        self.assertEqual(report["model_usage"]["requests"], 0)
        self.assertFalse(report["model_usage"]["external_model_verified"])
        self.assertEqual(report["samples"][0]["status"], "infra_error")
        untouched = [arm for arm in report["arms"] if arm["attempted"] == 0]
        self.assertEqual(len(untouched), 3)
        self.assertTrue(all(arm["task_completion_rate"]["rate"] is None for arm in untouched))
        self.assertEqual(vars(engine.MODEL_CONTEXT), before)

    def test_settlement_failure_keeps_the_attempt_and_blocks_followup(self):
        answer = io.BytesIO(json.dumps({"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                                      "usage": {"prompt_tokens": 4, "completion_tokens": 2}}).encode())
        engine.MODEL_CONTEXT.remaining = 9
        engine.MODEL_CONTEXT.usage_records = []
        with patch("engine.reserve_request", return_value=object()), \
                patch("engine.settle_request", side_effect=CampaignBudgetError("ledger_error")), \
                patch("engine.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = answer
            with self.assertRaises(RuntimeError):
                engine.model_request(self.config.model_config, [{"role": "user", "content": "offline"}])
        self.assertEqual(engine.MODEL_CONTEXT.remaining, 8)
        self.assertEqual(len(engine.MODEL_CONTEXT.usage_records), 1)
        self.assertEqual(engine.MODEL_CONTEXT.campaign_stopped_reason, "campaign_budget_exhausted")

    def test_unreadable_summary_does_not_drop_the_partial_sample(self):
        with patch("engine.reserve_request", side_effect=CampaignBudgetError("ledger_error")), \
                patch("study.public_summary", side_effect=CampaignBudgetError("ledger_error")):
            report = study.run_study(self.config, {"repetitions": 2})
        self.assertEqual(report["completed"], 1)
        self.assertEqual(report["campaign_budget"]["status"], "unavailable")
        self.assertEqual(report["stopped_reason"], "campaign_budget_exhausted")


if __name__ == "__main__":
    unittest.main()
