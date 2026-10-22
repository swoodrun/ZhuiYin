"""Build a self-contained, offline replay from saved evidence, never a new run."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.eight_arm_experiment import fixed_design, readonly_budget, readiness
from tools.verify_evidence import verify
from tools.verify_eight_arm import verify as verify_study


def build(output=None):
    evaluation = ROOT / "data/evaluations"
    paths = {
        "v06": evaluation / "v06-protocol-20261017/case-001-report.json",
        "v061": evaluation / "v061-review-20261017/case-001-report.json",
        "old_zip": evaluation / "追因-v0.6升级与真实同错精简-20261017.zip",
        "new_zip": evaluation / "追因-v0.6.1评价修正与交互案例-20261017.zip",
        "A": ROOT / "data/verifications/verifier-consistency-20261017/synthetic-negative-copies/A-false-failed-SYNTHETIC.zip",
        "B": ROOT / "data/verifications/verifier-consistency-20261017/synthetic-negative-copies/B-denied-final-runs-SYNTHETIC.zip",
    }
    cases = {key: json.loads(paths[key].read_text(encoding="utf-8")) for key in ("v06", "v061")}
    checks = {key: verify(paths[key]) for key in ("old_zip", "new_zip", "A", "B")}
    if not checks["old_zip"]["passed"] or not checks["new_zip"]["passed"] or checks["A"]["passed"] or checks["B"]["passed"]:
        raise ValueError("演示来源核验不符合预期，停止生成")
    for key in ("A", "B"):
        if any(report["conclusion_status"] != "unverified" for report in checks[key]["reports"]):
            raise ValueError("矛盾材料必须保留未验证结论")
    design, budget = fixed_design(), readonly_budget()
    real_root = evaluation / "eight-arm-real-20261018"
    real_study = None
    if (real_root / "run/file-manifest.json").is_file():
        policy = real_root / "campaign/campaign-policy.json"
        verified = verify_study(real_root / "run", real_root / "prepared", policy)
        if not verified["passed"]:
            raise ValueError("真实八组材料未通过内部一致性核对，不能写入演示结论")
        real_report = json.loads((real_root / "run/study-report.json").read_text(encoding="utf-8"))
        design = json.loads((real_root / "prepared/fixed-design.json").read_text(encoding="utf-8"))
        budget = readonly_budget(policy)
        real_study = {"status": real_report["status"], "mode": real_report["mode"],
                      "attempted": real_report["completed"], "planned": real_report["planned"],
                      "arms": verified["arms"], "trials": verified["trials"],
                      "accounting": verified["accounting"], "checks": verified["check_count"],
                      "source": "data/evaluations/eight-arm-real-20261018/真实八组对照报告.md"}
        paths["real_report"] = real_root / "run/study-report.json"
        paths["real_design"] = real_root / "prepared/fixed-design.json"
        paths["real_verification"] = real_root / "final-verification/verification.json"
    data = {
        "edition": "2026-10-18", "replay_only": True, "current_new_model_requests": 0,
        "cases": {key: {"config": report["config"], "mode": report["mode"],
                         "baseline": report["baseline"], "fixed": report["fixed"],
                         "minimization": report["minimization"], "model_usage": report["model_usage"]}
                  for key, report in cases.items()},
        "verifications": {key: {"passed": value["passed"], "checks": len(value["checks"]),
                                 "errors": [item["check"] for item in value["checks"] if not item["passed"]],
                                 "reports": value["reports"]} for key, value in checks.items()},
        "coverage": checks["new_zip"]["coverage"], "not_covered": checks["new_zip"]["not_covered"],
        "design": design, "budget": budget, "readiness": readiness(design, budget),
        "real_study": real_study,
        "sources": [{"name": key, "path": path.relative_to(ROOT).as_posix(),
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for key, path in paths.items()],
    }
    # Raw provider dialogues are not in saved evidence. Only archived public
    # events/rows/usage are embedded; no model settings or keys are requested.
    encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    template = (ROOT / "templates/defense.html").read_text(encoding="utf-8")
    content = template.replace("__EVIDENCE_DATA__", encoded)
    target = Path(output) if output else ROOT / "答辩演示.html"
    target.write_text(content, encoding="utf-8")
    return {"output": str(target.resolve()), "bytes": target.stat().st_size,
            "sources": len(paths), "new_model_requests": 0, "replay_only": True}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False))
