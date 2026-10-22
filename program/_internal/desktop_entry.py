"""Windowless desktop entry point; the existing web UI remains unchanged."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.request

from runtime_paths import data_root


def notify(message, error=False):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, '追因', 0x10 if error else 0x40)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def stop_service():
    record = data_root() / 'desktop-session.json'
    if not record.is_file():
        return '没有找到正在运行的 EXE 服务。'
    session = json.loads(record.read_text(encoding='utf-8'))
    port = session['port']
    if isinstance(port, bool) or not isinstance(port, int) or not 8766 <= port <= 8775:
        raise ValueError('本机服务记录无效')
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/desktop/stop',
        data=json.dumps({'token': session['token']}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return json.load(response)['message']
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise ValueError('仍有实验运行，请先在网页停止或等待完成，再退出追因。') from None
        raise ValueError('无法关闭该服务；请检查数据目录中的日志。') from None


def main():
    parser = argparse.ArgumentParser(description='追因桌面启动器')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    group.add_argument('--stop', action='store_true', help='退出本用户的 EXE 服务')
    parser.add_argument('--port', type=int, default=8766, help=argparse.SUPPRESS)
    parser.add_argument('--page', choices=('workbench', 'testing'), default='workbench')
    parser.add_argument('--data-dir', type=Path, help='指定独立的绝对数据目录')
    parser.add_argument('--no-browser', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.no_browser:
        os.environ['ZHUIYIN_NO_BROWSER'] = '1'
    if args.data_dir:
        if not args.data_dir.is_absolute():
            raise ValueError('数据目录必须使用绝对路径')
        os.environ['ZHUIYIN_DATA_DIR'] = str(args.data_dir)
    if args.stop:
        notify(stop_service())
    elif args.serve:
        import server
        token = secrets.token_urlsafe(32)
        server.DESKTOP_TOKEN = token
        record = data_root() / 'desktop-session.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        # Written after the listener is bound, so a competing launcher cannot
        # replace the live service's control record on a failed bind.
        def ready():
            record.write_text(json.dumps({'port': args.port, 'token': token}), encoding='utf-8')
        sys.argv = [sys.argv[0], '--port', str(args.port)]
        try:
            server.main(on_ready=ready)
        finally:
            try:
                if json.loads(record.read_text(encoding='utf-8')).get('token') == token:
                    record.unlink()
            except (OSError, ValueError):
                pass
    else:
        if getattr(sys, 'frozen', False) and sys.platform == 'win32' and os.environ.get('ZHUIYIN_NO_BROWSER') != '1':
            from desktop_shell import run_desktop
            return run_desktop(args.page)
        else:
            import launch
            sys.argv = [sys.argv[0], '--page', args.page]
            launch.main()


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except Exception:
        import traceback
        folder = data_root()
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'desktop-error.log').open('a', encoding='utf-8') as stream:
            traceback.print_exc(file=stream)
        notify('追因启动或退出失败。详情请查看：\n' + str(folder / 'desktop-error.log'), error=True)
        sys.exit(1)
