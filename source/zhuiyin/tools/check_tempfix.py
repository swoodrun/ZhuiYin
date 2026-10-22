"""Adversarial startup check: system TEMP is a file, not a directory."""
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(port, route, body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}'+route,
        data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with OPENER.open(req, timeout=5) as reply:
        raw = reply.read()
        return json.loads(raw) if reply.headers['Content-Type'].startswith('application/json') else raw


def main():
    delivery = Path(sys.argv[1]).resolve()
    archive = delivery/'追因-单文件启动修复版-Windows-x64.zip'
    if not archive.exists():
        archive = delivery/'追因-单文件版-Windows-x64.zip'
    work = ROOT.parent/'.pkg'/('tempfix-check-'+str(int(time.time())))
    work.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None
        assert not any('/追因数据/' in n or '/追因运行缓存/' in n for n in package.namelist())
        package.extractall(work/'中文 空格路径')
    folder = work/'中文 空格路径'/'追因'
    exe = folder/'追因.exe'
    blocked = work/'NOT_A_TEMP_DIRECTORY'
    blocked.write_text('invalid TEMP test fixture', encoding='utf-8')
    environment = os.environ.copy()
    environment['TEMP'] = environment['TMP'] = str(blocked)
    environment['PATH'] = os.environ.get('SystemRoot', 'C:/Windows')+'/System32'
    for name in ('PYTHONPATH', 'PYTHONHOME', 'ZHUIYIN_DATA_DIR', 'ZHUIYIN_NO_BROWSER'):
        environment.pop(name, None)
    manifest = json.loads((folder/'构建清单.json').read_text(encoding='utf-8'))

    def start(extra=(), data=None):
        data = data or folder/'追因数据'
        began = time.monotonic()
        process = subprocess.Popen([str(exe), *extra], cwd=os.environ.get('SystemRoot', 'C:/Windows'),
            env=environment, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.wait(timeout=35)
        assert process.returncode == 0, process.returncode
        session = json.loads((data/'desktop-session.json').read_text())
        health = request(session['port'], '/api/health')
        assert health['desktop']['build_id'] == manifest['build_id']
        return session, round(time.monotonic()-began, 3)

    def stop(session):
        request(session['port'], '/api/desktop/stop', {'token': session['token']})
        for _ in range(80):
            try:
                request(session['port'], '/api/health')
            except OSError:
                return
            time.sleep(0.1)
        raise AssertionError('Test server did not stop')

    session, seconds = start()  # No args: same entry as a normal double-click.
    try:
        port = session['port']
        assert b'#testing' in request(port, '/')
        cached = list((folder/'追因运行缓存').glob('*/core.exe'))
        assert len(cached) == 1
        core = cached[0]
        assert hashlib.sha256(core.read_bytes()).hexdigest() == manifest['launcher']['payload_sha256']
        assert list((core.parent/'temp').glob('_MEI*'))
        again, repeat_seconds = start()
        assert again == session, 'Repeated launch replaced the running service'
        run = request(port, '/api/run', {'request_id': 'SYNTHETIC-TEMP-REGRESSION', 'faults': []})
        for _ in range(100):
            report = request(port, '/api/runs/'+run['run_id'])
            if report['status'] in ('done', 'error'):
                break
            time.sleep(0.1)
        assert report['status'] == 'done', report.get('error')
        assert report['result']['baseline']['actual_success'] is True
        assert report['result']['model_usage']['requests'] == 0
    finally:
        stop(session)
    # Damage only this isolated test's cached binary; the embedded copy must repair it.
    for _ in range(50):
        try:
            core.write_bytes(b'SYNTHETIC CACHE CORRUPTION')
            break
        except PermissionError:
            time.sleep(0.1)
    assert core.read_bytes() == b'SYNTHETIC CACHE CORRUPTION'
    session, restart_seconds = start()
    try:
        assert hashlib.sha256(core.read_bytes()).hexdigest() == manifest['launcher']['payload_sha256']
        old = request(session['port'], '/api/runs/'+run['run_id'])
        assert old['result']['baseline']['actual_success'] is True
    finally:
        stop(session)
    alternate = work/'独立数据 带空格'
    session, _ = start(('--data-dir', str(alternate), '--no-browser', '--page', 'testing'), data=alternate)
    try:
        assert (alternate/'runs/history.sqlite').is_file()
    finally:
        stop(session)
    inspected = work/'native-dependencies.exe'
    shutil.copyfile(exe, inspected)
    imports = subprocess.check_output(['objdump', '-p', str(inspected)], text=True, encoding='utf-8', errors='replace')
    dlls = [line.strip().split(':', 1)[1].strip() for line in imports.splitlines() if 'DLL Name:' in line]
    assert not any(name.lower().startswith(('libstdc++', 'libgcc', 'libwinpthread')) for name in dlls)
    output = {'passed': True, 'system_temp_intentionally_invalid': True,
        'normal_no_argument_start': True, 'working_directory_unrelated_to_exe': True,
        'python_absent_from_path': True, 'app_local_extraction_confirmed': True,
        'repeated_start_reuses_session': True, 'cache_corruption_repaired': True,
        'sqlite_history_survives_restart': True, 'unicode_space_arguments_preserved': True,
        'external_model_requests': 0, 'dll_imports': dlls,
        'seconds': {'first_start': seconds, 'repeat': repeat_seconds, 'restart': restart_seconds}}
    (delivery/'临时目录故障回归实测.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
