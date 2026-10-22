"""Build a derived observation report only after independent verification."""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_paired_judgment import verify

NAMES = {"ambiguous": "原有模糊信息", "ordinary_reread": "普通再查询", "reliable_verification": "可靠核验"}
WORLD = {"committed": "A 已提交", "not_committed": "B 未提交"}


def build(prepared, source, output, markdown):
    prepared, source, output, markdown = map(lambda p: Path(p).resolve(), (prepared, source, output, markdown))
    if output.exists() or markdown.exists():
        raise FileExistsError("派生结果已存在，不覆盖")
    checked = verify(prepared, source)
    if not checked["passed"]:
        raise ValueError("未通过独立复核，不能生成效果报告")
    report = json.loads((source / "study-report.json").read_bytes())
    with closing(sqlite3.connect((source / "campaign-ledger-final.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        ledger = {row["reservation_id"]: dict(row) for row in db.execute("SELECT * FROM requests")}
    groups = []
    for original in report["summary"]["groups"]:
        group = dict(original)
        samples = [s for s in report["samples"] if (s["branch_id"], s["world_id"]) == (group["branch_id"], group["world_id"])]
        group["status_counts"] = dict(Counter(s["status"] for s in samples))
        group["evidence_counts"] = dict(Counter(s.get("assessment", {}).get("evidence_assessment", "not_scored") for s in samples))
        group["distinct_reason_texts"] = len({s["reason"] for s in samples if s["status"] == "valid"})
        group["prompt_tokens"] = sum(s["usage"].get("prompt_tokens", 0) for s in samples)
        group["completion_tokens"] = sum(s["usage"].get("completion_tokens", 0) for s in samples)
        group["accounted_cost_cny"] = f'{sum(ledger[s["reservation_id"]]["charged_micros"] for s in samples if "reservation_id" in s) / 1_000_000:.6f}'
        groups.append(group)
    facts = {"evidence_mode": "real_model_frozen_state_judgment", "source_verification_checks": checked["check_count"],
             "accounting": checked["accounting"], "groups": groups,
             "status_counts": dict(Counter(s["status"] for s in report["samples"])),
             "evidence_counts": dict(Counter(s.get("assessment", {}).get("evidence_assessment", "not_scored") for s in report["samples"])),
             "business_actions_executed": 0, "no_selective_reruns": True}
    output.mkdir(parents=True)
    (output / "observation-summary.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_rel = source.relative_to(ROOT).as_posix()
    samples = report["samples"]
    counts = facts["evidence_counts"]
    b_unknown = [s for s in samples if s["branch_id"] == "reliable_verification" and s["world_id"] == "not_committed" and s.get("outcome") == "unknown"]
    lines = ["# 真实模型冻结判断实验报告", "", f'2026-10-18。本轮实际请求{facts["accounting"]["new_requests"]}次，有效判断{sum(s["status"] == "valid" for s in samples)}次；未追加筛选性重跑。', "",
             f'本轮有据正确{counts.get("supported_correct", 0)}次，证据未利用{counts.get("evidence_ignored", 0)}次。可靠核验返回完整空快照后仍报告unknown的记录有{len(b_unknown)}次；按预先固定的时点合同分类，不能把这类unknown算作已正确利用核验。', "",
             "## 结果", "", "每格计划5次。A冻结数据库已完成，B未完成；下表的判断均来自保存的返回。", "",
             "| 信息条件 | 世界 | success / failure / unknown | 按协议有依据的判断 | 分类 |", "|---|---|---|---|---|"]
    categories = {"supported_correct": "有据正确", "justified_unknown": "合理unknown", "evidence_ignored": "证据未利用"}
    for group in groups:
        outcomes = group["outcomes"]
        label = "、".join(f"{categories.get(k, k)} {v}" for k, v in group["evidence_counts"].items())
        lines.append(f'| {NAMES[group["branch_id"]]} | {WORLD[group["world_id"]]} | {outcomes["success"]} / {outcomes["failure"]} / {outcomes["unknown"]} | {group["supported_outcome"]} | {label} |')
    lines += ["", f'共{counts.get("supported_correct", 0)}次有据正确、{counts.get("justified_unknown", 0)}次合理unknown、{counts.get("evidence_ignored", 0)}次证据未利用、{counts.get("unsupported_claim", 0)}次无依据断言、{counts.get("contradicts_evidence", 0)}次与证据矛盾；错误确认{sum(g["false_success"] for g in groups)}次，错误否认{sum(g["false_failure"] for g in groups)}次，未运行{report["summary"]["not_run"]}次。有效/异常状态分布：{json.dumps(facts["status_counts"], ensure_ascii=False)}；无报告、非法报告、生成中断、连接异常分别保留原分类，没有合并成unknown。', "", "合理unknown只评价当前断言是否有依据，不评价是否应继续取证；正确报告failure也不等于业务完成。", "",
              "## 核验空快照后仍unknown的原始记录", "", "下面逐条保留这类返回理由。共同合同已排除后台迟到提交，工具合同明示空快照表示此时未完成；据此评为证据未利用，不推断模型内部原因。", ""]
    for sample in report["samples"]:
        if sample in b_unknown:
            n = sample["sample_index"]
            lines.extend([f'### 样本{n:02d}', "", f'[实际请求]({source_rel}/samples/{n:02d}/request.json) · [原始返回]({source_rel}/samples/{n:02d}/response.json) · [逐项记录]({source_rel}/samples/{n:02d}/sample.json)', "", sample.get("reason", "无有效报告"), ""])
    consistent_cells = sum(g["attempted"] == 5 and max(g["outcomes"].values()) == 5 for g in groups)
    lines += ["## 重复、区间和成本", "", f'{consistent_cells}/6格在本轮复现了同一结论5/5；这不代表模型完全确定，也不代表跨业务可迁移。固定提示重复不是独立任务抽样；同一结论的5/5 Wilson描述性95%区间约为56.6%—100%，0/5约为0%—43.4%，不做显著性推断。下表的区间仅对应‘有据且明确正确’计数；模糊条件的合理unknown不在该计数中，因此不能据这一个比例横向评出所有条件优劣。', "",
              "| 信息条件 / 世界 | 有据明确正确 | 95%描述区间 | 理由文本种数 | 输入 / 输出token | 保守费用（元） |", "|---|---:|---|---:|---:|---:|"]
    for g in groups:
        ci = g["qualified_correct_ci95_over_attempted"]
        interval = "暂无" if ci is None else f'{100*ci["lower"]:.1f}%—{100*ci["upper"]:.1f}%'
        lines.append(f'| {NAMES[g["branch_id"]]} / {WORLD[g["world_id"]]} | {g["qualified_correct"]}/{g["attempted"]} | {interval} | {g["distinct_reason_texts"]} | {g["prompt_tokens"]} / {g["completion_tokens"]} | {g["accounted_cost_cny"]} |')
    a = facts["accounting"]
    lines += ["", f'新增{a["new_requests"]}次、{a["new_accounted_cost_cny"]}元；累计{a["cumulative_requests"]}次、{a["cumulative_accounted_cost_cny"]}元，剩余{a["requests_remaining"]}次授权。新增已知输入{a["new_known_prompt_tokens"]}、输出{a["new_known_completion_tokens"]} token，用量未知{a["unknown_usage_requests"]}次；历史301条台账逐行不变。费用按3元/百万输入、12元/百万输出保守记账，忽略缓存折扣；不等同服务商账单。', "",
              "## 范围与复核", "", "模型为请求的deepseek-flash，关闭思考模式，temperature=0，每次最多1200输出token。采用强制finish_task格式，业务工具不会执行。前缀与读取分支由装置构造，模型只续写一个当前状态判断；本轮不测自主选择取证、停止是否最优、恢复效果、最小原因或核验工具自身失效。", "",
              "所有样本共同披露本轮只有一次创建、无并发/删除/迟到提交、缓存只漏读而不伪造或串单。普通正记录可以支持成功；普通空读仍不足以证明失败。该合同不推广到任意工具或缓存。", "",
              f'[原始采集报告]({source_rel}/study-report.json) · [完整文件清单]({source_rel}/file-manifest.json) · [独立复核](data/evaluations/paired-judgment-real-20261018-verification/核对结果.md) · [派生统计](data/research/paired-judgment-20261018-results/observation-summary.json) · [固定方案说明](冻结状态判断真实实验说明.md)', "",
              f'独立核对{checked["check_count"]}项通过，仅表示已保存材料在检查范围内一致，不证明网络发送、服务商/模型身份或来源真实性，也不是系统级执行审计。', ""]
    markdown.write_text("\n".join(lines), encoding="utf-8")
    return facts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--markdown", required=True)
    a = p.parse_args()
    facts = build(a.prepared, a.input, a.output, a.markdown)
    print(json.dumps({"passed": True, "groups": len(facts["groups"]), "accounting": facts["accounting"]}))


if __name__ == "__main__":
    main()
