"""Distribute the additional contract audit without rewriting old archives."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import build_frozen_judgment_package as base

NAME = "追因-独立合同审计与三列演示-20261018.zip"
AUDIT = "data/research/contract-audit-20261018"
REVIEW = "data/research/review92-20261018"
EXTRA = ("研究贡献与答辩口径.md", "现场演示走查清单.md", "答辩演示稿.md", "答辩问答.md",
         "92分评审改进说明.md", "tools/audit_frozen_contract.py", "test_frozen_contract_audit.py",
         "tools/build_review92_package.py")


def build():
    workspace = ROOT.parent
    output = ROOT / "data/evaluations" / NAME
    acceptance = ROOT / "data/research/review92-20261018-package-acceptance"
    if output.exists() or acceptance.exists() or Path(str(output) + ".sha256").exists():
        raise FileExistsError("只允许新输出，不覆盖旧归档")
    before = base.snapshot(ROOT)
    baseline = json.loads((ROOT / REVIEW / "frozen-before.json").read_bytes())["files"]
    if any(base.sha((ROOT / name).read_bytes()) != value for name, value in baseline.items()):
        raise ValueError("本轮冻结输入发生变化")
    files = {"zhuiyin/" + name: (ROOT / name).read_bytes() for name in (*base.INCLUDE_FILES, *EXTRA)}
    for directory in (*base.INCLUDE_DIRS, AUDIT, REVIEW):
        for file in (ROOT / directory).rglob("*"):
            if file.is_file():
                relative = file.relative_to(ROOT).as_posix()
                if any(part in base.EXCLUDED or part.startswith(".tmp") for part in Path(relative).parts):
                    raise ValueError("不打包临时或排除目录")
                files["zhuiyin/" + relative] = file.read_bytes()
    files["包说明.md"] = ("# 追因：合同交叉审计与三列演示\n\n"
        "这是已有30次真实判断的零模型成本补充包，不重新采集样本。请完整解压到较短目录，"
        "例如C:\\z\\zy，减少Windows深层路径造成的解压失败。主要文件在zhuiyin/。\n\n"
        "先阅读[改进说明](zhuiyin/92分评审改进说明.md)、[贡献与答辩口径](zhuiyin/研究贡献与答辩口径.md)，"
        "打开[三列演示](zhuiyin/配对证据演示.html)。现场浏览器验证状态以"
        "[人工走查清单](zhuiyin/现场演示走查清单.md)和其中明确列出的实测记录为准；"
        "界面契约通过不能替代实屏验收。\n\n"
        "在Python 3.11或更新版本下进入zhuiyin/，各输出目录必须此前不存在：\n\n"
        "```text\ncd zhuiyin\n"
        "python tools/verify_paired_judgment.py --prepared data/research/paired-judgment-20261018-prepared --input data/evaluations/paired-judgment-real-20261018 --output my-material-check\n"
        "python tools/audit_frozen_contract.py --prepared data/research/paired-judgment-20261018-prepared --input data/evaluations/paired-judgment-real-20261018 --pair-source data/research/paired-evidence-20261018 --output my-contract-audit\n"
        "python tools/verify_paired_evidence.py --input data/research/paired-evidence-20261018 --output my-pair-check\n```\n\n"
        "第一项保留原592项材料核对；第二项是纯标准库的额外合同审计，不调用设计生成函数或项目裁判；"
        "第三项核对原配对材料86项。三者都不联网、不调用模型、不执行业务工具或归档源码。"
        "合同审计覆盖固定工单模板，不代替台账核对，不是两套完全独立的系统或外部认证。\n\n"
        "[真实报告](zhuiyin/真实模型冻结判断实验报告.md)保留10次有据正确、15次合理unknown、5次证据未利用。"
        "failure只表示指定冻结时点未完成；标签不证明模型没看证据或不理解回滚。"
        "没有筛选性重跑，也没有改变原30条评分。累计331次、0.922815元保守记账，剩余43次授权。"
        "不要从包内准备时点的301次台账另发请求；本包不是收费实验入口或完整工作台安装包。\n\n"
        "全部历史测试日志原样保留，新回归结果见改进说明。日志作为来源材料附带，不宣称包内能运行整项目全部测试。"
        "主材料的绝对ledger_path保留原字节，离线核对不使用它；来源真实性、模型身份、网络发送、"
        "外部账单及自主恢复均不由这些检查认证。链接闭合只证明本地目标存在。\n").encode("utf-8")
    files["分发核对.json"] = b"{}\n"
    additions, adjusted = base.close_links(files, workspace)
    links, sources = base.all_links(files, workspace)
    if not all(row["exists_in_package"] for row in links) or not all(row["passed"] for row in sources):
        raise ValueError("链接或来源哈希未闭合")
    distribution = {"passed": True, "new_model_requests": 0, "checked_local_links": len(links),
                    "links": links, "source_checks": sources, "added_dependencies": additions,
                    "adjusted_nonfrozen_review_document_copies": adjusted}
    files["分发核对.json"] = base.json_bytes(distribution)
    files["冻结输入保全.json"] = base.json_bytes({"files": before, "review_baseline_files": len(baseline)})
    files["文件清单.json"] = base.json_bytes({name: {"sha256": base.sha(data), "bytes": len(data)}
                                              for name, data in sorted(files.items())})
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    checked = base.readback(output, workspace)
    if not checked["passed"]:
        raise ValueError("新归档回读失败")
    acceptance.mkdir(parents=True)
    short = workspace / ".r92"
    short.mkdir(exist_ok=True)
    moved = []
    with tempfile.TemporaryDirectory(prefix="p-", dir=short) as temporary:
        target = Path(temporary)
        with zipfile.ZipFile(output) as archive:
            archive.extractall(target)
        for tool, arguments, name in (
            ("verify_paired_judgment.py", ["--prepared", base.PREPARED, "--input", base.COLLECTED], "material"),
            ("audit_frozen_contract.py", ["--prepared", base.PREPARED, "--input", base.COLLECTED,
                                            "--pair-source", base.FROZEN[2]], "contract"),
            ("verify_paired_evidence.py", ["--input", base.FROZEN[2]], "pair")):
            completed = subprocess.run([sys.executable, "-X", "utf8", "tools/" + tool, *arguments, "--output", "moved-" + name],
                                       cwd=target / "zhuiyin", capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=90)
            try:
                parsed = json.loads(completed.stdout)
                count = parsed.get("check_count", parsed.get("checks"))
                passed = (completed.returncode == 0 and parsed.get("passed") is True
                          and count == {"material": 592, "contract": 424, "pair": 86}[name])
            except (ValueError, TypeError, AttributeError):
                passed, count = False, None
            result = {"name": name, "passed": passed, "check_count": count, "return_code": completed.returncode,
                      "stdout": completed.stdout, "stderr": completed.stderr}
            moved.append(result)
        if not all(item["passed"] for item in moved):
            (acceptance / "moved-failed.json").write_bytes(base.json_bytes(moved))
            raise ValueError("搬移核对未通过")
    after = base.snapshot(ROOT)
    if after != before or any(base.sha((ROOT / name).read_bytes()) != value for name, value in baseline.items()):
        raise ValueError("冻结材料发生变化")
    final = {**checked, "archive_sha256": base.sha(output.read_bytes()), "moved_checks": moved,
             "historical_baseline_unchanged": True, "historical_baseline_files": len(baseline),
             "frozen_inputs_unchanged": True, "frozen_input_files": len(before), "new_model_requests": 0}
    (acceptance / "package-checks.json").write_bytes(base.json_bytes(final))
    (acceptance / "package-links.json").write_bytes(base.json_bytes(distribution))
    Path(str(output) + ".sha256").write_text(final["archive_sha256"] + "  " + output.name + "\n", encoding="utf-8")
    return final


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    result = base.readback(args.check, ROOT.parent) if args.check else build()
    print(json.dumps({key: value for key, value in result.items() if key not in ("links", "paired_demo_source_checks")}, ensure_ascii=True))
