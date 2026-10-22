"""Run the frozen, visible Tk launcher with real event-loop readiness probes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    delivery = Path(sys.argv[1]).resolve()
    work = ROOT.parent/'.pkg'/('desktop-window-check-'+str(time.time_ns()))
    work.mkdir(parents=True)
    with zipfile.ZipFile(delivery/'追因-便携版-Windows-x64.zip') as bundle:
        assert bundle.testzip() is None
        bundle.extractall(work/'中文 窗口检查')
    executable = work/'中文 窗口检查'/'追因'/'追因.exe'
    manifest = json.loads((executable.parent/'构建清单.json').read_text(encoding='utf-8'))
    data = work/'独立状态数据'
    blocked = work/'invalid-temp-file'
    blocked.write_text('not a folder', encoding='utf-8')
    environment = {key: value for key, value in os.environ.items()
        if key not in ('ZHUIYIN_NO_BROWSER', 'ZHUIYIN_DATA_DIR', 'PYTHONHOME', 'PYTHONPATH', 'ZHUIYIN_CAMPAIGN_POLICY')}
    environment['TEMP'] = environment['TMP'] = str(blocked)
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    environment['ZHUIYIN_LAUNCHER_PROBE'] = '1'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    process = subprocess.Popen([str(executable), '--data-dir', str(data)],
        cwd=os.environ.get('SystemRoot', 'C:/Windows'), env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    session = None
    try:
        process.wait(timeout=45)
        assert process.returncode == 0
        session = json.loads((data/'desktop-session.json').read_text(encoding='utf-8'))
        url = 'http://127.0.0.1:'+str(session['port'])
        health = json.load(opener.open(url+'/api/health', timeout=5))
        assert health['desktop']['build_id'] == manifest['build_id']
        probe = json.loads((data/'desktop-window-check.json').read_text(encoding='utf-8'))
        for key in ('service_ready', 'window_viewable', 'open_enabled', 'copy_enabled', 'stop_enabled'):
            assert probe[key] is True, (key, probe)
        assert probe['browser_method'] == 'direct_browser', probe
        assert (data/'打开追因界面.html').is_file()
        output = {'passed': True, 'build_id': manifest['build_id'], 'probe': probe,
                  'invalid_system_temp': True, 'chinese_space_path': True,
                  'external_model_requests': 0, 'scope': 'Real frozen Tk visibility/event loop and browser process dispatch; manual Explorer double-click and OS security prompts are not automated.'}
        (delivery/'可见启动窗口实测.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(output, ensure_ascii=False))
    finally:
        if session is not None:
            request = urllib.request.Request('http://127.0.0.1:'+str(session['port'])+'/api/desktop/stop',
                data=json.dumps({'token': session['token']}).encode(), headers={'Content-Type': 'application/json'})
            opener.open(request, timeout=6).close()


if __name__ == '__main__':
    main()
