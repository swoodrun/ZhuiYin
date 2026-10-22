"""Package independently verified real observations without invoking a model."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.build_qualification_package import gather, add_file
from tools.build_frozen_judgment_package import close_links, readback
from tools.verify_evidence_qualification import verify

PREPARED = ROOT / "data/research/evidence-qualification-20261018-prepared"
COLLECTED = ROOT / "data/evaluations/evidence-qualification-real-20261018"
NAME = "追因-证据资格真实对照-20261018.zip"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build(output=None, acceptance=None):
    output = Path(output or ROOT / "data/evaluations" / NAME).resolve()
    acceptance = Path(acceptance or ROOT / "data/research/evidence-qualification-real-20261018-package-acceptance").resolve()
    if output.exists() or Path(str(output) + ".sha256").exists() or acceptance.exists():
        raise FileExistsError("real package/output must be new")
    audit = verify(PREPARED, COLLECTED)
    if not audit["passed"]:
        raise ValueError("unverified observations cannot enter a real-results package")
    report = json.loads((COLLECTED / "study-report.json").read_text(encoding="utf-8"))
    if report["status"] not in ("finished", "partial"):
        raise ValueError("running data cannot be sealed")
    files, scope = gather()
    for directory in (COLLECTED, ROOT / "data/research/evidence-qualification-real-20261018-verification",
                      ROOT / "data/research/evidence-qualification-real-20261018-review"):
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                add_file(files, path, ROOT)
    for name in ("证据资格真实对照实验报告.md", "tools/build_qualification_real_package.py"):
        add_file(files, ROOT / name, ROOT)
    files["核对后的真实结果.json"] = (json.dumps(audit, ensure_ascii=False, indent=2) + "\n").encode()
    extra, adjusted = close_links(files, ROOT.parent)
    files["文件清单.json"] = (json.dumps({name: {"sha256": sha(data), "bytes": len(data)}
        for name, data in sorted(files.items())}, ensure_ascii=False, indent=2) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    result = {**readback(output, ROOT.parent), **scope, "package": output.name, "sha256": sha(output.read_bytes()),
              "additional_link_dependencies": extra, "additional_adjusted_prose": adjusted,
              "packaging_model_requests": 0, "study_model_requests": audit["accounting"]["new_requests"],
              "accounting": audit["accounting"], "study_status": report["status"],
              "scope": "24 fixed frozen judgments; citations and assertions audited separately; no identity or network-source certification"}
    if not result["passed"]:
        raise ValueError("package readback or link verification failed")
    Path(str(output) + ".sha256").write_text(result["sha256"] + "  " + output.name + "\n", encoding="utf-8")
    acceptance.mkdir(parents=True, exist_ok=False)
    (acceptance / "package-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--acceptance")
    args = parser.parse_args()
    result = build(args.output, args.acceptance)
    print(json.dumps({k: result[k] for k in ("passed", "files", "checked_local_links", "sha256", "study_model_requests")}, ensure_ascii=False))
