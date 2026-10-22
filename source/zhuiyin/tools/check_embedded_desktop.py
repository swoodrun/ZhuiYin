"""Exercise the actual embedded engine and download path of a shipped EXE."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    delivery = Path(sys.argv[1]).resolve()
    work = ROOT.parent/'.pkg'/('embedded-check-'+str(time.time_ns()))
    work.mkdir(parents=True)
    with zipfile.ZipFile(delivery/'追因-便携版-Windows-x64.zip') as bundle:
        assert bundle.testzip() is None
        bundle.extractall(work/'中文 内嵌窗口')
    exe = work/'中文 内嵌窗口'/'追因'/'追因.exe'
    data = work/'独立实测数据'
    blocked = work/'invalid-temp-file'
    blocked.write_text('not a directory', encoding='utf-8')
    environment = {key: value for key, value in os.environ.items() if key not in (
        'ZHUIYIN_NO_BROWSER', 'ZHUIYIN_LAUNCHER_PROBE', 'ZHUIYIN_DATA_DIR',
        'PYTHONHOME', 'PYTHONPATH', 'ZHUIYIN_CAMPAIGN_POLICY')}
    environment['TEMP'] = environment['TMP'] = str(blocked)
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    environment['ZHUIYIN_EMBEDDED_PROBE'] = '1'
    environment['PATH'] = os.environ.get('SystemRoot', 'C:/Windows')+'/System32'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    log = (work/'embedded-process.log').open('wb')
    process = subprocess.Popen([str(exe), '--data-dir', str(data)], env=environment,
        cwd=os.environ.get('SystemRoot', 'C:/Windows'), creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    session = None
    try:
        for _ in range(600):
            record = data/'desktop-session.json'
            if record.is_file():
                session = json.loads(record.read_text(encoding='utf-8'))
            if process.poll() is not None:
                break
            time.sleep(0.1)
        assert process.poll() == 0, 'Embedded app did not exit cleanly; see '+str(work)
        # The native .NET host writes Encoding.UTF8, which includes a BOM.
        value = json.loads((data/'embedded-check.json').read_text(encoding='utf-8-sig'))
        assert value['window_visible'] is True
        assert value['engine'] == 'WebView2'
        assert value['off_the_record'] is True
        assert value['external_browser_requested'] is False
        assert value['browser_process_id'] > 0
        assert value['dom']['workbench'] is True
        assert '已就绪' in value['dom']['ready']
        assert value['dom']['fileInputs'] >= 2 and value['dom']['batchMultiple'] is True
        icons = value['native_icons']
        assert icons['app_user_model_id'] == 'Zhuiyin.Desktop' and icons['show_in_taskbar'] is True
        assert icons['icon_file_sha256'] == hashlib.sha256((ROOT/'packaging/assets/zhuiyin.ico').read_bytes()).hexdigest()
        assert icons['big_handle_present'] and icons['small_handle_present']
        assert icons['big_matches_loaded_icon'] and icons['small_matches_loaded_icon'], icons
        for name, layout in value['layouts'].items():
            left, right, button, form = (layout[key] for key in ('left', 'right', 'button', 'form'))
            viewport = layout['viewport']
            assert layout['logo'] and layout['favicon'], (name, layout)
            assert viewport['scrollWidth'] <= viewport['width'] + 1, (name, 'horizontal overflow')
            assert abs(button['x'] + button['width']/2 - form['x'] - form['width']/2) < 2
            assert button['width'] >= form['width'] * 0.6
            assert button['y'] >= 0 and button['bottom'] <= viewport['height'] + 1, (name, 'start button not visible')
            menu = layout['more_menu']
            assert menu['visible'] and menu['top'] >= 0 and menu['bottom'] <= button['bottom'], (name, 'footer menu not visible above its button')
            if name.startswith('desktop'):
                assert abs(left['y'] - right['y']) < 2 and right['x'] >= left['right']
            else:
                assert abs(left['x'] - right['x']) < 2 and right['y'] >= left['bottom']
        for page in ('workbench', 'testing', 'import', 'statistics', 'history', 'usage'):
            clicked = value['page_clicks'][page]
            assert clicked == {'hash': '#'+page, 'visible': True, 'active': True}, (page, clicked)
        template = (ROOT/'templates/scenarios.csv').read_bytes()
        downloaded = (data/'embedded-probe-download.csv').read_bytes()
        assert downloaded == template, 'Engine download differs from the shipped template'
        assert value['download_bytes'] == len(downloaded)
        assert value['download_sha256'] == hashlib.sha256(downloaded).hexdigest()
        assert not (data/'desktop-session.json').exists(), 'Window close did not stop its service'
        origin = urlsplit(value['url'])
        assert origin.scheme == 'http' and origin.hostname == '127.0.0.1' and 8766 <= origin.port <= 8775
        try:
            with socket.create_connection((origin.hostname, origin.port), timeout=0.5):
                raise AssertionError('Window close left its service listening')
        except OSError:
            pass
        assert (data/'embedded-window.png').stat().st_size > 10000
        checks = ['real_embedded_frontend', 'window_visible', 'frontend_health_ready',
                  'webview2_inprivate_profile', 'file_input_and_multiple_upload_contract',
                  'engine_download_bytes_match_template', 'window_close_stops_service',
                  'invalid_temp', 'unicode_space_path', 'no_external_browser_dispatch',
                  'six_navigation_pages_click_and_display', 'approved_logo_and_favicon_loaded',
                  'approved_native_window_icons_and_taskbar_identity', 'two_columns_at_100_and_125_percent',
                  'narrow_390_single_column_no_horizontal_overflow', 'centered_wide_start_button_visible_at_all_sizes',
                  'footer_more_menu_opens_upward_and_stays_visible']
        output = {'passed': True, 'checks': checks, 'engine_observation': value,
                  'external_model_requests': 0, 'external_browser_requests': 0,
                  'scope': 'Actual frozen WebView2 page rendering, DOM readiness, native engine template download, and window-close service shutdown. No model run or external model API call is initiated. File-dialog interaction, actual file upload, and real-model behavior are not claimed.'}
        (delivery/'内嵌窗口实测.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
        shutil.copyfile(data/'embedded-window.png', delivery/'内嵌窗口实屏.png')
        for source, target in [('workbench-100.png', '工作台-100百分比.png'), ('workbench-125.png', '工作台-125百分比.png'), ('workbench-narrow.png', '工作台-窄窗口.png')]:
            shutil.copyfile(data/source, delivery/target)
        print(json.dumps(output, ensure_ascii=False))
    finally:
        if session is not None and (data/'desktop-session.json').is_file():
            req = urllib.request.Request('http://127.0.0.1:'+str(session['port'])+'/api/desktop/stop',
                data=json.dumps({'token': session['token']}).encode(), headers={'Content-Type': 'application/json'})
            try:
                opener.open(req, timeout=6).close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()


if __name__ == '__main__':
    main()
