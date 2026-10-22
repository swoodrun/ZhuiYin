"""Double-click launcher for the local verification application."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from engine import APP_VERSION
from test_suite import WORKFLOW_VERSION
from runtime_paths import data_root, desktop_identity

ROOT = Path(__file__).resolve().parent


def _browser_executable(command):
    """Read only a known browser executable, never execute registry shell text."""
    match = re.match(r'^\s*(?:"([^"]+\.exe)"|([^\s]+\.exe))(?:\s|$)', command or '', re.I)
    if not match:
        return None
    path = Path(match.group(1) or match.group(2))
    if path.name.lower() not in {'msedge.exe', 'chrome.exe', 'firefox.exe', 'brave.exe', 'opera.exe'}:
        return None
    return path if path.is_absolute() and path.is_file() else None


def windows_browser():
    """Prefer the registered default browser, then an installed known browser."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice') as key:
            program = winreg.QueryValueEx(key, 'ProgId')[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, program+r'\shell\open\command') as key:
            path = _browser_executable(winreg.QueryValueEx(key, None)[0])
        if path:
            return path
    except OSError:
        pass
    for browser in ('msedge.exe', 'chrome.exe', 'firefox.exe', 'brave.exe', 'opera.exe'):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths'+'\\'+browser,
                                        0, winreg.KEY_READ | view) as key:
                        value = winreg.QueryValueEx(key, None)[0]
                    path = _browser_executable('"'+value.strip('"')+'"')
                    if path:
                        return path
                except OSError:
                    pass
    return None


def open_page(url):
    if os.environ.get('ZHUIYIN_NO_BROWSER') == '1':
        return 'disabled'
    if getattr(sys, 'frozen', False) and sys.platform == 'win32':
        import ctypes
        # A rundll32 process can exit without displaying a page. Prefer a real
        # browser executable; the desktop window still exposes the URL if the
        # browser declines to show it. Never inherit the bundle's DLL directory.
        ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:
            browser = windows_browser()
            command = [str(browser), url] if browser else [str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32' / 'rundll32.exe'),
                                                          'url.dll,FileProtocolHandler', url]
            clean_environment = {key: value for key, value in os.environ.items()
                                 if not key.startswith(('_PYI_', 'PYINSTALLER_')) and key not in ('PYTHONHOME', 'PYTHONPATH')}
            subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW, env=clean_environment)
            return 'direct_browser' if browser else 'windows_url_handler'
        finally:
            ctypes.windll.kernel32.SetDllDirectoryW(str(getattr(sys, '_MEIPASS', ROOT)))
    else:
        return 'system_browser' if webbrowser.open(url) else 'not_opened'


def healthy(port):
    try:
        if getattr(sys, 'frozen', False):
            with socket.create_connection(('127.0.0.1', port), timeout=0.15):
                pass
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            value = json.load(response)
        if getattr(sys, 'frozen', False) and value.get('desktop') != desktop_identity():
            return False
        return value.get("workbench_billing") == "run-billing-v1" and value.get("agent_workbench") == "agent-ui-v1" and value.get("version") == APP_VERSION and value.get("concurrency") is True and value.get("default_mode") == "rules" and value.get("history") is True and "corrupt_payload" in value.get("faults", {}) and value.get("test_suites") is True and value.get("test_workflow_version") == WORKFLOW_VERSION
    except Exception:
        return False


def ensure_service(page='workbench', on_status=None):
    report = on_status or (lambda message: None)
    report('正在检查本机服务…')
    for port in range(8766, 8776):
        if healthy(port):
            return f"http://127.0.0.1:{port}/#{page}"
    # Reuse a matching service anywhere in the range before choosing a free port.
    for port in range(8766, 8776):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        logs = data_root()
        logs.mkdir(parents=True, exist_ok=True)
        report('正在启动追因服务…')
        frozen = getattr(sys, 'frozen', False)
        command = ([sys.executable, '--serve', '--port', str(port)] if frozen else
                   [sys.executable, str(ROOT / 'server.py'), '--port', str(port)])
        environment = os.environ.copy()
        if frozen:
            # The server outlives this launcher, including one-file extraction.
            environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        with (logs / "server.log").open("ab") as out, (logs / "server-error.log").open("ab") as err:
            child = subprocess.Popen(command,
                             # A one-file launcher must not leave its extraction
                             # folder as the persistent server's working directory.
                             cwd=logs if frozen else ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             env=environment,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                             close_fds=True)
        for _ in range(150 if frozen else 30):
            if healthy(port):
                return f"http://127.0.0.1:{port}/#{page}"
            if child.poll() is not None:
                raise RuntimeError('后台进程提前退出，请查看启动日志。')
            time.sleep(0.2)
        raise RuntimeError("服务启动失败，请检查 data/server-error.log")
    raise RuntimeError("8766—8775 端口均被占用，请关闭旧服务后重试")


def main():
    parser = argparse.ArgumentParser(description="打开追因本机页面")
    parser.add_argument("--page", choices=("workbench", "testing"), default="workbench")
    args = parser.parse_args()
    open_page(ensure_service(args.page))


if __name__ == "__main__":
    main()
