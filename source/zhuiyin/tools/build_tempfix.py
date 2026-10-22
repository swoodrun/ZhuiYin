"""Wrap a sealed one-file core in a native bootstrap, avoiding system TEMP."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'packaging/portable_bootstrap.cpp'


def build_wrapper(payload, output, work):
    payload, output, work = Path(payload).resolve(), Path(output).resolve(), Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    shutil.copyfile(payload, work/'payload.bin')
    (work/'payload_config.h').write_text('#define PAYLOAD_SHA256 "'+digest+'"\n'
        '#define PAYLOAD_SHORT_W L"'+digest[:16]+'"\n', encoding='utf-8')
    (work/'bootstrap.manifest').write_text('''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
<requestedExecutionLevel level="asInvoker" uiAccess="false"/>
</requestedPrivileges></security></trustInfo>
<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
<longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
</windowsSettings></application></assembly>''', encoding='utf-8')
    (work/'payload.rc').write_text('101 RCDATA "'+(work/'payload.bin').as_posix()+'"\n'
        '1 24 "'+(work/'bootstrap.manifest').as_posix()+'"\n', encoding='utf-8')
    subprocess.run(['windres', str(work/'payload.rc'), '-O', 'coff', '-o', str(work/'payload.o')], check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['g++', '-std=c++17', '-Os', '-static', '-s', '-municode', '-mwindows',
        '-I', str(work), str(SOURCE), str(work/'payload.o'), '-ladvapi32', '-lshell32', '-o', str(work/'bootstrap.exe')], check=True)
    shutil.copyfile(work/'bootstrap.exe', output)
    return {'bootstrap_version': 'app-local-temp-v1', 'payload_sha256': digest,
            'bootstrap_source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            'exe_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    original = Path(sys.argv[1]).resolve()
    delivery = original.parent/(original.name+'-单文件启动修复-20261020')
    folder = delivery/'单文件修复版'
    folder.mkdir(parents=True, exist_ok=True)
    record = build_wrapper(original/'单文件版/追因.exe', folder/'追因.exe',
                           ROOT.parent/'.pkg'/'tempfix-native')
    for name in ('退出追因.cmd', '打开批量测试.cmd', 'Python许可.txt'):
        shutil.copyfile(original/'单文件版'/name, folder/name)
    core = json.loads((original/'单文件版/构建清单.json').read_text(encoding='utf-8'))
    (folder/'构建清单.json').write_text(json.dumps({**core, 'launcher': record}, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder/'使用说明.txt').write_text('''追因单文件启动修复版（Windows x64）

双击“追因.exe”即可启动，保留原网页样式，无需安装 Python。
本版不使用系统 TEMP/TMP 目录：首次运行会在 EXE 旁生成“追因运行缓存”。
实验历史仍在 EXE 旁的“追因数据”。请放在可写目录；移动程序时带上“追因数据”。
退出时双击“退出追因.cmd”。有实验运行时请先停止或等待完成。
“追因运行缓存”是运行文件，不是实验历史；退出后可以删除，下次会重新生成。
模型连接仍须用户自行配置，密钥不保存；程序不会自动调用收费模型。
旧版数据可复制到本版 EXE 旁的“追因数据”中，复制前先退出旧版。
''', encoding='utf-8-sig')
    archive = delivery/'追因-单文件启动修复版-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(folder.iterdir()):
            if path.is_file():
                bundle.write(path, '追因/'+path.name)
    result = {'delivery': str(delivery), 'archive': str(archive), 'launcher': record,
              'zip_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (delivery/'修复构建记录.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
