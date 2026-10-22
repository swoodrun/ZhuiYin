"""Offline adapter contracts for shared workbench billing; synthetic data only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_suite
import engine
from run_billing import RunBudgetError
import study
from test_suite import prepare_uploads


PRICES = {"input_cache_hit_price_per_million": 1,
          "input_cache_miss_price_per_million": 4, "output_price_per_million": 8}


def config(billing=None, **changes):
    return engine.Config.from_dict({"agent_mode": "llm", "faults": [], "max_requests": 80,
        "model_config": {"base_url": "https://synthetic.invalid/v1", "model": "synthetic-billing",
                         "api_key": "SYNTHETIC-DO-NOT-PERSIST"},
        "billing": billing or PRICES, **changes})


def trial_result(complete=True):
    return {"completed": complete, "failed": not complete, "actual_success": complete,
        "reported_outcome": "success" if complete else None, "reported_success": complete if complete else None,
        "business_failed": False, "report_failed": False, "failure_fingerprint": "",
        "violations": [] if complete else [{"code": "MODEL_EXECUTION_ERROR"}],
        "stats": {"tool_calls": 0, "physical_writes": 0}, "events": []}


def account_request(*, known=True, prompt=1000, completion=0):
    """Exercise real reserve/settle while replacing only the provider request."""
    ctx = engine.MODEL_CONTEXT
    if ctx.remaining <= 0:
        ctx.budget_exhausted = True
        return False
    try:
        reservation = ctx.run_budget.reserve({"max_tokens": 1, "messages": []})
    except RunBudgetError:
        ctx.cost_stopped_reason = "cost_budget_exhausted"
        ctx.budget_exhausted = True
        return False
    ctx.remaining -= 1
    record = {"status": "ok" if known else "network_error", "usage_available": known,
        "cache_usage_available": known, "prompt_tokens": prompt if known else None,
        "completion_tokens": completion if known else None,
        "total_tokens": prompt + completion if known else None,
        "prompt_cache_hit_tokens": 400 if known else None,
        "prompt_cache_miss_tokens": prompt - 400 if known else None}
    ctx.usage_records.append(record)
    ctx.run_budget.settle(reservation, record)
    return True


def synthetic_plan():
    return prepare_uploads([{"filename": "synthetic-workbench-billing.json", "content": json.dumps({
        "kind": "scenarios", "scenarios": [{"kind": "scenario", "name": "synthetic case " + str(index),
            "config": {"request_id": "SYNTHETIC-BILLING-" + str(index), "faults": []}}
            for index in range(2)]})}])


class BillingJobAdaptersTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(vars(engine.MODEL_CONTEXT))
        vars(engine.MODEL_CONTEXT).clear()

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.saved)

    def run_suite(self, cfg, runner):
        plan = synthetic_plan()
        self.assertEqual(plan["counts"]["ready"], 2)
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-workbench-billing-") as folder:
            with patch.object(engine, "run_trial", side_effect=runner):
                report = agent_suite.execute_agent_suite(plan, Path(folder), cfg)
            reports = []
            for path in (Path(folder) / "cases").glob("*/*.json"):
                if path.name != "case.json":
                    reports.append(json.loads(path.read_text(encoding="utf-8")))
            saved = json.loads((Path(folder) / "suite-report.json").read_text(encoding="utf-8"))
            return report, reports, saved

    def test_study_three_prices_apply_to_each_sample_and_keep_cache_counters(self):
        def fake(*args, **kwargs):
            account_request(completion=50)
            return trial_result()
        cfg = config(max_requests=17)
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(cfg, {"repetitions": 2, "strategies": ["baseline"]})
        self.assertEqual(report["model_usage"]["limit"], 17)
        self.assertEqual(report["model_usage"]["prompt_cache_hit_tokens"], 800)
        self.assertEqual(report["model_usage"]["prompt_cache_miss_tokens"], 1200)
        self.assertAlmostEqual(report["model_usage"]["estimated_cost_cny"], .0064)
        self.assertAlmostEqual(report["run_budget"]["accounted_cost_cny"], .0064)
        self.assertEqual(report["run_budget"]["settled_requests"], 2)
        for sample in report["samples"]:
            self.assertAlmostEqual(sample["model_usage"]["estimated_cost_cny"], .0032)
            self.assertEqual(sample["model_usage"]["records"][0]["prompt_cache_hit_tokens"], 400)
        self.assertNotIn("SYNTHETIC-DO-NOT-PERSIST", json.dumps(report))

    def test_study_amount_is_shared_and_cost_stop_is_not_request_stop(self):
        prices = {key: 1 for key in PRICES}
        cfg = config({**prices, "max_cost_cny": .0105})
        guards = []
        def fake(*args, **kwargs):
            guards.append(engine.MODEL_CONTEXT.run_budget)
            return trial_result(account_request())
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(cfg, {"repetitions": 2,
                "strategies": ["baseline", "verified"], "max_requests": 80})
        self.assertEqual(report["stopped_reason"], "cost_budget_exhausted")
        self.assertEqual(report["model_usage"]["requests"], 3)
        self.assertGreater(report["model_usage"]["limit"], report["model_usage"]["requests"])
        self.assertTrue(all(guard is guards[0] for guard in guards))
        self.assertAlmostEqual(report["run_budget"]["accounted_cost_cny"], .003)
        self.assertEqual(sum(s["model_usage"]["requests"] for s in report["samples"]), 3)

    def test_suite_one_amount_covers_all_four_strategies_and_both_inputs(self):
        cfg = config({**{key: 1 for key in PRICES}, "max_cost_cny": .014})
        seen = []
        def fake(current, faults, strategy, mode):
            seen.append((current.max_requests, current.billing, engine.MODEL_CONTEXT.run_budget))
            return trial_result(account_request())
        report, trials, saved = self.run_suite(cfg, fake)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stopped_reason"], "cost_budget_exhausted")
        self.assertFalse(report["cancelled"])
        self.assertEqual(report["model_usage"]["requests"], 6)
        self.assertEqual(report["run_budget"]["settled_requests"], 6)
        self.assertTrue(all(limit == 80 and billing == cfg.billing for limit, billing, _ in seen))
        self.assertTrue(all(guard is seen[0][2] for _, _, guard in seen))
        self.assertEqual(len(report["cases"][0]["strategies"]), 4)
        self.assertGreater(report["evaluation"]["not_run_trials"], 0)
        self.assertEqual(saved["model_usage"], report["model_usage"])
        self.assertEqual(sum(trial.get("model_usage", {}).get("requests", 0) for trial in trials), 6)
        self.assertTrue(all(record["prompt_cache_hit_tokens"] == 400
            for trial in trials for record in trial.get("model_usage", {}).get("records", [])))

    def test_missing_usage_keeps_unknown_actual_cost_and_nonzero_reservation(self):
        def fake(*args, **kwargs):
            account_request(known=False)
            return trial_result(False)
        with patch.object(engine, "run_trial", side_effect=fake):
            report = study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]})
        usage = report["model_usage"]
        self.assertIsNone(usage["estimated_cost_cny"])
        self.assertIsNone(usage["prompt_tokens"])
        self.assertEqual(usage["usage_unknown_requests"], 2)
        self.assertGreater(usage["accounted_cost_cny"], 0)
        self.assertGreater(usage["records"][0]["budget_reserved_cost_cny"], 0)
        self.assertEqual(usage["records"][0]["budget_cost_basis"], "reserved_unknown_usage")

    def test_legacy_study_two_price_result_is_unchanged(self):
        records = [{"status": "ok", "usage_available": True,
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}]
        options = study.validate_study_options({"input_price_per_million": 2,
            "output_price_per_million": 8, "currency": "USD"})
        legacy = study._usage(records, options)
        self.assertEqual(legacy, study._usage(records, options, {}))
        self.assertAlmostEqual(legacy["estimated_cost"], .0006)
        self.assertEqual(legacy["currency"], "USD")
        self.assertNotIn("billing", legacy)
        self.assertNotIn("records", legacy)

    def test_study_restores_enclosing_guard_when_checkpoint_raises(self):
        old_guard = object()
        old_records = [{"parent": True}]
        vars(engine.MODEL_CONTEXT).update(run_budget=old_guard, cost_stopped_reason="parent",
            remaining=19, usage_records=old_records)
        before = dict(vars(engine.MODEL_CONTEXT))
        def checkpoint(_):
            raise OSError("synthetic persistence failure")
        with patch.object(engine, "run_trial", return_value=trial_result()), self.assertRaises(OSError):
            study.run_study(config(), {"repetitions": 2, "strategies": ["baseline"]}, on_checkpoint=checkpoint)
        self.assertEqual(vars(engine.MODEL_CONTEXT), before)
        self.assertIs(engine.MODEL_CONTEXT.run_budget, old_guard)
        self.assertIs(engine.MODEL_CONTEXT.usage_records, old_records)

    def test_suite_restores_enclosing_guard_after_success(self):
        old_guard = object()
        vars(engine.MODEL_CONTEXT).update(run_budget=old_guard, cost_stopped_reason="parent", remaining=19)
        before = dict(vars(engine.MODEL_CONTEXT))
        def fake(*args, **kwargs):
            return trial_result(account_request())
        report, _, _ = self.run_suite(config(), fake)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(vars(engine.MODEL_CONTEXT), before)
        self.assertIs(engine.MODEL_CONTEXT.run_budget, old_guard)

    def test_guard_initialization_failure_never_changes_enclosing_context(self):
        vars(engine.MODEL_CONTEXT).update(run_budget=object(), remaining=19)
        before = dict(vars(engine.MODEL_CONTEXT))
        cfg = config()
        cfg.billing = {"max_cost_cny": 1}
        with self.assertRaises(ValueError):
            study.run_study(cfg, {"repetitions": 2, "strategies": ["baseline"]})
        self.assertEqual(vars(engine.MODEL_CONTEXT), before)
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-invalid-billing-") as folder, self.assertRaises(ValueError):
            agent_suite.execute_agent_suite(synthetic_plan(), Path(folder), cfg)
        self.assertEqual(vars(engine.MODEL_CONTEXT), before)


if __name__ == "__main__":
    unittest.main()
