"""Offline real Agent loop tests; no external calls or held-out input data."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import engine
from agent_suite import execute_agent_suite
from test_suite import prepare_uploads
from test_experiment_budget import config, SyntheticModel


def plan():
    return prepare_uploads([{"filename": "synthetic-agent-batch.json", "content": json.dumps({"kind": "scenarios", "scenarios": [
        {"kind": "scenario", "name": "synthetic " + str(i), "config": {
            "request_id": "SYNTHETIC-AGENT-BATCH-" + str(i), "faults": [],
            "scenario": "equipment" if i == 0 else "ticket"}} for i in range(2)]})}])


class AgentSuiteTests(unittest.TestCase):
    def run_batch(self, budget=80, stop=None):
        folder = tempfile.TemporaryDirectory(prefix="SYNTHETIC-agent-suite-")
        self.addCleanup(folder.cleanup)
        fake = SyntheticModel()
        with patch.object(engine.urllib.request, 'build_opener', return_value=fake), \
             patch.object(engine, 'rules_agent', side_effect=AssertionError('rules invoked')):
            report = execute_agent_suite(plan(), Path(folder.name), config(max_requests=budget), should_stop=stop)
        return report, fake, Path(folder.name)

    def test_two_scenarios_use_real_agent_tools_and_keep_evidence_without_credentials(self):
        report, fake, folder = self.run_batch()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['agent_mode'], 'llm')
        self.assertEqual(report['evaluation']['execution_valid_trials'], 8)
        self.assertEqual(report['model_usage']['requests'], len(fake.requests))
        self.assertEqual(report['model_usage']['remaining'], 80-len(fake.requests))
        for case in report['cases']:
            self.assertEqual(case['execution_scope'], 'generated_sqlite_agent')
            for strategy, links in case['artifacts'].items():
                if strategy not in engine.STRATEGIES:
                    continue
                result = json.loads((folder / links['report']).read_text(encoding='utf-8'))
                self.assertTrue(result['events'])
                self.assertIn('agent_spec', result)
                self.assertTrue((folder / links['database']).is_file())
        with zipfile.ZipFile(folder/'suite-export.zip') as archive:
            text = archive.read('suite-report.json').decode()
            self.assertNotIn('SYNTHETIC_ONLY', text)
            self.assertEqual(json.loads(text)['model_usage'], report['model_usage'])

    def test_shared_budget_does_not_reset_between_tasks_or_strategies(self):
        report, fake, folder = self.run_batch(budget=5)
        self.assertEqual(len(fake.requests), 5)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['stopped_reason'], 'request_budget_exhausted')
        self.assertFalse(report['cancelled'])
        self.assertGreater(report['evaluation']['not_run_trials'], 0)
        self.assertEqual(json.loads((folder/'suite-report.json').read_text(encoding='utf-8'))['model_usage']['requests'], 5)

    def test_cancel_before_first_call_and_restore_enclosing_thread_context(self):
        before = dict(vars(engine.MODEL_CONTEXT))
        report, fake, _ = self.run_batch(stop=lambda: True)
        self.assertEqual(fake.requests, [])
        self.assertEqual(report['status'], 'cancelled')
        self.assertEqual(report['evaluation']['attempted_trials'], 0)
        self.assertEqual(vars(engine.MODEL_CONTEXT), before)


if __name__ == '__main__':
    unittest.main()
