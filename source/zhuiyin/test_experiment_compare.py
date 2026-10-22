"""Saved comparisons keep independent dimensions and missing-data boundaries."""
import copy
import json
import unittest

from experiment_compare import compare_jobs, retest_candidates, reusable_config


def config(**extra):
    return {"scenario": "equipment", "equipment": "相机", "stock": 3, "quantity": 1,
            "request_id": "request-1", "seed": 42, "faults": [], "agent_mode": "rules",
            "prompt_policy": "neutral", "max_requests": 20, **extra}


def trial(success=True, business=False, report=False, outcome="success", **extra):
    return {"completed": True, "actual_success": success, "business_failed": business,
            "report_failed": report, "reported_outcome": outcome, "failed": business or report,
            "evaluation_protocol": "business-report-v2", **extra}


def job(run_id="left", baseline=None, **extra):
    report = {"config": config(), "mode": "rules", "evaluation_protocol": "business-report-v2",
              "baseline": baseline if baseline is not None else trial(), **extra}
    return {"run_id": run_id, "kind": "experiment", "status": "done", "result": report}


class CompareTests(unittest.TestCase):
    def test_does_not_mutate_records_or_reexecute(self):
        left, right = job(), job("right")
        saved = copy.deepcopy((left, right))
        result = compare_jobs(left, right)
        self.assertEqual((left, right), saved)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["paired_counts"], {"unchanged": 1})

    def test_records_improvement_without_calling_it_causal(self):
        result = compare_jobs(job(baseline=trial(False, True, True)), job("right"))
        self.assertEqual(result["paired_counts"], {"improved": 1})
        self.assertEqual(set(result["paired"][0]["improved_metrics"]), {"actual_success", "business_failed", "report_failed"})
        self.assertTrue(any("因果" in x for x in result["warnings"]))

    def test_mixed_changes_not_collapsed_to_success(self):
        result = compare_jobs(job(baseline=trial(False, False, False)), job("right", baseline=trial(True, True, False)))
        self.assertEqual(result["paired"][0]["status"], "mixed")

    def test_missing_flags_are_not_zero(self):
        left = job(baseline={"completed": True, "actual_success": True, "reported_outcome": "success"})
        result = compare_jobs(left, job("right"))
        value = result["left"]["groups"][0]["metrics"]["business_failed"]
        self.assertEqual(value, {"count": None, "known": 0, "missing": 1, "rate": None})
        self.assertNotIn("business_failed", result["paired"][0]["improved_metrics"])

    def test_unknown_report_is_not_execution_error(self):
        result = compare_jobs(job(baseline=trial(outcome="unknown")), job("right"))
        group = result["left"]["groups"][0]
        self.assertEqual(group["valid"], 1)
        self.assertEqual(group["execution_errors"], 0)
        self.assertEqual(group["metrics"]["uncertain"]["count"], 1)
        self.assertEqual(result["paired"][0]["status"], "report_changed")

    def test_not_run_does_not_supply_negative_business_metrics(self):
        result = compare_jobs(job(baseline=trial(False, False, False, None, completed=False, status="not_run")), job("right"))
        group = result["left"]["groups"][0]
        self.assertEqual((group["not_run"], group["valid"]), (1, 0))
        self.assertIsNone(group["metrics"]["actual_success"]["count"])

    def test_execution_error_metrics_are_excluded(self):
        result = compare_jobs(job(baseline=trial(False, True, True, completed=False, status="infra_error")), job("right"))
        group = result["left"]["groups"][0]
        self.assertEqual(group["execution_errors"], 1)
        self.assertEqual(group["metrics"]["business_failed"]["known"], 0)

    def test_rules_and_models_are_not_aligned_or_pooled(self):
        right = job("right", config=config(agent_mode="llm"), mode="llm")
        result = compare_jobs(job(), right)
        self.assertEqual(result["paired"], [])
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(len(result["unmatched"]["left"]), 1)

    def test_evaluation_protocols_do_not_align(self):
        right = job("right", baseline=trial(evaluation_protocol="legacy"), evaluation_protocol="legacy")
        self.assertEqual(compare_jobs(job(), right)["paired"], [])

    def test_business_configuration_must_match(self):
        right = job("right", config=config(quantity=2))
        self.assertEqual(compare_jobs(job(), right)["paired"], [])

    def test_changed_prompt_can_be_displayed_without_pooling(self):
        right = job("right", config=config(prompt_policy="recovery"))
        result = compare_jobs(job(baseline=trial(False, True, True)), right)
        self.assertEqual(len(result["paired"]), 1)
        self.assertTrue(result["paired"][0]["condition_changed"])
        self.assertEqual(len(result["groups"]), 2)

    def test_same_id_is_explicitly_not_independent(self):
        self.assertTrue(any("同一份" in note for note in compare_jobs(job(), job())["warnings"]))

    def test_regression_and_ablation_do_not_double_count(self):
        record = job(fixed=trial(), regression={"cases": [trial()] * 16}, ablation={"arms": [trial()] * 4}, minimization={"initial_runs": [trial()] * 2})
        result = compare_jobs(record, record)
        self.assertEqual(result["left"]["records"], 2)

    def test_static_and_duplicate_cases_not_added_to_statistics(self):
        record = {"result": {"kind": "test_suite", "agent_mode": "rules", "cases": [
            {"kind": "scenario", "status": "duplicate", "config": config(), "strategies": {"baseline": trial()}},
            {"kind": "trace", "status": "completed", "analysis": trial()},
            {"kind": "scenario", "status": "completed", "config": config(), "strategies": {"baseline": trial()}}]}}
        result = compare_jobs(record, record)
        self.assertEqual(result["left"]["records"], 1)
        self.assertEqual(sum(result["left"]["excluded"].values()), 2)

    def test_standalone_static_trace_never_becomes_a_primary_execution(self):
        record = {"kind": "trace", "result": {**trial(), "kind": "trace", "config": config()}}
        self.assertEqual(compare_jobs(record, record)["left"]["records"], 0)
        self.assertEqual(retest_candidates(record), [])

    def test_completed_strategy_survives_partially_failed_batch(self):
        record = {"result": {"kind": "test_suite", "agent_mode": "rules", "cases": [
            {"kind": "scenario", "status": "execution_error", "config": config(), "strategies": {"baseline": trial()}}]}}
        self.assertEqual(compare_jobs(record, record)["left"]["groups"][0]["valid"], 1)

    def test_ambiguous_duplicate_task_keys_are_not_zipped(self):
        record = {"result": {"kind": "test_suite", "agent_mode": "rules", "cases": [
            {"kind": "scenario", "status": "completed", "config": config(), "strategies": {"baseline": trial()}},
            {"kind": "scenario", "status": "completed", "config": config(), "strategies": {"baseline": trial()}}]}}
        self.assertEqual(compare_jobs(record, record)["paired"], [])

    def test_study_repetitions_are_kept_separate(self):
        record = {"result": {"kind": "study", "mode": "rules", "config": config(), "evaluation_protocol": "business-report-v2",
            "samples": [trial(strategy="baseline", prompt_policy="neutral", trial_index=n, status="valid") for n in (1, 2)],
            "arms": [{"strategy": "baseline", "prompt_policy": "neutral", "unrun": 3}]}}
        result = compare_jobs(record, record)
        self.assertEqual(len(result["paired"]), 2)
        group = result["left"]["groups"][0]
        self.assertEqual(group["independent_tasks"], 1)
        self.assertEqual(group["repeated_observations"], 2)
        self.assertEqual(group["not_run"], 3)

    def test_fully_unrun_study_arm_retains_declared_count(self):
        record = {"result": {"kind": "study", "mode": "rules", "config": config(), "evaluation_protocol": "business-report-v2",
            "samples": [], "arms": [{"strategy": "baseline", "prompt_policy": "neutral", "unrun": 5}]}}
        group = compare_jobs(record, record)["left"]["groups"][0]
        self.assertEqual(group["not_run"], 5)
        self.assertIsNone(group["metrics"]["actual_success"]["count"])

    def test_unknown_usage_and_cost_stay_unknown(self):
        left = job(model_usage={"requests": 2, "total_tokens": None, "estimated_cost": 0.1, "cost_complete": False, "known_cost_cny": 0.1})
        usage = compare_jobs(left, job("right"))["left"]["usage"]
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["estimated_cost"])
        self.assertIsNone(usage["elapsed_ms"])
        self.assertEqual(usage["known_cost_cny"], 0.1)

    def test_recorded_elapsed_is_used_without_summing_subrequests(self):
        left = job(elapsed_ms=1000, model_usage={"records": [{"elapsed_ms": 3000}]})
        self.assertEqual(compare_jobs(left, job("right"))["left"]["usage"]["elapsed_ms"], 1000)

    def test_complete_request_records_can_supply_missing_token_summary(self):
        left = job(model_usage={"requests": 2, "records": [
            {"usage_available": True, "prompt_tokens": 3, "completion_tokens": 4},
            {"usage_available": True, "prompt_tokens": 5, "completion_tokens": 6}]})
        usage = compare_jobs(left, job("right"))["left"]["usage"]
        self.assertEqual(usage["total_tokens"], 18)
        self.assertEqual(usage["total_tokens_source"], "complete_request_records")
        left["result"]["model_usage"]["total_tokens"] = None
        self.assertIsNone(compare_jobs(left, job("right"))["left"]["usage"]["total_tokens"])

    def test_actual_source_and_prompt_hash_changes_are_reported(self):
        left = job(provenance={"source_tree_sha256": "a", "prompt_definition_sha256": "c"})
        right = job("right", provenance={"source_tree_sha256": "b", "prompt_definition_sha256": "d"})
        fields = {item["field"] for item in compare_jobs(left, right)["configuration_differences"]}
        self.assertIn("version.source_tree_sha256", fields)
        self.assertIn("version.prompt_definition_sha256", fields)

    def test_credentials_and_full_connection_urls_not_returned(self):
        left = job(config={**config(), "model_config": {"api_key": "SECRET", "base_url": "https://x/SECRET"}},
                   agent_specs={"baseline": {"manifest": {"api_key": "SECRET"}}},
                   model_identity={"model": "demo", "api_origin": "https://user:SECRET@example.org/api?key=SECRET"})
        encoded = json.dumps(compare_jobs(left, job("right")))
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("/api?", encoded)
        self.assertNotIn("model_config", reusable_config(left))

    def test_retest_candidates_only_preview_reusable_primary_cases(self):
        record = job(baseline=trial(False, True, False), fixed=trial(outcome="unknown"))
        candidates = retest_candidates(record)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(candidates[0]["eligible"])
        self.assertTrue(candidates[0]["failed"])
        self.assertTrue(candidates[1]["attention"])
        self.assertEqual(candidates[1]["strategy"], "verified")
        self.assertEqual(len({c["candidate_id"] for c in candidates}), 2)

    def test_partial_or_invalid_config_not_eligible(self):
        record = job(config={"scenario": "equipment", "quantity": 1})
        self.assertFalse(retest_candidates(record)[0]["eligible"])

    def test_execution_error_retest_is_attention_not_business_failure(self):
        record = job(baseline=trial(False, True, True, completed=False, status="infra_error"))
        candidate = retest_candidates(record)[0]
        self.assertFalse(candidate["failed"])
        self.assertTrue(candidate["source_failed"])
        self.assertTrue(candidate["attention"])
        record = job(config=config(stock=-1))
        self.assertFalse(retest_candidates(record)[0]["eligible"])

    def test_template_snapshot_is_kept_without_model_connection(self):
        snapshot = {"id": "t1", "version": 1, "name": "练习", "config": {**config(), "model_config": {"api_key": "SECRET"}}}
        cfg = reusable_config(job(template_snapshot=snapshot))
        self.assertEqual(cfg["template_snapshot"]["id"], "t1")
        self.assertNotIn("SECRET", json.dumps(cfg))

    def test_template_contract_is_a_separate_saved_metric(self):
        left = job(baseline=trial(template_contract={"status": "fail"}))
        right = job("right", baseline=trial(template_contract={"status": "pass"}))
        result = compare_jobs(left, right)
        group = result["left"]["groups"][0]
        self.assertEqual(group["metrics"]["business_failed"]["count"], 0)
        self.assertEqual(group["metrics"]["contract_failed"]["count"], 1)
        self.assertIn("contract_failed", result["paired"][0]["improved_metrics"])
        self.assertFalse(retest_candidates(job())[0]["attention"])

    def test_legacy_report_does_not_invent_protocol(self):
        record = {"result": {"config": config(), "baseline": {"actual_success": True, "reported_success": True, "failed": False}}}
        group = compare_jobs(record, record)["left"]["groups"][0]
        self.assertEqual(group["evaluation_protocol"], "unknown")
        self.assertIsNone(group["metrics"]["business_failed"]["count"])

    def test_empty_job_is_readable_without_manufacturing_results(self):
        result = compare_jobs({"run_id": "a", "status": "running"}, {"run_id": "b", "status": "error"})
        self.assertEqual(result["left"]["records"], 0)
        self.assertEqual(result["groups"], [])
        self.assertEqual(retest_candidates({"run_id": "a"}), [])

    def test_missing_business_identity_cannot_be_paired_as_improvement(self):
        left = {"result": {"baseline": trial(False, True, True)}}
        right = {"result": {"baseline": trial()}}
        result = compare_jobs(left, right)
        self.assertEqual(result["paired"], [])
        self.assertEqual(len(result["unmatched"]["left"]), 1)
        self.assertEqual(len(result["unmatched"]["right"]), 1)
        self.assertIsNone(result["left"]["groups"][0]["independent_tasks"])
        self.assertTrue(any("缺少完整业务任务身份" in note for note in result["warnings"]))

    def test_partial_business_identity_cannot_be_paired(self):
        left = job(config={"scenario": "equipment", "request_id": "same", "quantity": 1})
        result = compare_jobs(left, copy.deepcopy(left))
        self.assertEqual(result["paired"], [])

    def test_missing_protocol_or_policy_never_claims_all_conditions_known(self):
        left = {"result": {"config": {key: value for key, value in config().items() if key != "prompt_policy"},
                           "baseline": {"completed": True, "actual_success": True, "reported_outcome": "success"}}}
        result = compare_jobs(left, copy.deepcopy(left))
        self.assertEqual(len(result["paired"]), 1)
        self.assertTrue(result["paired"][0]["condition_changed"])

    def test_case_snapshot_survives_second_retest_candidate_extraction(self):
        snapshot = {"id": "123456789abc", "name": "历史规则", "version": 3,
                    "config": config(), "contract": {"record_count": {"mode": "exactly", "value": 0}, "expected_fields": {}, "stock_delta": None}}
        record = {"run_id": "first-retest", "result": {"kind": "test_suite", "agent_mode": "rules", "cases": [
            {"kind": "scenario", "status": "completed", "config": config(), "template_snapshot": snapshot,
             "strategies": {"baseline": trial(template_contract={"status": "fail"})}}]}}
        candidate = retest_candidates(record)[0]
        self.assertTrue(candidate["failed"])
        self.assertEqual(candidate["config"]["template_snapshot"], snapshot)
        from product_workflows import prepare_retest
        from retest_templates import attach_retest_templates
        from task_templates import validate_snapshot
        from test_suite import prepare_uploads
        files, _ = prepare_retest(record, [candidate["candidate_id"]], [candidate])
        plan = attach_retest_templates(prepare_uploads(files), [candidate["candidate_id"]], [candidate])
        self.assertEqual(plan["cases"][0]["template_snapshot"], validate_snapshot(snapshot))

    def test_case_level_rule_change_never_claims_same_conditions(self):
        def batch(version, status):
            snapshot = {"id": "123456789abc", "name": "规则", "version": version, "config": config(),
                        "contract": {"record_count": {"mode": "exactly", "value": version}}}
            return {"result": {"kind": "test_suite", "agent_mode": "rules", "cases": [
                {"kind": "scenario", "status": "completed", "config": config(), "template_snapshot": snapshot,
                 "strategies": {"baseline": trial(template_contract={"status": status})}}]}}
        result = compare_jobs(batch(1, "fail"), batch(2, "pass"))
        self.assertEqual(result["paired"][0]["status"], "improved")
        self.assertTrue(result["paired"][0]["condition_changed"])


if __name__ == "__main__":
    unittest.main()
