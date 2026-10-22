"""Record immutable research hashes and source baselines before product edits."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / 'data' / 'product-upgrade-20261021'


def main():
    AUDIT.mkdir(parents=True, exist_ok=True)
    target = AUDIT / 'before.json'
    if target.exists():
        raise SystemExit('Baseline already exists; refusing to replace it')
    sources = [*ROOT.glob('*.py'), *ROOT.joinpath('static').rglob('*'),
               ROOT / 'tools' / 'build_desktop.py']
    source_hashes = {}
    for path in sources:
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        source_hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        destination = AUDIT / 'source-before' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    research = {}
    for folder in ('evaluations', 'research'):
        for path in ROOT.joinpath('data', folder).rglob('*'):
            if path.is_file():
                research[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    target.write_text(json.dumps({'python': __import__('sys').executable,
        'sources': source_hashes, 'research': research}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'source_files': len(source_hashes), 'research_files': len(research)}))


if __name__ == '__main__':
    main()
