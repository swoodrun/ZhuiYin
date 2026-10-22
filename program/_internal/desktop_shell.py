"""Windows WebView2 host: the frontend stays inside the application window."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import launch
from runtime_paths import data_root, resource_root


def same_origin(url, origin):
    try:
        target, source = urlsplit(url), urlsplit(origin)
        return (target.scheme == source.scheme == 'http' and
                target.hostname == source.hostname == '127.0.0.1' and
                target.port == source.port and not target.username and not target.password)
    except (TypeError, ValueError):
        return False


def navigation_allowed(url, origin):
    if not isinstance(url, str):
        return False
    if url == 'about:blank' or url.startswith(('data:', 'qrc:')):
        return True
    if url.startswith('blob:'):
        return same_origin(url[5:], origin)
    return bool(origin) and same_origin(url, origin)


def download_name(value):
    value = str(value or '').replace('\\', '/').rsplit('/', 1)[-1]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', value).strip(' .')[:180].strip(' .')
    if not value or value.split('.', 1)[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
            *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        return '追因结果.json'
    return value


def run_desktop(page='workbench'):
    url = launch.ensure_service(page)
    host = resource_root()/'webview-host'/'追因窗口.exe'
    if not host.is_file():
        raise RuntimeError('内嵌窗口组件缺失，请完整解压最新版本并保留_internal目录。')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('_PYI_', 'PYINSTALLER_')) and key not in ('PYTHONHOME', 'PYTHONPATH')}
    # The .NET host uses the already-installed Microsoft runtime. Do not pass
    # PyInstaller's DLL search directory into another executable.
    import ctypes
    ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        process = subprocess.Popen([str(host), url, str(data_root())], env=environment,
            cwd=data_root(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    finally:
        import sys
        ctypes.windll.kernel32.SetDllDirectoryW(str(getattr(sys, '_MEIPASS', resource_root())))
    return process.wait()
