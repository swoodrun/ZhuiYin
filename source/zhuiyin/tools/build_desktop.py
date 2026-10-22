"""Explicit allow-list desktop build: never bundle histories, keys or test data."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from build_tempfix import build_wrapper, SOURCE as BOOTSTRAP_SOURCE
from build_webview_host import build_host, ICON, SDK, SDK_VERSION


ROOT = Path(__file__).resolve().parents[1]
MODULES = ('desktop_entry.py', 'runtime_paths.py', 'launch.py', 'server.py', 'engine.py',
           'credentials.py', 'model_budget.py', 'job_store.py', 'ingest.py', 'study.py',
           'concurrency_lab.py', 'test_suite.py', 'agent_suite.py', 'run_billing.py', 'desktop_shell.py',
           'diagnostics.py', 'experiment_compare.py', 'task_templates.py', 'product_workflows.py', 'retest_templates.py')


def main():
    parser = argparse.ArgumentParser(description='Build a clean Windows desktop distribution')
    parser.add_argument('--format', choices=('onedir', 'onefile', 'both'), default='onedir')
    args = parser.parse_args()
    import PyInstaller
    sources = [ROOT/name for name in MODULES]
    product_docs = [ROOT / '产品升级使用说明_20261021.md']
    assets = [path for folder in ('static', 'templates') for path in sorted((ROOT/folder).rglob('*')) if path.is_file()]
    host_inputs = [ROOT/'packaging'/'WebViewHost.cs', ROOT/'packaging'/'WebViewHost.manifest',
                   ROOT/'tools'/'build_webview_host.py', ICON, ICON.with_name('icon-provenance.json')]
    hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sources+assets+host_inputs+product_docs}
    build_inputs = json.dumps({'files': hashes, 'python': sys.version, 'pyinstaller': PyInstaller.__version__,
                              'format': args.format, 'webview2_sdk': SDK_VERSION}, sort_keys=True).encode()
    build_id = hashlib.sha256(build_inputs + Path(__file__).read_bytes() + BOOTSTRAP_SOURCE.read_bytes()).hexdigest()[:20]
    work = ROOT.parent / '.pkg' / 'desktop-build' / build_id
    work.mkdir(parents=True, exist_ok=True)
    host = build_host(work/'webview-host')
    sys.path.insert(0, str(ROOT))
    from engine import APP_VERSION
    manifest = {'build_id': build_id, 'app_version': APP_VERSION, 'packaging': 'desktop-v6-embedded-webview2',
                'python': sys.version.split()[0], 'python_base_executable': getattr(sys, '_base_executable', sys.executable),
                'pyinstaller': PyInstaller.__version__, 'webview2_sdk': SDK_VERSION,
                'engine': 'WebView2', 'format': args.format, 'files': hashes,
                'app_icon': ICON.relative_to(ROOT).as_posix(),
                'app_icon_sha256': hashes[ICON.relative_to(ROOT).as_posix()]}
    (work/'desktop-build.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    delivery = ROOT/'data'/'deliveries'/('追因-Windows-'+build_id)
    delivery.mkdir(parents=True, exist_ok=True)
    datas = [(str(path), '.') for path in sources]
    datas += [(str(ROOT/folder), folder) for folder in ('static', 'templates')]
    datas += [(str(work/'desktop-build.json'), '.')]
    datas += [(str(host), 'webview-host')]
    for kind, label in [('onedir', '便携版'), ('onefile', '单文件版')]:
        if args.format not in (kind, 'both'):
            continue
        spec = work/(kind+'.spec')
        program = (f'a = Analysis([{str(ROOT/"desktop_entry.py")!r}], pathex=[{str(ROOT)!r}], '
                   f'binaries=[], datas={datas!r}, hiddenimports=[], hookspath=[], hooksconfig={{}}, '
                   'runtime_hooks=[], excludes=["tkinter", "PyQt5", "PyQt6", "PySide6", "shiboken6"], noarchive=False, optimize=0)\n'
                   'pyz = PYZ(a.pure)\n')
        if kind == 'onedir':
            program += ('exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="追因", '
                        f'debug=False, strip=False, upx=False, console=False, icon={str(ICON)!r})\n'
                        'coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="追因")\n')
        else:
            program += ('exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="追因", '
                        f'debug=False, strip=False, upx=False, console=False, icon={str(ICON)!r})\n')
        spec.write_text(program, encoding='utf-8')
        import os
        environment = os.environ.copy()
        system = Path(os.environ.get('SystemRoot', 'C:/Windows'))
        environment['PATH'] = os.pathsep.join(map(str, (Path(sys.executable).parent, Path(sys.base_prefix),
                                                       Path(sys.base_prefix)/'DLLs', system/'System32', system)))
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                        '--workpath', str(work/kind), '--distpath', str(delivery/label), str(spec)],
                       env=environment, check=True)
        folder = delivery/label/('追因' if kind == 'onedir' else '')
        wrapper = None
        if kind == 'onefile':
            payload = work/'onefile-core.exe'
            shutil.copyfile(folder/'追因.exe', payload)
            wrapper = build_wrapper(payload, folder/'追因.exe', work/'native-bootstrap')
            # The native bootstrap replaces the PyInstaller EXE, so brand its final PE resources too.
            from PyInstaller.utils.win32.icon import CopyIcons_FromIco
            CopyIcons_FromIco(str(folder/'追因.exe'), [str(ICON)])
            wrapper['app_icon_sha256'] = manifest['app_icon_sha256']
            wrapper['exe_sha256'] = hashlib.sha256((folder/'追因.exe').read_bytes()).hexdigest()
        (folder/'退出追因.cmd').write_bytes('@echo off\r\nchcp 65001 >nul\r\nstart "" "%~dp0追因.exe" --stop\r\n'.encode('utf-8'))
        (folder/'打开批量测试.cmd').write_bytes('@echo off\r\nchcp 65001 >nul\r\nstart "" "%~dp0追因.exe" --page testing\r\n'.encode('utf-8'))
        (folder/'使用说明.txt').write_text('''追因 Windows 桌面版

1. 完整解压后，双击“追因.exe”，前端页面直接显示在软件窗口中，不打开外部浏览器。
   无需安装 Python；运行前须已安装 Microsoft Edge WebView2 Runtime，本包不包含该运行时。
   便携版请保留完整的 _internal 目录，勿单独移动其中的文件。
2. 真实 Agent 实验需在工作台填写模型 API 地址、名称和密钥；密钥不会保存。配置后可进入批量测试。
   请求上限、可选费用保护额度和输入命中／未命中／输出三项单价，统一在工作台“调用预算与费用”设置；批量、对比沿用。
3. 点击软件窗口右上角关闭按钮，会停止本版后台服务并退出；有实验或下载运行时会提示，请先停止或等待完成。
4. 历史、日志、内嵌页面缓存和运行临时文件保存在 EXE 旁的“追因数据”文件夹。
   请解压到可写目录；移动程序时一起移动此文件夹，更新时保留它。
5. 输入文件通过软件内的文件选择框或拖入操作导入；下载结果时在软件弹出的保存框选择位置。
6. 软件通过 WebView2 在内部窗口显示本机工作台，不依赖系统默认浏览器设置。
7. 包内不附带原项目历史、API 密钥、研究台账或留待实测的新样本；原网页端数据不会自动迁入。
8. 需要独立保存位置时，可运行：追因.exe --data-dir "D:\\追因数据"。退出时使用相同参数并加 --stop。

当前仅支持内置设备借用／工单创建 Agent，不支持直接导入任意外部 Agent。
''', encoding='utf-8-sig')
        shutil.copyfile(product_docs[0], folder/'新功能使用说明.txt')
        shutil.copyfile(work/'desktop-build.json', folder/'构建清单.json')
        if wrapper:
            (folder/'构建清单.json').write_text(json.dumps({**manifest, 'launcher': wrapper}, ensure_ascii=False, indent=2), encoding='utf-8')
        shutil.copyfile(Path(sys.base_prefix)/'LICENSE.txt', folder/'Python许可.txt')
        notices = folder/'第三方许可'
        notices.mkdir(exist_ok=True)
        webview_notices = notices/'WebView2'
        webview_notices.mkdir(exist_ok=True)
        for name in ('LICENSE.txt', 'NOTICE.txt'):
            shutil.copyfile(SDK/name, webview_notices/name)
        archive = delivery/(f'追因-{label}-Windows-x64.zip')
        allowed = {'_internal', '追因.exe', '退出追因.cmd', '打开批量测试.cmd',
                   '使用说明.txt', '新功能使用说明.txt', '构建清单.json', 'Python许可.txt', '第三方许可'}
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
            for path in sorted(folder.rglob('*')):
                if path.is_file() and path.relative_to(folder).parts[0] in allowed:
                    output.write(path, '追因/'+path.relative_to(folder).as_posix())
    checksums = {path.name: {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in delivery.glob('*.zip')}
    (delivery/'下载校验.json').write_text(json.dumps(checksums, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'delivery': str(delivery), 'build_id': build_id, 'downloads': checksums}, ensure_ascii=False))


if __name__ == '__main__':
    main()
