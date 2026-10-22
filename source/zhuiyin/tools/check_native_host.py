"""Test the real WebView2 host against a fresh local backend; no model calls."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
data = ROOT.parent/'.pkg'/('native-webview-check-'+str(time.time_ns()))
data.mkdir(parents=True)
environment = os.environ.copy()
environment['ZHUIYIN_DATA_DIR'] = str(data)
environment['ZHUIYIN_EMBEDDED_PROBE'] = '1'
for port in range(8766, 8776):
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
            break
        except OSError:
            pass
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
log = (data/'server.log').open('wb')
server = subprocess.Popen([sys.executable, str(ROOT/'desktop_entry.py'), '--serve', '--port', str(port)],
    env=environment, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
session = None
try:
    for _ in range(100):
        record = data/'desktop-session.json'
        if record.is_file():
            session = json.loads(record.read_text(encoding='utf-8'))
            break
        time.sleep(0.1)
    assert session
    host = ROOT.parent/'.pkg'/'webview-host-test'/'追因窗口.exe'
    process = subprocess.Popen([str(host), 'http://127.0.0.1:'+str(port)+'/#workbench', str(data)],
        env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    print('Native host PID', process.pid, 'data', data, flush=True)
    process.wait(timeout=60)
    print('EXIT', process.returncode, flush=True)
    value = json.loads((data/'embedded-check.json').read_text(encoding='utf-8-sig'))
    assert value['engine'] == 'WebView2' and value['dom']['workbench'] is True
    assert value['off_the_record'] is True
    assert value['dom']['fileInputs'] >= 2
    print(json.dumps(value, ensure_ascii=False), flush=True)
    print('SCREENSHOT', str(data/'embedded-window.png'), flush=True)
finally:
    if session and (data/'desktop-session.json').is_file():
        request = urllib.request.Request('http://127.0.0.1:'+str(port)+'/api/desktop/stop',
            data=json.dumps({'token': session['token']}).encode(), headers={'Content-Type': 'application/json'})
        try:
            opener.open(request, timeout=5).close()
        except OSError:
            pass
    server.wait(timeout=8)
    log.close()
