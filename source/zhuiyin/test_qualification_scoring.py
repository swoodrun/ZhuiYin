"""Adversarial citation and non-report checks; all responses are synthetic."""
from copy import deepcopy
import json
import unittest
import evidence_qualification as fixture
import qualification_scoring as scoring


def response(args, finish="tool_calls"):
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "tool_calls": [
        {"type": "function", "function": {"name": "finish_task", "arguments": json.dumps(args)}}]}}]}


def evidence(case):
    receipt = case["receipt"]
    return {"tool_call_id": "eq-call-4", "observed_request_id": receipt["request_id"],
            "snapshot_complete": receipt["snapshot"]["complete"], "snapshot_consistency": "authoritative",
            "snapshot_id": receipt["snapshot"]["id"], "basis": "record" if receipt["work_orders"] else "empty_result",
            "record_ids": [r["id"] for r in receipt["work_orders"]], "missing_attributes": []}


class ScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = fixture.build_design()
        cls.cases = {c["condition_id"]: c for c in cls.design["cases"]}

    def quote(self, condition, outcome, edit=None):
        case = self.cases[condition]
        fields = evidence(case)
        if edit:
            fields.update(edit)
        return scoring.audit_citation(fields, case["payloads"]["cited"], self.design["task"], case["supported_outcome"], outcome)

    def test_conditional_support_four_conditions(self):
        self.assertEqual([scoring.visible_support(c["receipt"], self.design["task"]) for c in self.design["cases"]],
                         ["failure", "unknown", "unknown", "success"])

    def test_foreign_receipt_can_be_accurately_quoted_without_being_applicable(self):
        result = self.quote("E2", "unknown")
        self.assertTrue(result["reference_correct"])
        self.assertFalse(result["task_bound"])
        self.assertTrue(result["supports_report"])

    def test_correct_quote_does_not_rescue_unsupported_failure(self):
        result = self.quote("E1", "failure")
        self.assertTrue(result["reference_correct"])
        self.assertFalse(result["supports_report"])
        self.assertEqual(scoring.assess("failure", "unknown", True)["evidence_assessment"], "unsupported_claim")

    def test_citing_reliable_empty_does_not_justify_unknown(self):
        result = self.quote("E0", "unknown")
        self.assertTrue(result["reference_correct"])
        self.assertFalse(result["supports_report"])

    def test_invented_message_and_record_rejected(self):
        self.assertFalse(self.quote("E3", "success", {"tool_call_id": "absent"})["reference_correct"])
        self.assertFalse(self.quote("E3", "success", {"record_ids": [99]})["reference_correct"])

    def test_fabricated_missing_or_true_for_false_rejected(self):
        self.assertFalse(self.quote("E1", "unknown", {"snapshot_complete": True})["reference_correct"])
        self.assertFalse(self.quote("E0", "failure", {"missing_attributes": ["snapshot.complete"]})["reference_correct"])

    def test_no_citation_no_credit(self):
        result = self.quote("E0", "failure", {"basis": "not_provided"})
        self.assertFalse(result["exists"])
        self.assertFalse(result["supports_report"])

    def test_direct_and_cited_exact_schema(self):
        args = {"outcome": "failure", "reason": "test"}
        self.assertEqual(scoring.classify(response(args), "direct")["status"], "valid")
        self.assertEqual(scoring.classify(response(args), "cited")["status"], "invalid_report")
        args["evidence"] = evidence(self.cases["E0"])
        self.assertEqual(scoring.classify(response(args), "cited")["status"], "valid")
        self.assertEqual(scoring.classify(response(args), "direct")["status"], "invalid_report")

    def test_boolean_is_not_integer_and_false_is_not_missing(self):
        args = {"outcome": "unknown", "reason": "test", "evidence": evidence(self.cases["E1"])}
        self.assertEqual(scoring.classify(response(args), "cited")["status"], "valid")
        args["evidence"]["record_ids"] = [True]
        self.assertEqual(scoring.classify(response(args), "cited")["status"], "invalid_report")

    def test_missing_nulls_match_real_message_not_guessed(self):
        case = self.cases["E1"]
        fields = {"tool_call_id": "eq-call-3", "observed_request_id": None,
                  "snapshot_id": None, "snapshot_complete": None, "snapshot_consistency": None,
                  "basis": "empty_result", "record_ids": [],
                  "missing_attributes": ["request_id", "snapshot.id", "snapshot.complete", "snapshot.consistency"]}
        result = scoring.audit_citation(fields, case["payloads"]["cited"], self.design["task"], "unknown", "unknown")
        self.assertTrue(result["reference_correct"])
        self.assertFalse(result["task_bound"])

    def test_duplicate_nonfinite_and_truncation_rejected(self):
        item = response({"outcome": "unknown", "reason": "test"})
        function = item["choices"][0]["message"]["tool_calls"][0]["function"]
        for text in ('{"outcome":"failure","outcome":"unknown","reason":"x"}',
                     '{"outcome":"unknown","reason":NaN}'):
            function["arguments"] = text
            self.assertEqual(scoring.classify(item, "direct")["status"], "invalid_report")
        self.assertEqual(scoring.classify(response({"outcome": "unknown", "reason": "x"}, "length"), "direct")["status"], "generation_incomplete")

    def test_all_unknown_cannot_be_called_qualified_correct(self):
        rows = []
        for plan in self.design["execution_plan"]:
            case = self.cases[plan["condition_id"]]
            row = {**plan, "status": "valid", "outcome": "unknown",
                   "assessment": scoring.assess("unknown", case["supported_outcome"], case["actual_success"])}
            if plan["protocol_id"] == "cited":
                row["citation"] = self.quote(plan["condition_id"], "unknown")
            rows.append(row)
        result = scoring.summarize(self.design, rows)
        self.assertEqual(sum(g["qualified_correct"] for g in result["groups"]), 0)
        self.assertEqual(sum(g["justified_unknown"] for g in result["groups"]), 12)
        self.assertEqual(sum(g["evidence_ignored"] for g in result["groups"]), 12)

    def test_zero_attempts_do_not_get_interval_or_perfect_rate(self):
        result = scoring.summarize(self.design, [])
        self.assertTrue(all(g["target_correct_ci95_over_attempted"] is None for g in result["groups"]))


if __name__ == "__main__":
    unittest.main()
