"""Developer-only render diagnostics against an isolated local backend."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
data = ROOT.parent/'.pkg'/('qt-source-diagnostic-'+str(time.time_ns()))
data.mkdir(parents=True)
os.environ['ZHUIYIN_DATA_DIR'] = str(data)
os.environ['ZHUIYIN_EMBEDDED_PROBE'] = '1'
for port in range(8766, 8776):
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
            break
        except OSError:
            pass
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
out = (data/'backend.log').open('wb')
child = subprocess.Popen([sys.executable, str(ROOT/'desktop_entry.py'), '--serve', '--port', str(port)],
    env=os.environ.copy(), stdout=out, stderr=out, creationflags=subprocess.CREATE_NO_WINDOW)
session = None
try:
    for _ in range(100):
        record = data/'desktop-session.json'
        if record.is_file():
            session = json.loads(record.read_text(encoding='utf-8'))
            break
        time.sleep(0.1)
    assert session
    from PySide6.QtCore import QTimer, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView
    import desktop_shell
    desktop_shell.launch.ensure_service = lambda *args: f'http://127.0.0.1:{port}/#workbench'
    qInstallMessageHandler(lambda kind, context, message: print('QT:', message, flush=True))
    original = QWebEngineView.setPage
    def setpage(view, page):
        page.loadFinished.connect(lambda ok: print('LOADED', ok, page.url().toString(), flush=True))
        page.renderProcessTerminated.connect(lambda *args: print('RENDER STOP', args, flush=True))
        page.profile().downloadRequested.connect(lambda request: print('DOWNLOAD', request.downloadFileName(), flush=True))
        return original(view, page)
    QWebEngineView.setPage = setpage
    app = QApplication([])
    QTimer.singleShot(30000, app.quit)
    print('START', data, flush=True)
    desktop_shell.run_desktop()
    print('END', (data/'embedded-check.json').read_text(encoding='utf-8') if (data/'embedded-check.json').is_file() else 'No probe', flush=True)
finally:
    if session and (data/'desktop-session.json').is_file():
        req = urllib.request.Request(f'http://127.0.0.1:{port}/api/desktop/stop', data=json.dumps({'token': session['token']}).encode(), headers={'Content-Type': 'application/json'})
        opener.open(req, timeout=5).close()
    child.wait(timeout=8)
    out.close()
