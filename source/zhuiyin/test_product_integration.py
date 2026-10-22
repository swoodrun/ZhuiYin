"""Exercise template-preserving retests against real generated transactions."""
import json
from pathlib import Path
import tempfile
import unittest

from engine import Config, run_experiment
from experiment_compare import retest_candidates
from product_workflows import attach_template, execution_config, prepare_retest
from retest_templates import attach_retest_templates
from task_templates import TemplateStore
from test_suite import prepare_uploads, execute_suite, save_prepared, load_prepared


class ProductIntegrationTests(unittest.TestCase):
    def test_retest_keeps_template_contract_in_quick_and_full_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = TemplateStore(root / 'templates.sqlite').create({
                'name': '独立附加规则', 'description': '合成验收',
                'config': {'scenario': 'equipment', 'stock': 6, 'quantity': 2,
                           'faults': [], 'request_id': 'SYNTHETIC-RETEST'},
                'contract': {'record_count': {'mode': 'exactly', 'value': 1},
                             'expected_fields': {'quantity': 3}, 'stock_delta': -2}})
            cfg = execution_config({**template['config'], 'agent_mode': 'rules',
                                    'template_snapshot': template})
            report = attach_template(run_experiment(cfg), cfg)
            source = {'run_id': 'synthetic-source', 'kind': 'experiment', 'status': 'done',
                      'result': report, 'config': report['config']}
            candidates = retest_candidates(source)
            selected = [row['candidate_id'] for row in candidates if row['failed']]
            self.assertTrue(selected)
            files, lineage = prepare_retest(source, selected, candidates)
            for profile in ('quick', 'full'):
                with self.subTest(profile=profile):
                    plan = attach_retest_templates(prepare_uploads(files, profile), selected, candidates)
                    plan['retest_source'] = lineage
                    suite_id = save_prepared(root / ('plans-' + profile), plan)
                    loaded = load_prepared(root / ('plans-' + profile), suite_id)
                    destination = root / profile
                    result = execute_suite(loaded, destination)
                    self.assertEqual(result['retest_source']['source_run_id'], 'synthetic-source')
                    case = next(item for item in result['cases'] if item.get('kind') == 'scenario')
                    self.assertEqual(case['template_snapshot'], template)
                    for strategy, value in case['strategies'].items():
                        self.assertFalse(value['business_failed'])
                        self.assertEqual(value['template_contract']['status'], 'fail')
                        saved = json.loads((destination / case['artifacts'][strategy]['report']).read_text(encoding='utf-8'))
                        self.assertEqual(saved['template_contract']['status'], 'fail')
                    next_candidates = retest_candidates({'run_id': 'synthetic-retest', 'kind': 'test_suite', 'result': result})
                    self.assertTrue(next_candidates)
                    self.assertTrue(all(row['config'].get('template_snapshot') == template for row in next_candidates))


if __name__ == '__main__':
    unittest.main()
