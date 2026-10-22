"""Verify delivered PE icon resources against an approved ICO without running EXEs.

Run with the build environment that provides pefile, for example:
  python tools/audit_desktop_icon_resources.py data/deliveries/<build-folder>
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

import pefile

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def image_record(width, height, planes, bits, size, payload):
    require(len(payload) == size, 'Truncated icon image payload')
    return {'width': width or 256, 'height': height or 256, 'planes': planes,
            'bits': bits, 'bytes': size, 'sha256': digest(payload), 'payload': payload}


def ico_images(data):
    reserved, kind, count = struct.unpack_from('<HHH', data)
    require((reserved, kind) == (0, 1) and count > 0, 'Not a nonempty ICO file')
    images = []
    for index in range(count):
        width, height, colors, zero, planes, bits, size, offset = struct.unpack_from('<BBBBHHII', data, 6+index*16)
        images.append(image_record(width, height, planes, bits, size, data[offset:offset+size]))
    return images


def audit(path, delivery, expected):
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
        resources = {}
        for resource_type in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if resource_type.id not in (3, 14):
                continue
            for name in resource_type.directory.entries:
                require(name.id is not None, 'Unexpected named icon resource')
                for language in name.directory.entries:
                    record = language.data.struct
                    resources[(resource_type.id, name.id, language.id)] = pe.get_data(record.OffsetToData, record.Size)
        groups = []
        for (kind, name, language), group in sorted(resources.items()):
            if kind != 14:
                continue
            reserved, icon_type, count = struct.unpack_from('<HHH', group)
            require((reserved, icon_type) == (0, 1), 'Invalid RT_GROUP_ICON header')
            images = []
            for index in range(count):
                width, height, colors, zero, planes, bits, size, icon_id = struct.unpack_from('<BBBBHHIH', group, 6+index*14)
                candidates = [(lang, payload) for (resource_kind, resource_id, lang), payload in resources.items()
                              if resource_kind == 3 and resource_id == icon_id]
                require(bool(candidates), 'Missing RT_ICON '+str(icon_id))
                payload = next((data for lang, data in candidates if lang == language), candidates[0][1])
                images.append(image_record(width, height, planes, bits, size, payload))
            groups.append({'group_id': name, 'language': language, 'image_count': count,
                           'matches_approved_ico_bytes': images == expected,
                           'sizes': [[image['width'], image['height']] for image in images],
                           'image_payload_sha256': [image['sha256'] for image in images]})
        require(bool(groups) and all(group['matches_approved_ico_bytes'] for group in groups),
                'PE icon images differ from approved ICO: '+str(path))
        return {'path': path.relative_to(delivery).as_posix(), 'exe_sha256': digest(path.read_bytes()),
                'passed': True, 'groups': groups}
    finally:
        pe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('delivery', type=Path)
    parser.add_argument('--ico', type=Path, default=ROOT/'packaging/assets/zhuiyin.ico')
    parser.add_argument('--expect-executables', type=int, default=2)
    args = parser.parse_args()
    delivery, source = args.delivery.resolve(), args.ico.resolve()
    source_bytes = source.read_bytes()
    expected = ico_images(source_bytes)
    executables = sorted(delivery.rglob('*.exe'))
    require(len(executables) == args.expect_executables,
            'Unexpected executable count: '+str(len(executables)))
    report = {'build_id': delivery.name.removeprefix('追因-Windows-'),
              'method': 'pefile RT_GROUP_ICON and RT_ICON payload byte comparison; executables were not launched',
              'python': sys.version, 'pefile': pefile.__version__, 'approved_ico': str(source),
              'approved_ico_sha256': digest(source_bytes), 'approved_image_count': len(expected),
              'audit_script': str(Path(__file__).resolve()),
              'audit_script_sha256': digest(Path(__file__).read_bytes()),
              'executables': [audit(path, delivery, expected) for path in executables], 'passed': True}
    target = delivery/'PE图标资源独立核验.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'evidence': str(target),
                      'executables': [result['path'] for result in report['executables']],
                      'matched_image_count_each': len(expected)}))


if __name__ == '__main__':
    main()
