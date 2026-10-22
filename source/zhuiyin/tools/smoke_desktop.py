"""Exercise shipped Windows executables with synthetic input and zero model calls."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(port, path, value=None, timeout=8):
    data = None if value is None else json.dumps(value).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}'+path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with OPENER.open(req, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else raw


def main():
    delivery = Path(sys.argv[1]).resolve()
    manifest = json.loads((delivery/'便携版'/'追因'/'构建清单.json').read_text(encoding='utf-8'))
    checkroot = ROOT.parent/'.pkg'/'desktop-smoke'/(manifest['build_id']+'-'+str(int(time.time())))
    checkroot.mkdir(parents=True, exist_ok=True)
    results = []
    for label, port in [('便携版', 8774), ('单文件版', 8775)]:
        target = checkroot/(label+' 中文 空格路径')
        archive = delivery/f'追因-{label}-Windows-x64.zip'
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.namelist()
            assert not any('/data/' in name or 'api_key' in name.lower() for name in members)
            bundle.extractall(target)
        folder = target/'追因'
        executable = folder/'追因.exe'
        data = checkroot/(label+'持久数据')
        environment = {key: value for key, value in os.environ.items() if key not in ('PYTHONHOME', 'PYTHONPATH')}
        environment['PATH'] = os.environ.get('SystemRoot', 'C:/Windows')+'/System32'
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        temporary = checkroot/'temp'
        temporary.mkdir(exist_ok=True)
        environment['TEMP'] = environment['TMP'] = str(temporary)
        log = (checkroot/(label+'.log')).open('ab')

        def start(exe):
            process = subprocess.Popen([str(exe), '--serve', '--port', str(port), '--data-dir', str(data)],
                cwd=checkroot, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW)
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError('Executable exited: '+label)
                try:
                    health = request(port, '/api/health', timeout=0.3)
                    assert health['desktop']['build_id'] == manifest['build_id']
                    return process, health
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            raise RuntimeError('Executable did not become ready: '+label)

        def stop(process):
            session = json.loads((data/'desktop-session.json').read_text(encoding='utf-8'))
            reply = request(port, '/api/desktop/stop', {'token': session['token']})
            assert '退出' in reply['message']
            process.wait(timeout=15)
            assert process.returncode == 0

        process, health = start(executable)
        try:
            page = request(port, '/')
            assert b'#testing' in page
            for name, digest in manifest['files'].items():
                if name.startswith('static/'):
                    route = '/' if name == 'static/index.html' else '/'+Path(name).name
                    assert hashlib.sha256(request(port, route)).hexdigest() == digest, name
            preview = request(port, '/api/agent-preview', {'config': {'scenario': 'ticket', 'faults': []}})
            assert 'create_ticket' in json.dumps(preview)
            # This invokes the frozen inspect.getsource/provenance path as well
            # as SQLite output persistence, without a real model connection.
            job = request(port, '/api/run', {'request_id': 'SYNTHETIC-EXE-SMOKE', 'faults': []})
            for _ in range(100):
                report = request(port, '/api/runs/'+job['run_id'])
                if report['status'] in ('done', 'error'):
                    break
                time.sleep(0.1)
            assert report['status'] == 'done', report.get('error')
            assert report['result']['baseline']['actual_success'] is True
            assert report['result']['model_usage']['requests'] == 0
            assert report['result']['provenance']['source_sha256']['engine.py'] == manifest['files']['engine.py']
            exported = request(port, '/api/runs/'+job['run_id']+'/export')
            assert exported['baseline']['actual_success'] is True
            uploaded = request(port, '/api/import', {'filename': 'synthetic-desktop.json', 'content': json.dumps({
                'kind': 'scenario', 'name': 'SYNTHETIC EXE IMPORT', 'config': {
                    'request_id': 'SYNTHETIC-EXE-IMPORT', 'faults': []}})})
            assert uploaded['kind'] == 'scenarios'
            # A loopback-only synthetic provider exercises the frozen HTTP/model
            # path, without a provider account, secrets or a chargeable request.
            sys.path.insert(0, str(ROOT))
            from test_experiment_budget import SyntheticModel
            transport = SyntheticModel()
            class Provider(BaseHTTPRequestHandler):
                def do_POST(self):
                    raw = self.rfile.read(int(self.headers['Content-Length']))
                    answer = transport.open(SimpleNamespace(data=raw), timeout=1).read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(answer)))
                    self.end_headers()
                    self.wfile.write(answer)
                def log_message(self, *_):
                    pass
            provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
            threading.Thread(target=provider.serve_forever, daemon=True).start()
            try:
                agent = request(port, '/api/run-agent', {'request_id': 'SYNTHETIC-EXE-AGENT',
                    'faults': [], 'agent_mode': 'llm', 'max_requests': 30,
                    'model_config': {'base_url': f'http://127.0.0.1:{provider.server_port}/v1',
                                     'model': 'synthetic-local-only', 'api_key': ''}})
                for _ in range(100):
                    actual = request(port, '/api/runs/'+agent['run_id'])
                    if actual['status'] in ('done', 'error'):
                        break
                    time.sleep(0.1)
                assert actual['status'] == 'done', actual.get('error')
                assert actual['result']['execution_profile'] == 'agent-only-v1'
                assert actual['result']['baseline']['actual_success'] is True
                assert actual['result']['model_usage']['requests'] == len(transport.requests) > 0
            finally:
                provider.shutdown()
                provider.server_close()
            try:
                request(port, '/api/desktop/stop', {'token': 'wrong'})
                raise AssertionError('Invalid exit token accepted')
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
        finally:
            stop(process)
        # Move the distribution and restart it against the same data folder.
        moved = checkroot/(label+' 移动后')
        shutil.move(str(folder), str(moved))
        restarted, _ = start(moved/'追因.exe')
        try:
            restored = request(port, '/api/runs/'+job['run_id'])
            assert restored['status'] == 'done'
            assert restored['result']['baseline']['actual_success'] is True
        finally:
            stop(restarted)
            log.close()
        results.append({'format': label, 'passed': True, 'port': port,
                        'external_model_requests': 0, 'local_synthetic_requests': len(transport.requests), 'source_data_untouched': True,
                        'checks': ['中文空格路径', 'PATH中无Python', '全部网页资源哈希', '工具预览',
                                   '冻结源码溯源', 'SQLite实验与导出', '用户导入', '退出鉴权',
                                   '迁移程序后重启与历史恢复', '本机合成模型HTTP往返与Agent工具循环']})
    output = {'build_id': manifest['build_id'], 'passed': True, 'results': results}
    (delivery/'EXE实测记录.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
