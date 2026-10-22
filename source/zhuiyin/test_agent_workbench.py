"""Agent-only orchestration and exact preview contracts. No external calls."""
from html.parser import HTMLParser
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import engine
import server
import launch
from test_experiment_budget import SyntheticModel, config


class PageStructure(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.stack, self.ids, self.parents = [], [], {}
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
            self.parents[attrs['id']] = [a for _, a in self.stack if a]
        if tag not in {'input', 'meta', 'link', 'br', 'hr', 'img'}:
            self.stack.append((tag, attrs.get('id')))
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                break


class AgentWorkbenchTests(unittest.TestCase):
    def test_launcher_reuses_matching_service_before_opening_another_instance(self):
        with patch.object(launch.sys,'argv',['launch.py']), patch.object(launch,'healthy',side_effect=lambda port:port==8770), \
             patch.object(launch.socket,'socket',side_effect=AssertionError('opened port before checking existing service')), \
             patch.object(launch.webbrowser,'open') as opened:
            launch.main()
        opened.assert_called_once_with('http://127.0.0.1:8770/#workbench')

    def handler(self, path, body):
        handler=server.Handler.__new__(server.Handler)
        handler.headers={'Host':'127.0.0.1'}
        handler.server=SimpleNamespace(server_port=8766)
        handler.path=path
        handler.read_json=Mock(return_value=body)
        handler.json=Mock()
        return handler

    def test_agent_only_repeats_real_agent_loop_without_any_rules_trials(self):
        cfg=config(faults=['commit_response_lost','stale_read','latency'])
        fake=SyntheticModel()
        original=engine.run_trial
        modes=[]
        def checked(*args, **kwargs):
            mode=kwargs.get('agent_mode',args[3] if len(args)>3 else 'rules')
            modes.append(mode)
            self.assertEqual(mode,'llm','Agent-only workflow invoked rules')
            return original(*args,**kwargs)
        with patch.object(engine.urllib.request,'build_opener',return_value=fake), patch.object(engine,'run_trial',side_effect=checked):
            report=engine.run_experiment(cfg,include_rule_checks=False)
        self.assertGreater(len(modes),2)
        self.assertEqual(report['execution_profile'],'agent-only-v1')
        self.assertEqual(set(report['agent_specs']),{'baseline','verified'})
        self.assertTrue(report['baseline']['failed'])
        self.assertEqual(report['model_usage']['requests'],len(fake.requests))
        for section in ('regression','ablation','expanded_regression'):
            self.assertTrue(report[section]['skipped'])
            self.assertFalse(report[section]['completed'])
            self.assertFalse(report[section].get('cases') or report[section].get('arms'))
        self.assertEqual(fake.requests[0]['messages'][0]['content'],report['agent_specs']['baseline']['system'])
        self.assertEqual(fake.requests[0]['tools'],report['agent_specs']['baseline']['tools'])

    def test_failed_agent_baseline_also_preserves_exact_spec_and_no_rules(self):
        cfg=config(faults=[])
        fake=SyntheticModel()
        def fail(*args,**kwargs):
            raise RuntimeError('synthetic offline failure')
        with patch.object(engine,'model_request',side_effect=fail):
            with self.assertRaises(engine.IncompleteBaselineError) as caught:
                engine.run_experiment(cfg,include_rule_checks=False)
        report=caught.exception.partial_result
        self.assertEqual(report['execution_profile'],'agent-only-v1')
        self.assertEqual(report['agent_specs']['baseline'],engine.agent_spec(cfg,'baseline'))
        self.assertTrue(report['regression']['skipped'])

    def test_preview_tracks_task_policy_and_capabilities_without_model_or_storage(self):
        for scenario in ('equipment','ticket'):
            for policy in ('neutral','recovery'):
                raw={'scenario':scenario,'prompt_policy':policy,'request_id':'SYNTHETIC-PREVIEW','faults':[]}
                handler=self.handler('/api/agent-preview',{'config':raw})
                with patch.object(server,'model_request',side_effect=AssertionError('preview called model')), patch.object(server,'create_job',side_effect=AssertionError('preview stored job')):
                    handler.do_POST()
                status,value=handler.json.call_args.args
                self.assertEqual(status,200)
                cfg=engine.Config.from_dict(raw)
                for strategy in ('baseline','verified'):
                    self.assertEqual(value['variants'][strategy],engine.agent_spec(cfg,strategy))
                self.assertNotIn('verify_receipt',[t['function']['name'] for t in value['variants']['baseline']['tools']])
                self.assertIn('verify_receipt',[t['function']['name'] for t in value['variants']['verified']['tools']])

    def test_preview_rejects_credential_configuration(self):
        handler=self.handler('/api/agent-preview',{'config':{'model_config':{'api_key':'SYNTHETIC-PRIVATE'}}})
        handler.do_POST()
        self.assertEqual(handler.json.call_args.args[0],400)
        self.assertNotIn('SYNTHETIC-PRIVATE',str(handler.json.call_args.args))

    def test_agent_endpoint_rejects_rules_and_dispatches_agent_profile(self):
        invalid=self.handler('/api/run-agent',{'agent_mode':'rules'})
        with patch.object(server,'create_job') as create:
            invalid.do_POST()
            create.assert_not_called()
        self.assertEqual(invalid.json.call_args.args[0],400)
        valid=self.handler('/api/run-agent',{'agent_mode':'llm','model_config':{'base_url':'https://synthetic.invalid','model':'synthetic','api_key':'SYNTHETIC-PRIVATE'}})
        with patch.object(server,'create_job',return_value='synthetic-job'), patch.object(server.threading,'Thread') as thread:
            valid.do_POST()
        self.assertEqual(valid.json.call_args.args[0],202)
        self.assertTrue(thread.call_args.kwargs['args'][2])
        self.assertEqual(thread.call_args.kwargs['args'][1].agent_mode,'llm')

    def test_page_places_rules_inside_last_help_tab_and_keeps_unique_form_ids(self):
        root=Path(__file__).resolve().parent
        text=(root/'static/index.html').read_text(encoding='utf-8')
        page=PageStructure(text)
        self.assertEqual(len(page.ids),len(set(page.ids)))
        for name in ('page-concurrency','run-rule-check','rule-batch-host'):
            self.assertIn('page-usage',page.parents[name])
        for name in ('base-url','model','api-key','prompt-policy'):
            self.assertIn('experiment-form',page.parents[name])
        nav=text.split('<nav class="page-nav"',1)[1].split('</nav>',1)[0]
        self.assertTrue(nav.rstrip().endswith('使用方法</a>'))
        self.assertIn('href="#testing"',nav)
        self.assertNotIn('page-usage',page.parents['page-testing'])
        self.assertNotIn('href="#concurrency"',nav)
        self.assertIn('id="agent-tools"',text)
        self.assertIn('id="agent-system-prompt"',text)


if __name__=='__main__':
    unittest.main()
