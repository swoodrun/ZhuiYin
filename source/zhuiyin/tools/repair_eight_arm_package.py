"""Repair distribution links in a NEW archive; never alter historical evidence.

Only standard-library, local file reads/copies are used. No model or server imports.
The outer archive mirrors the project root, with app files under ``zhuiyin/`` so
links to sibling review_artifacts can remain byte-for-byte unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
OLD_NAME = "追因-真实八组对照证据-20261018.zip"
NEW_NAME = "追因-真实八组对照证据-20261018-链接修正版.zip"
REVIEW = "review_artifacts/2026-10-18/eight-arm/package-links.json"
ACCEPTANCE = "review_artifacts/2026-10-18/eight-arm-link-repair"
FIXED_EVIDENCE = "data/evaluations/eight-arm-real-20261018"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(root):
    old = root / "data/evaluations" / OLD_NAME
    return {str(path.relative_to(root)).replace("\\", "/"): digest(path.read_bytes())
            for path in [old, *(p for p in (root / FIXED_EVIDENCE).rglob("*") if p.is_file())]}


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.evidence = False
        self.payload = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ("href", "src", "poster", "action"):
            if attrs.get(key):
                self.links.append(("html:" + key, attrs[key]))
        if tag == "script" and attrs.get("id") == "evidence" and attrs.get("type") == "application/json":
            self.evidence = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.evidence = False

    def handle_data(self, data):
        if self.evidence:
            self.payload.append(data)


def local_links(name, data):
    """Extract rendered static links plus the demo's schema-backed dynamic links.

    Template placeholders are not real destinations; the evidence JSON is the
    authoritative input to the source list and real-study report link renderer.
    Markdown code fences are examples and do not produce clickable links.
    """
    value = data.decode("utf-8-sig")
    links = []
    if name.lower().endswith((".html", ".htm")):
        page = PageLinks()
        page.feed(value)
        links.extend(page.links)
        if page.payload:
            evidence = json.loads("".join(page.payload))
            for source in evidence.get("sources", []):
                links.append(("embedded_source_link", source["path"]))
            if (evidence.get("real_study") or {}).get("source"):
                links.append(("real_study_report", evidence["real_study"]["source"]))
    else:
        value = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", value)
        # Project documents use inline destinations and optional angle brackets.
        for match in re.finditer(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+['\"][^)]*['\"])?\s*\)", value):
            links.append(("markdown", match.group(1).strip("<>")))
        refs = {m.group(1).casefold(): m.group(2).strip("<>") for m in
                re.finditer(r"(?m)^\s*\[([^\]]+)\]:\s*(<[^>]+>|\S+)", value)}
        for match in re.finditer(r"!?\[([^\]]+)\]\[([^\]]*)\]", value):
            label = (match.group(2) or match.group(1)).casefold()
            if label in refs:
                links.append(("markdown_reference", refs[label]))
        page = PageLinks()
        page.feed(value)
        links.extend(page.links)
    return [(origin, target) for origin, target in links
            if "${" not in target and not target.startswith(("'+", '"+'))]


def resolve_local(origin, target, workspace):
    """Return archive-relative target; external URLs/fragments return None.

    Absolute links are rejected: even an existing original-drive path is not a
    portable package link. Relative destinations cannot escape the outer bundle.
    """
    target = unquote(unescape(target)).replace("\\", "/")
    if not target or target.startswith("#"):
        return None
    parts = urlsplit(target)
    if parts.scheme and parts.scheme.lower() != "file" and not re.match(r"^[A-Za-z]:/", target):
        return None
    if parts.scheme.lower() == "file" or target.startswith("/") or re.match(r"^[A-Za-z]:/", target):
        raise ValueError("nonportable absolute local link: " + target)
    clean = re.sub(r":\d+(?::\d+)?$", "", parts.path)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(origin), clean))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("local link escapes bundle: " + target)
    if any(part in {"sources", ".git", ".codex", ".aws"} for part in PurePosixPath(resolved).parts):
        raise ValueError("excluded directory dependency: " + resolved)
    return resolved


def inspect_links(files, workspace=WORKSPACE):
    results = []
    for origin, data in sorted(files.items()):
        if not origin.lower().endswith((".md", ".html", ".htm")):
            continue
        for kind, target in local_links(origin, data):
            resolved = resolve_local(origin, target, workspace)
            if resolved is None:
                continue
            exists = resolved in files or any(p.startswith(resolved.rstrip("/") + "/") for p in files)
            results.append({"origin": origin, "kind": kind, "link": target,
                            "resolved": resolved, "exists_in_package": exists})
    return results


def source_hash_checks(files):
    checks = []
    for name, data in sorted(files.items()):
        if not name.lower().endswith((".html", ".htm")):
            continue
        page = PageLinks()
        page.feed(data.decode("utf-8-sig"))
        if not page.payload:
            continue
        evidence = json.loads("".join(page.payload))
        for source in evidence.get("sources", []):
            if "sha256" not in source:
                continue
            target = resolve_local(name, source["path"], WORKSPACE)
            checks.append({"origin": name, "target": target,
                           "passed": target in files and digest(files[target]) == source["sha256"]})
    return checks


def portable_copy(name, data, workspace):
    """Make supplementary review-document drive links portable in this copy only."""
    if not name.lower().endswith((".md", ".html", ".htm")):
        return data
    value = data.decode("utf-8-sig")
    prefix = workspace.as_posix().rstrip("/") + "/"
    links = local_links(name, data)
    for _, target in links:
        decoded = unquote(target).replace("\\", "/")
        if decoded.casefold().startswith(prefix.casefold()):
            relative = re.sub(r":\d+(?::\d+)?$", "", decoded[len(prefix):])
            portable = posixpath.relpath(relative, posixpath.dirname(name))
            value = value.replace(target, portable)
    return value.encode("utf-8")


def close_dependencies(files, workspace):
    """Include all transitively linked local documents/assets; never rewrite them."""
    added = []
    while True:
        missing = sorted({x["resolved"] for x in inspect_links(files, workspace) if not x["exists_in_package"]})
        if not missing:
            return added
        progress = False
        unavailable = []
        for name in missing:
            source = workspace / PurePosixPath(name)
            if source.is_dir():
                children = [p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
                if not children:
                    raise ValueError("empty linked directory: " + name)
                for child in children:
                    child_name = child.relative_to(workspace).as_posix()
                    if child_name not in files:
                        files[child_name] = portable_copy(child_name, child.read_bytes(), workspace)
                        added.append(child_name)
                        progress = True
                continue
            if not source.is_file():
                unavailable.append(name)
                continue
            if source.resolve().is_relative_to(workspace.resolve()) is False:
                raise ValueError("dependency escapes workspace: " + name)
            files[name] = portable_copy(name, source.read_bytes(), workspace)
            added.append(name)
            progress = True
        if not progress:
            raise ValueError("missing local dependency: " + "; ".join(unavailable))


def repair(root=ROOT, output=None, acceptance=None):
    root = Path(root).resolve()
    workspace = root.parent
    output = Path(output) if output else root / "data/evaluations" / NEW_NAME
    acceptance = Path(acceptance) if acceptance else workspace / ACCEPTANCE
    if output.exists() or acceptance.exists():
        raise FileExistsError("Repair writes only NEW output paths")
    before = snapshot(root)
    old_path = root / "data/evaluations" / OLD_NAME
    with zipfile.ZipFile(old_path) as old:
        if old.testzip() is not None:
            raise ValueError("historical archive CRC failed")
        files = {"zhuiyin/" + name: old.read(name) for name in old.namelist() if not name.endswith("/")}
    original = dict(files)
    findings = json.loads((workspace / REVIEW).read_text(encoding="utf-8"))
    original_missing = [item for item in findings["links"] if not item["exists_in_package"]]
    for finding in original_missing:
        name = "zhuiyin/" + finding["path"]
        source = root / finding["path"]
        if name in files:
            raise ValueError("reviewed missing path unexpectedly present")
        files[name] = portable_copy(name, source.read_bytes(), workspace)
    dependencies = close_dependencies(files, workspace)
    note_name = "链接修正版说明.md"
    note = ("# 八组证据包链接修正版\n\n"
            "本包仅修复分发缺少依赖文件造成的断链，不改写实验或历史证据，不新增模型请求。\n\n"
            "原始 ZIP 保持原字节；包内 zhuiyin/ 下原有文件也逐字节保留。"
            "为保留原文中的相邻目录链接，本包以项目目录为外层，应用文件位于 zhuiyin/，"
            "引用的评审材料位于 review_artifacts/。请先完整解压，再打开 [答辩演示](zhuiyin/答辩演示.html)。\n\n"
            "新补入的评审说明中原为本机盘符的链接仅在本包副本改为相对文件链接，移除行号后缀；"
            "原始项目文件、原包成员及历史证据不改。\n\n"
            "补入九个已报告缺失目标，并递归补齐其 Markdown/HTML 本地依赖；"
            "演示 sources 动态列表及真实研究报告入口也独立解析检查。"
            "只证明本包本地文件目标存在，不证明外网可用、浏览器渲染或实验来源真实性。\n\n"
            "可运行 `python zhuiyin/tools/repair_eight_arm_package.py check --archive 本包路径` 复查。\n")
    files[note_name] = note.encode("utf-8")
    files["zhuiyin/tools/repair_eight_arm_package.py"] = Path(__file__).read_bytes()
    links = inspect_links(files, workspace)
    if any(not item["exists_in_package"] for item in links):
        raise ValueError("not all local links resolved")
    source_checks = source_hash_checks(files)
    if not all(item["passed"] for item in source_checks):
        raise ValueError("embedded demo source hash differs from packaged dependency")
    if any(files[name] != data for name, data in original.items()):
        raise ValueError("an original archive member was modified")
    after = snapshot(root)
    if before != after:
        raise ValueError("historical inputs changed during repair")
    preservation = {"passed": True, "old_archive_sha256": digest(old_path.read_bytes()),
                    "frozen_input_file_count": len(before), "before": before, "after": after,
                    "original_archive_members_preserved": len(original), "new_model_requests": 0}
    report = {"passed": True, "new_model_requests": 0, "old_archive": old_path.name,
              "new_archive": output.name, "original_missing_count": len(original_missing),
              "original_missing_fixed": [{"path": x["path"], "present": "zhuiyin/" + x["path"] in files}
                                         for x in original_missing],
              "recursive_added_count": len(dependencies), "recursive_added": dependencies,
              "checked_local_links": len(links), "missing_local_links": 0, "links": links,
              "embedded_source_hash_checks": source_checks,
              "original_archive_members_preserved": len(original), "frozen_inputs_unchanged": True,
              "scope": "Local destinations including rendered evidence-source links; no external URL or browser rendering certification"}
    files["分发链接核对.json"] = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    files["历史保全核对.json"] = json.dumps(preservation, ensure_ascii=False, indent=2).encode("utf-8")
    manifest = {name: {"sha256": digest(data), "bytes": len(data)} for name, data in sorted(files.items())}
    files["链接修正版文件清单.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    acceptance.mkdir(parents=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    validation = check_archive(output)
    if not validation["passed"]:
        raise ValueError("new archive failed independent read-back")
    report["archive_readback"] = validation
    report["archive_sha256"] = digest(output.read_bytes())
    for name, value in (("package-links.json", report), ("preservation.json", preservation)):
        (acceptance / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (Path(str(output) + ".sha256")).write_text(report["archive_sha256"] + "  " + output.name + "\n", encoding="utf-8")
    (acceptance / "验收记录.md").write_text(
        f"# 八组旧包链接分发修正验收\n\n原九处缺失全部补齐。递归核对 {len(links)} 个本地链接，断链 0。"
        f"补入九目标后继续补齐 {len(dependencies)} 个依赖，原 {len(original)} 个归档成员保持原字节。"
        f"冻结输入 {len(before)} 个文件哈希前后一致；模型请求为 0。\n\n"
        "动态生成的 sources 链接按内嵌 JSON 实际路径检查，避免只扫描 HTML 静态 href。"
        "本次没有浏览器实屏验收；本地链接闭合不证明外部网页可用或模型效果。\n",
        encoding="utf-8")
    return report


def check_archive(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            return {"passed": False, "reason": "crc"}
        files = {n: archive.read(n) for n in archive.namelist() if not n.endswith("/")}
    manifest = json.loads(files["链接修正版文件清单.json"])
    hashes = all(n in files and digest(files[n]) == item["sha256"] and len(files[n]) == item["bytes"]
                 for n, item in manifest.items())
    exact = set(files) == set(manifest) | {"链接修正版文件清单.json"}
    links = inspect_links(files)
    missing = [item for item in links if not item["exists_in_package"]]
    source_checks = source_hash_checks(files)
    return {"passed": hashes and exact and not missing and all(x["passed"] for x in source_checks), "manifest_hashes_match": hashes,
            "manifest_exact_members": exact, "files": len(files),
            "checked_local_links": len(links), "missing_local_links": missing,
            "embedded_source_hash_checks": source_checks,
            "new_model_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("repair")
    check = sub.add_parser("check")
    check.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    result = repair() if args.command == "repair" else check_archive(args.archive)
    print(json.dumps({key: value for key, value in result.items() if key not in {"links", "recursive_added"}}, ensure_ascii=False))
