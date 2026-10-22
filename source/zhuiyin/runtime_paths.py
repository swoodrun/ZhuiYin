"""Separate immutable application resources from persistent desktop user data."""
import hashlib
import json
import os
from pathlib import Path
import sys


def resource_root():
    return Path(__file__).resolve().parent


def data_root():
    override = os.environ.get('ZHUIYIN_DATA_DIR')
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError('数据目录必须使用绝对路径')
        return path
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / '追因数据'
    return resource_root() / 'data'


def desktop_identity():
    if not getattr(sys, 'frozen', False):
        return None
    build = json.loads((resource_root() / 'desktop-build.json').read_text(encoding='utf-8'))
    return {'build_id': build['build_id'],
            'data_id': hashlib.sha256(str(data_root().resolve()).encode('utf-8')).hexdigest()}
