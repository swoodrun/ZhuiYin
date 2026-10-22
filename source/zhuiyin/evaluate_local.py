"""Run an auditable local rule matrix; never contact an external model.

This is an evaluation CLI, not a test with predetermined winning strategies.
Every matrix value comes from a fresh engine.run_trial / real SQLite Lab.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import itertools
import json
from pathlib import Path
import sys
import time
import uuid

from engine import Config, FAULTS, STRATEGIES, report_provenance, run_trial


INFRA_CODES = {"MODEL_EXECUTION_ERROR", "AGENT_DID_NOT_FINISH"}
OUTCOME_FIELDS = ("failed", "actual_success", "failure_fingerprint")
SCOPE_LIMITATIONS = [
    "全部执行使用规则模式和本机 SQLite；没有调用真实模型。",
    "矩阵穷举当前七类本地故障的子集，只使用设备库存6、数量2和默认工单这两组业务参数。",
    "四种策略各自使用全新数据库。策略的业务结果直接来自数据库裁判，未指定某一组应当获胜。",
    "失败、实际完成和错误指纹分别比较；没有差异只表示这组有限条件下未观察到差异。",
    "本地故障组合和安排好的并发交错分别报告，均不能替代真实故障迁移或真实模型的实验。",
]


def _timestamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _write_json(path, value):
    # A new evaluation directory and exclusive writes avoid replacing evidence
    # from an earlier run. No uploaded SQL or source text is read or executed.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _fixtures():
    return [
        Config.from_dict({"scenario": "equipment", "stock": 6, "quantity": 2,
                          "agent_mode": "rules", "faults": []}),
        Config.from_dict({"scenario": "ticket", "agent_mode": "rules", "faults": []}),
    ]


def evaluate_matrix():
    """Execute all faults/strategies, then compare observed paired outcomes."""
    started = time.perf_counter()
    fault_order, strategy_order = list(FAULTS), list(STRATEGIES)
    fixtures = _fixtures()
    rows = []
    for fixture in fixtures:
        for size in range(len(fault_order) + 1):
            for combo in itertools.combinations(fault_order, size):
                config = Config.from_dict({**fixture.public(), "faults": list(combo)})
                row = {"case_id": f"{fixture.scenario}-{len(rows) + 1:03d}",
                       "scenario": fixture.scenario, "config": config.public(),
                       "faults": list(combo), "strategies": {}}
                for strategy in strategy_order:
                    trial = run_trial(config, list(combo), strategy=strategy, agent_mode="rules")
                    codes = [violation["code"] for violation in trial["violations"]]
                    row["strategies"][strategy] = {
                        "failed": trial["failed"], "actual_success": trial["actual_success"],
                        "failure_fingerprint": trial["failure_fingerprint"],
                        "reported_success": trial["reported_success"],
                        "physical_writes": trial["stats"]["physical_writes"],
                        "violation_codes": codes,
                        "execution_status": "execution_error" if INFRA_CODES.intersection(codes) else "completed",
                    }
                rows.append(row)
                if len(rows) % 32 == 0:
                    print(f"规则矩阵：{len(rows)} / {len(fixtures) * 2 ** len(fault_order)} 组", file=sys.stderr)
    matrix = {"kind": "local_rule_matrix", "status": "finished", "created_at": _timestamp(),
              "mode": "rules", "model_identity": None,
              "model_usage": {"requests": 0, "external_model_verified": False},
              "fault_order": fault_order, "fault_labels": dict(FAULTS),
              "strategy_order": strategy_order,
              "strategy_labels": {key: value["label"] for key, value in STRATEGIES.items()},
              "fixtures": [config.public() for config in fixtures],
              "provenance_by_fixture": {config.scenario: report_provenance(config) for config in fixtures},
              "rows": rows, "limitations": list(SCOPE_LIMITATIONS),
              "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
    return matrix, summarize_matrix(matrix)


def summarize_matrix(matrix):
    """Compute differences from measurements, without assuming equivalence."""
    rows = matrix["rows"]
    by_strategy = {}
    for strategy in matrix["strategy_order"]:
        measurements = [row["strategies"][strategy] for row in rows]
        valid = [item for item in measurements if item["execution_status"] == "completed"]
        by_strategy[strategy] = {
            "trials": len(measurements), "valid_trials": len(valid),
            "execution_errors": len(measurements) - len(valid),
            "business_failures": sum(item["failed"] for item in valid),
            "actual_completions": sum(item["actual_success"] for item in valid),
            "physical_writes": sum(item["physical_writes"] for item in measurements),
        }
    differences, physical_differences, uncomparable = [], [], []
    for row in rows:
        left, right = (row["strategies"][strategy] for strategy in ("verification_only", "verified"))
        identity = {key: row[key] for key in ("case_id", "scenario", "faults")}
        if left["execution_status"] != "completed" or right["execution_status"] != "completed":
            uncomparable.append(identity)
            continue
        differing_fields = [field for field in OUTCOME_FIELDS if left[field] != right[field]]
        if differing_fields:
            differences.append({**identity, "differing_fields": differing_fields,
                                "verification_only": {key: left[key] for key in OUTCOME_FIELDS},
                                "verified": {key: right[key] for key in OUTCOME_FIELDS}})
        if left["physical_writes"] != right["physical_writes"]:
            physical_differences.append({**identity, "verification_only": left["physical_writes"],
                                         "verified": right["physical_writes"]})
    comparable = len(rows) - len(uncomparable)
    return {"kind": "local_rule_matrix_summary", "status": matrix["status"],
            "created_at": matrix["created_at"], "mode": "rules", "external_model_requests": 0,
            "fault_types": len(matrix["fault_order"]),
            "combinations_per_fixture": 2 ** len(matrix["fault_order"]),
            "fixture_count": len(matrix["fixtures"]), "case_count": len(rows),
            "trial_count": sum(item["trials"] for item in by_strategy.values()),
            "by_strategy": by_strategy,
            "verification_only_vs_verified": {
                "fields": list(OUTCOME_FIELDS), "comparable_pairs": comparable,
                "different_outcome_count": len(differences), "differences": differences,
                "physical_write_difference_count": len(physical_differences),
                "physical_write_differences": physical_differences,
                "uncomparable_pairs": uncomparable,
                "interpretation": (f"{comparable}组可比较条件中，业务结果有{len(differences)}组不同。"
                                   "这只描述所列本地条件，不能推断其他业务或模型的优胜关系。")},
            "elapsed_ms": matrix["elapsed_ms"], "limitations": list(SCOPE_LIMITATIONS),
            "matrix_file": "matrix.json"}


def evaluate_concurrency(directory):
    """Keep the new local re-entry cases separate from the 1,024-trial matrix."""
    from concurrency_lab import run_concurrency_suite

    entries = []
    for config in _fixtures():
        relative = f"concurrency-{config.scenario}"
        output = directory / relative
        report = run_concurrency_suite(config, output)
        _write_json(output / "report.json", report)
        entries.append({"scenario": config.scenario, "status": report["status"],
                        "completed": report["completed"], "planned": report["planned"],
                        "report_file": f"{relative}/report.json",
                        "cases": [{key: case[key] for key in
                                   ("schedule", "strategy", "failed", "actual_success", "failure_fingerprint", "database_filename")}
                                  for case in report["cases"]]})
    result = {"kind": "local_concurrency_summary", "mode": "rules",
              "external_model_requests": 0, "created_at": _timestamp(), "fixtures": entries,
              "limitations": ["两个业务各检查先后提交和安排好的并发提交，每种安排使用四种策略。",
                              "实际 SQLite 文件保存于各业务子目录；结果没有并入原故障矩阵。",
                              "这只是本机交错检查，不能推断真实分布式环境或真实模型的可靠性。"]}
    _write_json(directory / "concurrency-summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行双业务、七类故障、四策略的本地规则矩阵，不调用模型。")
    parser.add_argument("--output-dir", type=Path, help="新的输出目录；已有目录不会覆盖")
    parser.add_argument("--include-concurrency", action="store_true", help="另外运行双业务重复提交检查，并保存真实数据库")
    arguments = parser.parse_args(argv)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    directory = arguments.output_dir or (Path(__file__).resolve().parent / "data" / "evaluations" / f"{stamp}-{uuid.uuid4().hex[:8]}")
    directory = directory.resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("输出目录已存在；请指定一个新的目录，避免覆盖旧证据。")
    matrix, summary = evaluate_matrix()
    _write_json(directory / "matrix.json", matrix)
    _write_json(directory / "summary.json", summary)
    concurrency = evaluate_concurrency(directory) if arguments.include_concurrency else None
    print(json.dumps({"output_dir": str(directory), "mode": "rules",
                      "trial_count": summary["trial_count"], "external_model_requests": 0,
                      "verification_only_vs_verified": summary["verification_only_vs_verified"],
                      "concurrency_fixture_count": len(concurrency["fixtures"]) if concurrency else 0},
                     ensure_ascii=False, indent=2))
    return 2 if any(arm["execution_errors"] for arm in summary["by_strategy"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
