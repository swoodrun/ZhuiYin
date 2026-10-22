"""Compile a small .NET Framework WebView2 host using Microsoft's official SDK."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SDK_VERSION = '1.0.4022.49'
SDK = ROOT.parent/'.pkg'/('webview2-sdk-'+SDK_VERSION)/'sdk'
ICON = ROOT/'packaging'/'assets'/'zhuiyin.ico'


def build_host(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='webview-stage-', dir=output.parent) as stage:
        _compile_host(Path(stage))
        for file in Path(stage).iterdir():
            shutil.copyfile(file, output/file.name)
    return output


def _compile_host(output):
    source = ROOT/'packaging'/'WebViewHost.cs'
    compiler = Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')
    libraries = SDK/'lib'/'net462'
    arguments = [str(compiler), '/nologo', '/optimize+', '/platform:x64', '/target:winexe',
                 '/out:'+str(output/'追因窗口.exe'), '/win32manifest:'+str(ROOT/'packaging'/'WebViewHost.manifest'),
                 '/win32icon:'+str(ICON),
                 '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                 '/reference:System.Web.Extensions.dll']
    for name in ('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll'):
        arguments.append('/reference:'+str(libraries/name))
        shutil.copyfile(libraries/name, output/name)
    shutil.copyfile(SDK/'runtimes'/'win-x64'/'native'/'WebView2Loader.dll', output/'WebView2Loader.dll')
    shutil.copyfile(ICON, output/'zhuiyin.ico')
    subprocess.run(arguments+[str(source)], check=True)


if __name__ == '__main__':
    build_host(ROOT.parent/'.pkg'/'webview-host-test')
