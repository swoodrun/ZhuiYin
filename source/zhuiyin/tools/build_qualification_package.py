"""Build a NEW offline qualification preregistration package, only on explicit CLI use.

No collector, provider client, generator or archived source is executed. The
builder closes local document links, includes the recount's exact input files,
and checks the ZIP after writing. It cannot overwrite an old archive/output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_frozen_judgment_package import close_links, readback

NAME = "追因-证据资格对照离线预注册-20261018.zip"
INPUT_PREFIX = "evidence-qualification-20261018-"
FROZEN_PREFIXES = ("data/evaluations/eight-arm-real-20261018/",
                   "data/evaluations/paired-judgment-real-20261018/",
                   "data/research/paired-judgment-20261018-prepared/")
FILES = (
    "README.md", "当前交付口径.md", "新数据实测说明.md", "使用手册.md",
    "研究贡献与答辩口径.md", "答辩演示稿.md", "答辩问答.md", "现场演示走查清单.md",
    "tools/align_delivery_pages.py", "tools/package_fresh_field_inputs.py",
    "证据资格对照实验协议.md", "证据资格升级说明.md", "证据资格验收入口.md", "成本与收益对照.md", "故障与现实机制映射.md",
    "证据资格演示.html", "tools/build_qualification_demo.py", "tools/audit_evidence_boundary.py",
    "evidence_qualification.py", "qualification_scoring.py", "model_budget.py", "engine.py", "study.py",
    "job_store.py", "credentials.py", "paired_evidence.py", "paired_judgment_design.py",
    "tools/evidence_qualification_experiment.py", "tools/evidence_qualification_launcher.py",
    "tools/verify_evidence_qualification.py", "tools/evidence_boundary.py",
    "tools/paired_judgment_experiment.py", "tools/eight_arm_launcher.py", "tools/eight_arm_experiment.py",
    "tools/build_qualification_package.py", "tools/build_frozen_judgment_package.py",
    "tools/repair_eight_arm_package.py",
)
EXCLUDED = {"sources", ".git", ".codex", ".aws", "__pycache__"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def add_file(files, path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("source outside app")
    relative = path.relative_to(root)
    if any(part in EXCLUDED or part.startswith(".tmp") for part in relative.parts):
        raise ValueError("excluded package input")
    files["zhuiyin/" + relative.as_posix()] = path.read_bytes()


def gather(root=ROOT, include_final_log=True):
    root = Path(root).resolve()
    files = {}
    for relative in FILES:
        add_file(files, root / relative, root)
    for path in sorted(root.glob("test_*qualification*.py")):
        add_file(files, path, root)
    for path in sorted(root.glob("test_*boundary*.py")):
        add_file(files, path, root)
    directories = [p for p in (root / "data/research").glob(INPUT_PREFIX + "*") if p.is_dir()
                   and not p.name.endswith("-package-acceptance") and "prepared-draft-" not in p.name]
    required = {INPUT_PREFIX + suffix for suffix in ("fixture", "prepared", "boundary", "support")}
    if not required <= {p.name for p in directories}:
        raise ValueError("required preparation, fixture, boundary or support missing")
    for directory in sorted(directories):
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                # A dry input check may run while the root's suite is still
                # writing this log. A real build includes the completed log.
                if (not include_final_log and directory.name.endswith("-preservation")
                        and path.name in ("unittest-final.txt", "unittest-locked.txt")):
                    continue
                add_file(files, path, root)
    recount_manifest = json.loads((root / "data/research" / (INPUT_PREFIX + "support") / "source-file-hashes.json").read_text(encoding="utf-8"))
    for relative, record in recount_manifest["files"].items():
        path = (root / PurePosixPath(relative)).resolve()
        if not path.is_relative_to(root):
            raise ValueError("recount path escapes app")
        data = path.read_bytes()
        if digest(data) != record["sha256"] or len(data) != record["bytes"]:
            raise ValueError("recount source changed")
        add_file(files, path, root)
    files["CLAUDE.md"] = (root.parent / "CLAUDE.md").read_bytes()
    additions, adjusted = close_links(files, root.parent)
    # Local source files and frozen inputs are copied verbatim. Link conversion
    # can only apply to newly included review prose, reported separately.
    for name, data in files.items():
        if name.startswith(tuple("zhuiyin/" + p for p in FROZEN_PREFIXES)):
            if data != (root.parent / PurePosixPath(name)).read_bytes():
                raise ValueError("frozen material changed during packaging")
    return files, {"added_link_dependencies": additions, "adjusted_review_prose_copies": adjusted}


def build(root=ROOT, output=None, acceptance=None):
    root = Path(root).resolve()
    output = Path(output).resolve() if output else root / "data/evaluations" / NAME
    acceptance = Path(acceptance).resolve() if acceptance else root / "data/research" / (INPUT_PREFIX + "package-acceptance")
    if output.exists() or Path(str(output) + ".sha256").exists() or acceptance.exists():
        raise FileExistsError("new package and acceptance paths are required")
    files, scope = gather(root)
    files["文件清单.json"] = (json.dumps({name: {"sha256": digest(data), "bytes": len(data)}
                                         for name, data in sorted(files.items())},
                                        ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    result = {**readback(output, root.parent), **scope, "package": output.name,
              "sha256": digest(output.read_bytes()), "new_model_requests": 0,
              "real_qualification_results": "not_run", "scope": "Offline preparation and audit only; not model effect evidence"}
    if not result["passed"]:
        raise ValueError("new package readback or local links failed")
    Path(str(output) + ".sha256").write_text(result["sha256"] + "  " + output.name + "\n", encoding="utf-8")
    acceptance.mkdir(parents=True, exist_ok=False)
    (acceptance / "package-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--acceptance")
    parser.add_argument("--check-inputs-only", action="store_true", help="Close inputs/links without creating a ZIP")
    args = parser.parse_args()
    if args.check_inputs_only:
        files, scope = gather(include_final_log=False)
        print(json.dumps({"files": len(files), "new_model_requests": 0, **scope}, ensure_ascii=False))
    else:
        result = build(output=args.output, acceptance=args.acceptance)
        print(json.dumps({k: result[k] for k in ("passed", "files", "checked_local_links", "sha256", "new_model_requests")}, ensure_ascii=False))
