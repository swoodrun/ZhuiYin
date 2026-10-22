"""Derived prose must agree with verified observations, including other results."""
import json
from pathlib import Path
import tempfile
import unittest

from test_paired_judgment_experiment import response
from tools import paired_judgment_experiment as collection
from tools import summarize_paired_judgment as summary


class PairedJudgmentSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="judgment-summary-test-", dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prepared = self.root / "prepared"
        collection.prepare(self.prepared)

    def run_case(self, transport):
        source = self.root / "synthetic-source"
        collection.execute(self.prepared, source, "synthetic-not-a-real-key", transport)
        facts = summary.build(self.prepared, source, self.root / "derived", self.root / "report.md")
        return facts, (self.root / "report.md").read_text(encoding="utf-8")

    def test_totals_not_shadowed_by_last_table_row(self):
        def transport(body, _key):
            payload = json.loads(body)
            observed = json.loads([m for m in payload["messages"] if m["role"] == "tool"][-1]["content"])
            return response("success" if observed.get("work_orders") else "unknown")
        facts, text = self.run_case(transport)
        self.assertEqual(facts["evidence_counts"], {"supported_correct": 10, "justified_unknown": 15, "evidence_ignored": 5})
        self.assertIn("共10次有据正确、15次合理unknown、5次证据未利用", text)
        self.assertNotIn("共0次有据正确、0次合理unknown", text)

    def test_alternative_distribution_is_not_replaced_by_original_story(self):
        facts, text = self.run_case(lambda *_: response())
        self.assertEqual(facts["evidence_counts"], {"justified_unknown": 15, "evidence_ignored": 15})
        self.assertIn("共0次有据正确、15次合理unknown、15次证据未利用", text)
        self.assertNotIn("普通重读的正记录能被用于确认成功", text)

    def test_partial_prose_does_not_claim_thirty_reports_or_five_repetitions(self):
        def failure(*_):
            raise collection.ProviderError("synthetic_connection_failure")
        facts, text = self.run_case(failure)
        self.assertEqual(facts["status_counts"], {"infra_error": 1})
        self.assertIn("本轮实际请求1次，有效判断0次", text)
        self.assertIn("未运行29次", text)
        self.assertIn("0/6格在本轮复现了同一结论5/5", text)
        self.assertNotIn("全部返回有效判断", text)


if __name__ == "__main__":
    unittest.main()
