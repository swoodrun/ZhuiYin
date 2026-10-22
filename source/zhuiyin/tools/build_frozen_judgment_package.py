"""Create a NEW, offline-only distribution of the completed frozen probes.

Frozen inputs and collected provider material are copied byte for byte. This
builder never imports a collector, starts a service, or contacts a provider.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.repair_eight_arm_package import inspect_links, portable_copy, resolve_local

NAME = "追因-冻结判断真实模型证据-20261018.zip"
PREPARED = "data/research/paired-judgment-20261018-prepared"
COLLECTED = "data/evaluations/paired-judgment-real-20261018"
FROZEN = (PREPARED, COLLECTED, "data/research/paired-evidence-20261018")
INCLUDE_DIRS = (*FROZEN, "data/evaluations/paired-judgment-real-20261018-verification",
                "data/research/paired-judgment-20261018-results",
                "data/research/paired-judgment-20261018-readiness",
                "data/research/paired-evidence-20261018-final-verification",
                "data/research/paired-evidence-20261018-acceptance")
INCLUDE_FILES = ("真实模型冻结判断实验报告.md", "冻结状态判断真实实验说明.md",
                 "配对证据实验协议.md", "配对证据实验升级说明.md", "配对证据包说明.md",
                 "配对证据演示.html", "打开配对证据演示.cmd", "paired_judgment_design.py",
                 "tools/verify_paired_evidence.py", "tools/verify_paired_judgment.py",
                 "tools/summarize_paired_judgment.py", "tools/run_paired_evidence.py",
                 "paired_evidence.py", "engine.py", "model_budget.py",
                 "tools/build_paired_demo.py", "tools/check_paired_demo.js",
                 "tools/repair_eight_arm_package.py", "tools/build_frozen_judgment_package.py")
EXCLUDED = {"sources", ".git", ".codex", ".aws", "__pycache__"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(root):
    return {p.relative_to(root).as_posix(): sha(p.read_bytes())
            for name in FROZEN for p in (root / name).rglob("*") if p.is_file()}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class PairedData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active, self.bits = False, []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "paired-data":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, value):
        if self.active:
            self.bits.append(value)


def all_links(files, workspace):
    links = inspect_links(files, workspace)
    source_checks = []
    for name, data in sorted(files.items()):
        if not name.endswith(".html"):
            continue
        parser = PairedData()
        parser.feed(data.decode("utf-8-sig"))
        if not parser.bits:
            continue
        evidence = json.loads("".join(parser.bits))
        dynamic = [("paired_report", evidence["source"])]
        source = resolve_local(name, evidence["source"], workspace)
        source_checks.append({"origin": name, "target": source,
                              "passed": source in files and sha(files[source]) == evidence["source_sha256"]})
        study = evidence.get("real_study") or {}
        if study.get("report"):
            dynamic += [("prepared_directory", study["prepared"]), ("real_study_directory", study["source"])]
            report = study["source"] + "/study-report.json"
            source = resolve_local(name, report, workspace)
            dynamic.append(("real_study_report", report))
            source_checks.append({"origin": name, "target": source,
                                  "passed": source in files and sha(files[source]) == study["source_sha256"]})
            for sample in study["report"]["samples"]:
                base = study["source"] + "/samples/" + str(sample["sample_index"]).zfill(2) + "/"
                for leaf in ("request.json", "response.json", "sample.json"):
                    dynamic.append(("actual_sample_link", base + leaf))
        for kind, target in dynamic:
            resolved = resolve_local(name, target, workspace)
            exists = resolved in files or any(x.startswith(resolved.rstrip("/") + "/") for x in files)
            links.append({"origin": name, "kind": kind, "link": target,
                          "resolved": resolved, "exists_in_package": exists})
    return links, source_checks


def close_links(files, workspace):
    additions, adjusted_review_copies = [], []
    while True:
        links, _ = all_links(files, workspace)
        missing = sorted({x["resolved"] for x in links if not x["exists_in_package"]})
        if not missing:
            return additions, adjusted_review_copies
        for name in missing:
            source = workspace / PurePosixPath(name)
            if not source.exists() or not source.resolve().is_relative_to(workspace):
                raise ValueError("missing or escaped dependency: " + name)
            paths = list(source.rglob("*")) if source.is_dir() else [source]
            for child in paths:
                if not child.is_file():
                    continue
                relative = child.relative_to(workspace).as_posix()
                if any(p in EXCLUDED or p.startswith(".tmp") for p in PurePosixPath(relative).parts):
                    raise ValueError("excluded dependency: " + relative)
                if relative in files:
                    continue
                data = child.read_bytes()
                # Only newly added supplementary review prose can have its
                # drive links made portable. Frozen directories are untouched.
                if relative.startswith("review_artifacts/") and relative.endswith(".md"):
                    copied = portable_copy(relative, data, workspace)
                    if copied != data:
                        adjusted_review_copies.append(relative)
                    data = copied
                files[relative] = data
                additions.append(relative)


def readback(path, workspace):
    with zipfile.ZipFile(path) as archive:
        crc_ok = archive.testzip() is None
        files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    manifest = json.loads(files["文件清单.json"])
    exact = set(files) == set(manifest) | {"文件清单.json"}
    hashes = all(name in files and sha(files[name]) == item["sha256"] and len(files[name]) == item["bytes"]
                 for name, item in manifest.items())
    links, sources = all_links(files, workspace)
    return {"passed": crc_ok and exact and hashes and all(x["exists_in_package"] for x in links)
            and all(x["passed"] for x in sources), "crc_passed": crc_ok,
            "manifest_exact_members": exact, "manifest_hashes_match": hashes,
            "files": len(files), "checked_local_links": len(links),
            "missing_local_links": [x for x in links if not x["exists_in_package"]],
            "paired_demo_source_checks": sources, "new_model_requests": 0}


def build(root=ROOT, output=None, acceptance=None):
    root = Path(root).resolve()
    workspace = root.parent
    output = Path(output) if output else root / "data/evaluations" / NAME
    acceptance = Path(acceptance) if acceptance else root / "data/research/paired-judgment-20261018-package-acceptance"
    if output.exists() or Path(str(output) + ".sha256").exists() or acceptance.exists():
        raise FileExistsError("only NEW outputs are permitted")
    before = snapshot(root)
    files = {}
    for relative in INCLUDE_FILES:
        files["zhuiyin/" + relative] = (root / relative).read_bytes()
    for directory in INCLUDE_DIRS:
        for path in (root / directory).rglob("*"):
            if path.is_file():
                if any(p in EXCLUDED or p.startswith(".tmp") for p in path.relative_to(root).parts):
                    raise ValueError("temporary material in selected input")
                files["zhuiyin/" + path.relative_to(root).as_posix()] = path.read_bytes()
    additions, adjusted = close_links(files, workspace)
    files["包说明.md"] = ("# 追因：冻结判断真实模型证据包\n\n"
        "本包用于离线查看与复核已经完成的30次真实判断。请完整解压；"
        "主要文件位于zhuiyin/，相邻review_artifacts/仅是文档引用依赖。"
        "打开[配对证据演示](zhuiyin/配对证据演示.html)或[真实模型报告](zhuiyin/真实模型冻结判断实验报告.md)。\n\n"
        "先进入zhuiyin目录，在Python 3.11或更新版本下执行以下命令；输出必须是此前不存在的新目录。"
        "这些命令不联网、不调用模型、不执行保存的业务动作或源码快照。\n\n"
        "```text\ncd zhuiyin\npython tools/verify_paired_judgment.py --prepared data/research/paired-judgment-20261018-prepared --input data/evaluations/paired-judgment-real-20261018 --output my-frozen-verification\n"
        "python tools/verify_paired_evidence.py --input data/research/paired-evidence-20261018 --output my-original-pair-verification\n```\n\n"
        "预期分别592项和86项通过。独立复核只检查保存材料内部一致性，不认证真实发送、"
        "服务商或模型身份、来源真实性、外部账单或操作系统级执行审计。"
        "全项目535项测试的日志作为来源材料附带；本包不宣称可运行全部535项测试，"
        "也不是完整工作台安装包。附带构建工具和源码快照仅供流程检查。\n\n"
        "截至本轮累计331次、0.922815元保守记账，剩余43次授权，上限374次及10元。"
        "本包不授权重跑收费实验；prepare中的not_run与301次起点属于调用前历史记录，"
        "绝不能据其另开请求。campaign-policy.json中的绝对ledger_path按原字节保留，"
        "独立核验器不使用这个路径；复核只读取包内指定台账。不要运行任何采集或凭据入口。\n\n"
        "原pair、prepared、collected目录保持原字节。为关闭补充评审说明的盘符链接，"
        "仅对新增非冻结评审文档的包内副本作相对化，详见[分发核对](分发核对.json)；"
        "原项目文件均未修改。链接核对证明本地目标存在，不替代浏览器实屏验收或外网可用性。\n"
        ).encode("utf-8")
    # The README links to the distribution record generated immediately below.
    files["分发核对.json"] = b"{}\n"
    links, sources = all_links(files, workspace)
    if not all(x["exists_in_package"] for x in links) or not all(x["passed"] for x in sources):
        raise ValueError("link/source closure failed")
    if snapshot(root) != before:
        raise ValueError("frozen sources changed during build")
    distribution = {"passed": True, "new_model_requests": 0, "checked_local_links": len(links),
                    "missing_local_links": 0, "links": links, "paired_demo_source_checks": sources,
                    "recursive_dependencies_added": additions, "adjusted_review_document_copies": adjusted,
                    "frozen_files": len(before), "frozen_inputs_byte_identical": True}
    files["分发核对.json"] = json_bytes(distribution)
    files["冻结输入保全.json"] = json_bytes({"passed": True, "before": before, "after": snapshot(root)})
    files["文件清单.json"] = json_bytes({name: {"sha256": sha(data), "bytes": len(data)}
                                        for name, data in sorted(files.items())})
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    result = readback(output, workspace)
    if not result["passed"]:
        raise ValueError("archive readback failed")
    acceptance.mkdir(parents=True)
    # Executing only the packaged independent verifier in a moved temporary
    # tree proves its documented paths do not rely on the original drive.
    # A short workspace-relative base also works on Windows installations
    # where legacy MAX_PATH remains enabled for nested historical files.
    temporary_base = workspace / ".pkg"
    temporary_base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="p-", dir=temporary_base) as temp:
        target = Path(temp)
        with zipfile.ZipFile(output) as archive:
            archive.extractall(target)
        app = target / "zhuiyin"
        checks = []
        for argv, name, expected in (
            (["tools/verify_paired_judgment.py", "--prepared", PREPARED, "--input", COLLECTED,
              "--output", "moved-frozen-verification"], "moved-frozen-verification", 592),
            (["tools/verify_paired_evidence.py", "--input", FROZEN[2], "--output", "moved-pair-verification"],
             "moved-pair-verification", 86)):
            completed = subprocess.run([sys.executable, *argv], cwd=app, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=90)
            report = json.loads((app / name / "verification.json").read_text(encoding="utf-8"))
            item = {"return_code": completed.returncode, "stdout": completed.stdout,
                    "stderr": completed.stderr, "passed": completed.returncode == 0 and report["passed"]
                    and report["check_count"] == expected, "check_count": report["check_count"]}
            checks.append(item)
            (acceptance / (name + ".json")).write_bytes(json_bytes(report))
        if not all(x["passed"] for x in checks):
            raise ValueError("moved independent verifier failed")
    if snapshot(root) != before:
        raise ValueError("frozen sources changed during moved verification")
    result.update({"archive_sha256": sha(output.read_bytes()), "archive_bytes": output.stat().st_size,
                   "moved_verifier_checks": checks, "frozen_inputs_unchanged": True,
                   "frozen_input_file_count": len(before)})
    (acceptance / "package-checks.json").write_bytes(json_bytes(result))
    (acceptance / "package-links.json").write_bytes(json_bytes(distribution))
    (acceptance / "preservation.json").write_bytes(files["冻结输入保全.json"])
    Path(str(output) + ".sha256").write_text(result["archive_sha256"] + "  " + output.name + "\n", encoding="utf-8")
    (acceptance / "验收记录.md").write_text(
        "# 冻结判断真实证据包离线分发验收\n\n"
        f"新增包{len(files)}个文件；CRC、逐成员SHA-256和文件集合均通过。"
        f"递归核对{len(links)}个静态或内嵌动态本地链接，断链0，配对演示来源哈希均通过。\n\n"
        "完整解压到隔离临时目录，使用包内独立工具重新核对：真实判断592项、"
        f"原始离线配对86项通过。原pair、prepared、collected共{len(before)}个文件前后哈希相同。"
        "本次打包和搬移复核新增模型请求0；没有网络或浏览器实屏验收。\n",
        encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    result = readback(args.check, ROOT.parent) if args.check else build()
    print(json.dumps(result, ensure_ascii=False))
