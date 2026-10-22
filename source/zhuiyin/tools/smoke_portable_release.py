"""Final portable release check. Uses only generated data and a loopback fake model."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(port, route, body=None, timeout=6):
    req = urllib.request.Request(f'http://127.0.0.1:{port}'+route,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    with OPENER.open(req, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else raw


def wait_job(port, run_id):
    for _ in range(250):
        job = request(port, '/api/runs/'+run_id)
        if job['status'] != 'running':
            return job
        time.sleep(0.1)
    raise AssertionError('Synthetic release job did not terminate')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('delivery', type=Path)
    args = parser.parse_args()
    delivery = args.delivery.resolve()
    archive = delivery/'追因-便携版-Windows-x64.zip'
    work = ROOT.parent/'.pkg'/('portable-release-check-'+str(time.time_ns()))
    folder = work/'中文 空格路径'/'追因'
    work.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        names = bundle.namelist()
        assert not any('/追因数据/' in name or '/data/' in name for name in names)
        bundle.extractall(folder.parent)
    manifest = json.loads((folder/'构建清单.json').read_text(encoding='utf-8'))
    exe = folder/'追因.exe'
    invalid_temp = work/'NOT_A_TEMP_DIRECTORY'
    invalid_temp.write_text('intentionally not a directory', encoding='utf-8')
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('PYTHONHOME', 'PYTHONPATH', 'ZHUIYIN_DATA_DIR', 'ZHUIYIN_CAMPAIGN_POLICY')}
    environment['TEMP'] = environment['TMP'] = str(invalid_temp)
    environment['PATH'] = os.environ.get('SystemRoot', 'C:/Windows')+'/System32'
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    environment['ZHUIYIN_NO_BROWSER'] = '1'
    checks = []

    def launch(extra=()):
        started = time.monotonic()
        launcher = subprocess.Popen([str(exe), *extra], cwd=os.environ.get('SystemRoot', 'C:/Windows'),
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        launcher.wait(timeout=40)
        assert launcher.returncode == 0, launcher.returncode
        session = json.loads((folder/'追因数据'/'desktop-session.json').read_text(encoding='utf-8'))
        health = request(session['port'], '/api/health')
        assert health['desktop']['build_id'] == manifest['build_id']
        assert health['workbench_billing'] == 'run-billing-v1'
        return session, round(time.monotonic()-started, 3)

    def stop(session):
        request(session['port'], '/api/desktop/stop', {'token': session['token']})
        for _ in range(100):
            if not (folder/'追因数据'/'desktop-session.json').exists():
                return
            time.sleep(0.1)
        raise AssertionError('Portable release server did not stop')

    sys.path.insert(0, str(ROOT))
    from test_experiment_budget import SyntheticModel
    transport = SyntheticModel()
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            answer = json.loads(transport.open(SimpleNamespace(
                data=self.rfile.read(int(self.headers['Content-Length']))), timeout=1).read())
            answer['usage'] = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                               'prompt_cache_hit_tokens': 40, 'prompt_cache_miss_tokens': 60}
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *_):
            pass
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    config = {'scenario': 'ticket', 'faults': [], 'agent_mode': 'llm', 'max_requests': 30,
              'model_config': {'base_url': f'http://127.0.0.1:{provider.server_port}/v1',
                               'model': 'synthetic-release-only', 'api_key': ''},
              'billing': {'max_cost_cny': 1, 'input_cache_hit_price_per_million': 0.1,
                          'input_cache_miss_price_per_million': 1, 'output_price_per_million': 2}}
    session = None
    try:
        session, start_seconds = launch()
        port = session['port']
        assert launch()[0] == session
        checks += ['launcher_reuses_same_service', 'native_python_not_on_path',
                   'invalid_system_temp', 'chinese_and_space_path', 'unrelated_working_directory']
        for name, digest in manifest['files'].items():
            if name.startswith('static/'):
                route = '/' if name == 'static/index.html' else '/'+Path(name).relative_to('static').as_posix()
                assert hashlib.sha256(request(port, route)).hexdigest() == digest, name
        for name, mime in (('zhuiyin-logo.png', 'image/png'), ('favicon.png', 'image/png'), ('favicon.ico', 'image/x-icon')):
            with OPENER.open(f'http://127.0.0.1:{port}/assets/'+name, timeout=6) as response:
                assert response.headers['Content-Type'] == mime
                assert response.read() == (ROOT/'static/assets'/name).read_bytes()
        assert 'run_billing.py' in manifest['files']
        checks += ['all_web_assets_match_source', 'approved_logo_assets_bytes_and_mime', 'billing_source_in_manifest']
        for name in ('batch-example.zip', 'batch-equipment.csv', 'batch-tickets.csv'):
            assert request(port, '/api/templates/'+name)
        preview = request(port, '/api/agent-preview', {'config': {'scenario': 'ticket', 'faults': []}})
        assert 'create_ticket' in json.dumps(preview)
        checks += ['public_templates_available', 'agent_tools_preview']

        # A too-small allowance must be rejected before even the local fake receives a request.
        rejected = {**config, 'request_id': 'SYNTHETIC-RELEASE-COST-STOP',
                    'billing': {**config['billing'], 'max_cost_cny': 0.000001}}
        before = len(transport.requests)
        run = request(port, '/api/run-agent', rejected)
        stopped = wait_job(port, run['run_id'])
        result = stopped.get('partial_result') or stopped.get('result')
        assert stopped['status'] == 'error' and result['stopped_reason'] == 'cost_budget_exhausted'
        assert len(transport.requests) == before
        assert result['model_usage']['requests'] == 0
        checks += ['amount_gate_blocks_before_network_and_retains_partial_report']

        run = request(port, '/api/run-agent', {**config, 'request_id': 'SYNTHETIC-RELEASE-AGENT'})
        agent = wait_job(port, run['run_id'])
        assert agent['status'] == 'done', agent.get('error')
        result = agent['result']
        assert result['baseline']['actual_success'] is True
        assert result['model_usage']['prompt_cache_hit_tokens'] == result['model_usage']['requests'] * 40
        expected_cost = result['model_usage']['requests'] * 0.000104
        assert abs(result['model_usage']['estimated_cost_cny'] - expected_cost) < 1e-10
        assert result['run_budget']['max_cost_cny'] == 1
        assert result['provenance']['source_sha256']['run_billing.py'] == manifest['files']['run_billing.py']
        assert request(port, '/api/runs/'+run['run_id']+'/export')['baseline']['actual_success'] is True
        checks += ['frozen_agent_tool_loop', 'cache_split_and_three_price_cost',
                   'frozen_billing_provenance', 'report_export']

        study = request(port, '/api/study', {'config': {**config, 'request_id': 'SYNTHETIC-RELEASE-STUDY'},
            'options': {'repetitions': 2, 'strategies': ['baseline'], 'max_requests': 30}})
        study = wait_job(port, study['run_id'])
        assert study['status'] == 'done', study.get('error')
        assert study['result']['model_usage']['requests'] > 0
        assert study['result']['run_budget']['max_cost_cny'] == 1
        assert study['result']['model_usage']['prompt_cache_hit_tokens'] == study['result']['model_usage']['requests'] * 40
        checks += ['frozen_comparison_uses_shared_budget_and_prices']

        plan = request(port, '/api/test-suites/preview', {'files': [{
            'filename': 'release-batch.json', 'content': json.dumps({'kind': 'scenario', 'config': {
                'request_id': 'SYNTHETIC-RELEASE-BATCH', 'faults': []}})}]})
        batch = request(port, '/api/test-suites/start', {'suite_id': plan['suite_id'], 'profile': 'agent',
                                                       'agent_config': config})
        batch = wait_job(port, batch['run_id'])
        assert batch['status'] == 'done', batch.get('error')
        assert batch['result']['model_usage']['requests'] > 0
        assert batch['result']['model_usage']['limit'] == 30
        assert batch['result']['run_budget']['max_cost_cny'] == 1
        assert batch['result']['model_usage']['prompt_cache_hit_tokens'] == batch['result']['model_usage']['requests'] * 40
        checks += ['frozen_batch_uses_shared_budget_and_prices']

        imported = request(port, '/api/import', {'filename': 'release-synthetic.json', 'content': json.dumps({
            'kind': 'scenario', 'config': {'request_id': 'SYNTHETIC-RELEASE-IMPORT', 'faults': []}})})
        assert imported['kind'] == 'scenarios'
        for body in ({'token': 'wrong'}, {'token': None}):
            try:
                request(port, '/api/desktop/stop', body)
                raise AssertionError('Invalid stop token accepted')
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
        checks += ['user_import', 'authenticated_shutdown']
        stop(session)
        session = None
        session, restart_seconds = launch()
        restored = request(session['port'], '/api/runs/'+run['run_id'])
        assert restored['result']['baseline']['actual_success'] is True
        assert restored['result']['model_usage']['estimated_cost_cny'] == result['model_usage']['estimated_cost_cny']
        assert request(session['port'], '/api/history')['total'] >= 3
        checks += ['history_and_saved_cost_survive_restart']
    finally:
        if session is not None:
            stop(session)
        provider.shutdown()
        provider.server_close()
    outcome = {'passed': True, 'build_id': manifest['build_id'], 'format': 'onedir',
               'python': sys.version, 'checks': checks, 'external_model_requests': 0,
               'loopback_synthetic_requests': len(transport.requests),
               'start_seconds': start_seconds, 'restart_seconds': restart_seconds,
               'test_data': str(work), 'original_history_modified': False,
               'scope': 'Windows executable and loopback synthetic provider; not real-model effect evidence.'}
    (delivery/'便携版实测记录.json').write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(outcome, ensure_ascii=False))


if __name__ == '__main__':
    main()
