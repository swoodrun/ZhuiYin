"""Bounded, data-only batch intake and explicit rules-mode test execution.

Uploaded bytes are never extracted or executed. Only freshly generated engine
SQLite dumps cross the trusted output boundary in ``_save_generated_trial``.
"""
from __future__ import annotations

import copy
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import uuid
import zipfile

from credentials import sensitive_fields
from retest_templates import decorate_retest_trial
from engine import Config, STRATEGIES, evaluation_summary, run_experiment, run_trial, _trial_incomplete
from ingest import MAX_BYTES, parse_upload, _json

WORKFLOW_VERSION = "batch-test-v1"
MAX_UPLOAD_FILES = 200
MAX_CASES = 500
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
SUPPORTED = {".json", ".csv", ".jsonl", ".ndjson"}
IGNORED_PARTS = {"answer", "answers", "answer-key", "answer_keys", "records", "record", "metadata", "__macosx"}
PROFILES = {"quick", "full", "agent"}
SKIPPED_QUICK = ["fault_minimization", "exhaustive_regression", "expanded_regression"]
SCRIPT_FIELDS = {"sqlite_dump", "sql_dump", "script", "scripts", "source_code", "python_code", "javascript", "shell_command", "executable"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _safe(value, context=None):
    """Remove credential and script containers from durable data snapshots."""
    if isinstance(value, dict):
        private = sensitive_fields(value, context)
        return {key: _safe(item, key) for key, item in value.items()
                if key not in private and str(key).lower() not in SCRIPT_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_safe(item, context) for item in value]
    return value


def _label(value):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value))[:300]


def _safe_member(name):
    if not isinstance(name, str) or not name or len(name) > 500 or "\\" in name or "\x00" in name:
        raise ValueError("ZIP 路径无效")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ValueError("ZIP 不接受绝对路径")
    parts = name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} or ":" in part or any(ord(c) < 32 for c in part) for part in parts):
        raise ValueError("ZIP 路径含跳转或不安全片段")
    return "/".join(parts)


def _ignored(name):
    parts = PurePosixPath(name.lower()).parts
    return (any(part in IGNORED_PARTS for part in parts)
            or any(re.match(r"^(?:answers?|metadata|records?)[_.-]", part) for part in parts))


def _counts(cases, files):
    return {"files": len(files), "accepted_files": sum(f["status"] == "accepted" for f in files),
            "ignored_files": sum(f["status"] == "ignored" for f in files),
            "file_errors": sum(f["status"] == "import_error" for f in files),
            "cases": len(cases), "scenarios": sum(c["kind"] == "scenario" for c in cases),
            "traces": sum(c["kind"] == "trace" for c in cases),
            **{name: sum(c["status"] == status for c in cases) for name, status in
               (("ready", "ready"), ("duplicates", "duplicate"), ("conflicts", "conflict"),
                ("import_errors", "import_error"), ("completed", "completed"),
                ("execution_errors", "execution_error"), ("not_run", "not_run"))}}


def _refresh(plan):
    plan["counts"] = _counts(plan["cases"], plan["files"])
    plan["summary"] = dict(plan["counts"])
    # The frontend can use either spelling without losing the stable row IDs.
    if plan.get("status") == "prepared":
        plan["items"] = plan["cases"]
    return plan


def public_plan(plan):
    return copy.deepcopy(_safe(plan))


def prepare_uploads(files, profile="quick"):
    """Preview many text files or ZIPs without running a scenario.

    Each input is ``{filename, content: str}`` or ``{filename, data: bytes}``.
    Ordinary leaf/member/row failures remain visible alongside accepted rows.
    Envelope limits reject an abusive whole request before execution exists.
    """
    if profile not in PROFILES:
        raise ValueError("批量测试档位仅支持 quick 或 full")
    if not isinstance(files, list) or not files or len(files) > MAX_UPLOAD_FILES:
        raise ValueError(f"一次需要 1—{MAX_UPLOAD_FILES} 个上传文件")
    uploads, total = [], 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("上传文件需要 filename 与 content/data")
        filename = item.get("filename", "")
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 500:
            raise ValueError("上传文件名无效")
        if "data" in item:
            raw = item["data"]
            if not isinstance(raw, (bytes, bytearray)):
                raise ValueError("上传 data 必须为字节内容")
            raw = bytes(raw)
        else:
            content = item.get("content")
            if not isinstance(content, str):
                raise ValueError("上传 content 必须为 UTF-8 文本")
            try:
                raw = content.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("上传内容不是有效 UTF-8 文本") from None
        total += len(raw)
        if total > MAX_UPLOAD_BYTES:
            raise ValueError("上传总大小超过 20 MiB")
        uploads.append((filename, raw))
    plan = {"kind": "test_suite", "workflow_version": WORKFLOW_VERSION, "profile": profile,
            "status": "prepared", "created_at": _now(), "files": [], "cases": [], "warnings": [],
            "limits": {"upload_files": MAX_UPLOAD_FILES, "cases": MAX_CASES, "leaf_bytes": MAX_BYTES,
                       "upload_bytes": MAX_UPLOAD_BYTES, "expanded_bytes": MAX_EXPANDED_BYTES},
            "skipped_stages": SKIPPED_QUICK[:] if profile == "quick" else [],
            "progress": {"completed": 0, "total": 0, "percent": 0}}
    expanded = 0
    declared_expanded = 0
    archive_entries = 0

    def add_case(value):
        if len(plan["cases"]) >= MAX_CASES:
            raise ValueError(f"展开后最多允许 {MAX_CASES} 条逻辑用例（含错误和重复项）")
        value["case_id"] = f"case-{len(plan['cases']) + 1:04d}"
        plan["cases"].append(value)

    def error(origin, message, source_hash=None):
        value = {"kind": "import_error", "name": "导入错误", "status": "import_error",
                 "origin": origin, "error": _label(message)}
        if source_hash:
            value["source_sha256"] = source_hash
        add_case(value)

    def leaf(filename, raw, member=None):
        nonlocal expanded
        origin = {"filename": _label(filename)}
        if member is not None:
            origin["member"] = _label(member)
        record = {**origin, "size_bytes": len(raw), "source_sha256": _sha(raw), "status": "accepted"}
        plan["files"].append(record)
        expanded += len(raw)
        if expanded > MAX_EXPANDED_BYTES:
            raise ValueError("展开后的文件总大小超过 32 MiB")
        digest = record["source_sha256"]
        try:
            if not raw or len(raw) > MAX_BYTES:
                raise ValueError("文件为空或超过单文件 2 MiB 限制")
            name = member or filename
            if PurePosixPath(name.lower()).suffix not in SUPPORTED:
                raise ValueError("仅支持 JSON、CSV、JSONL、NDJSON 或 ZIP")
            text = raw.decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError) as exc:
            record.update(status="import_error", error=_label(exc))
            error(origin, str(exc), digest)
            return

        def normalized(payload, index):
            entry_origin = {**origin, "index": index}
            try:
                parsed = parse_upload(PurePosixPath(name).name, payload)
                for warning in parsed.get("warnings", []):
                    if warning not in plan["warnings"]:
                        plan["warnings"].append(warning)
                if parsed["kind"] == "scenarios":
                    for scenario in parsed["scenarios"]:
                        cfg = _safe(scenario["config"])
                        semantic = {**cfg, "faults": sorted(cfg["faults"])}
                        add_case({"kind": "scenario", "status": "ready", "name": scenario["name"],
                                  "description": scenario.get("description", ""), "origin": entry_origin,
                                  "request_id": cfg["request_id"], "source_sha256": digest,
                                  "config_sha256": _sha(_bytes(semantic)), "config": cfg,
                                  "stored_safe_sha256": _sha(_bytes(cfg))})
                else:
                    for subindex, analysis in enumerate(parsed["analyses"], 1):
                        safe = _safe(analysis)
                        add_case({"kind": "trace", "status": "ready", "name": safe.get("name", "静态日志"),
                                  "origin": {**entry_origin, "analysis_index": subindex},
                                  "request_id": safe.get("task", {}).get("request_id", ""),
                                  "source_sha256": digest, "stored_safe_sha256": _sha(_bytes(safe)),
                                  "analysis": safe})
            except (ValueError, TypeError, RecursionError) as exc:
                if str(exc).startswith("展开后最多允许"):
                    raise
                record["status"] = "import_error"
                error(entry_origin, str(exc), digest)

        suffix = PurePosixPath(name.lower()).suffix
        # Split only scenario containers. Every row still passes the original
        # validator, including all field/credential/mode restrictions.
        if suffix == ".csv":
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            try:
                header = next(reader)
                count = 0
                for index, row in enumerate(reader, 1):
                    if not row or not any(cell.strip() for cell in row):
                        continue
                    output = io.StringIO(newline="")
                    writer = csv.writer(output)
                    writer.writerow(header)
                    writer.writerow(row)
                    normalized(output.getvalue(), index)
                    count += 1
                if not count:
                    normalized(text, 1)
            except (StopIteration, csv.Error) as exc:
                record["status"] = "import_error"
                error({**origin, "index": max(1, reader.line_num)}, "CSV 格式错误或文件为空", digest)
        elif suffix == ".json":
            try:
                data, _ = _json(text)
            except (ValueError, TypeError, RecursionError):
                normalized(text, 1)
                return
            if (isinstance(data, dict) and data.get("kind") == "scenarios"
                    and set(data) <= {"kind", "scenarios"} and isinstance(data.get("scenarios"), list)
                    and data["scenarios"]):
                for index, scenario in enumerate(data["scenarios"], 1):
                    normalized(json.dumps({"kind": "scenarios", "scenarios": [scenario]}, ensure_ascii=False), index)
            else:
                normalized(text, 1)
        else:
            normalized(text, 1)

    for filename, raw in uploads:
        if PurePosixPath(filename.lower()).suffix != ".zip":
            declared_expanded += len(raw)
            if declared_expanded > MAX_EXPANDED_BYTES:
                raise ValueError("展开后的文件总大小超过 32 MiB")
            leaf(filename, raw)
            continue
        archive_origin = {"filename": _label(filename)}
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except (zipfile.BadZipFile, OSError):
            plan["files"].append({**archive_origin, "status": "import_error", "size_bytes": len(raw)})
            error(archive_origin, "ZIP 文件损坏或格式无效", _sha(raw))
            continue
        with archive:
            entries = archive.infolist()
            archive_entries += len(entries)
            if archive_entries > MAX_ZIP_ENTRIES:
                raise ValueError(f"本次上传的 ZIP 合计最多允许 {MAX_ZIP_ENTRIES} 个目录项")
            declared_expanded += sum(info.file_size for info in entries)
            if declared_expanded > MAX_EXPANDED_BYTES:
                raise ValueError("ZIP 声明的展开总大小超过 32 MiB")
            safe_entries, seen, duplicate_paths = [], set(), set()
            for info in entries:
                try:
                    name = _safe_member(info.filename)
                    folded = name.casefold()
                    if folded in seen:
                        duplicate_paths.add(folded)
                    seen.add(folded)
                    safe_entries.append((info, name))
                except ValueError as exc:
                    plan["files"].append({**archive_origin, "member": _label(info.filename), "status": "import_error"})
                    error({**archive_origin, "member": _label(info.filename)}, str(exc))
            has_inputs = any("inputs" in PurePosixPath(name.lower()).parts[:-1]
                             for info, name in safe_entries if not info.is_dir())
            for info, name in safe_entries:
                if info.is_dir():
                    continue
                origin = {**archive_origin, "member": _label(name)}
                mode = (info.external_attr >> 16) & 0xFFFF
                reason = None
                if name.casefold() in duplicate_paths:
                    reason = "ZIP 存在重复路径（不区分大小写），该路径全部跳过"
                elif stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                    reason = "ZIP 不接受符号链接或特殊文件"
                elif info.flag_bits & 1:
                    reason = "ZIP 不接受加密成员"
                elif info.file_size > MAX_BYTES:
                    reason = "ZIP 成员超过单文件 2 MiB 限制"
                elif info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 200:
                    reason = "ZIP 成员压缩比过高"
                if reason:
                    plan["files"].append({**origin, "status": "import_error"})
                    error(origin, reason)
                elif (_ignored(name) or PurePosixPath(name.lower()).suffix not in SUPPORTED
                      or (has_inputs and "inputs" not in PurePosixPath(name.lower()).parts[:-1])):
                    plan["files"].append({**origin, "status": "ignored", "reason": "非输入数据或不支持的附件；未读取"})
                else:
                    try:
                        with archive.open(info) as source:
                            payload = source.read(MAX_BYTES + 1)
                        if len(payload) != info.file_size:
                            raise ValueError("ZIP 成员实际大小与目录声明不一致")
                        leaf(filename, payload, name)
                    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, ValueError) as exc:
                        if str(exc).startswith(("展开后最多允许", "展开后的文件总大小")):
                            raise
                        plan["files"].append({**origin, "status": "import_error"})
                        error(origin, "ZIP 成员无法安全读取：" + _label(exc))

    scenarios = {}
    logs = {}
    for case in plan["cases"]:
        if case["kind"] == "scenario":
            scenarios.setdefault(case["request_id"], []).append(case)
        elif case["kind"] == "trace":
            key = (case["source_sha256"], case["origin"].get("analysis_index", 1))
            if key in logs:
                case.update(status="duplicate", duplicate_of=logs[key]["case_id"])
            else:
                logs[key] = case
    for group in scenarios.values():
        if len({case["config_sha256"] for case in group}) > 1:
            for case in group:
                case.update(status="conflict", error="相同请求编号对应不同场景配置；请更换编号或移除冲突后重新预览")
        else:
            for case in group[1:]:
                case.update(status="duplicate", duplicate_of=group[0]["case_id"])
    plan["progress"]["total"] = sum(case["status"] == "ready" for case in plan["cases"])
    return public_plan(_refresh(plan))


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def save_prepared(root, plan):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    suite_id = uuid.uuid4().hex
    destination = root / suite_id
    destination.mkdir()
    safe = public_plan(plan)
    safe["suite_id"] = suite_id
    _write_json(destination / "prepared.json", safe)
    plan["suite_id"] = suite_id
    return suite_id


def load_prepared(root, suite_id):
    if not isinstance(suite_id, str) or not re.fullmatch(r"[a-f0-9]{32}", suite_id):
        raise ValueError("测试批次编号无效")
    path = Path(root) / suite_id / "prepared.json"
    if path.is_symlink():
        raise ValueError("测试计划路径不安全")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_generated_trial(folder, strategy, trial):
    """Accept ONLY direct return values of trusted engine runners in this call.

    This private function is deliberately absent from upload/plan loading code.
    Uploaded sqlite_dump is stripped at intake and never reaches this boundary.
    """
    artifacts = {}
    dump = trial.get("sqlite_dump")
    if isinstance(dump, str):
        path = folder / f"{strategy}.sqlite"
        if path.exists() or path.is_symlink():
            raise ValueError("生成数据库目标已存在，拒绝覆盖")
        with closing(sqlite3.connect(path)) as database:
            database.executescript(dump)
            database.commit()
        artifacts["database"] = f"cases/{folder.name}/{path.name}"
    _write_json(folder / f"{strategy}.json", trial)
    artifacts["report"] = f"cases/{folder.name}/{strategy}.json"
    return artifacts


def _execution_valid(trial):
    return isinstance(trial, dict) and trial.get("completed") is True and not _trial_incomplete(trial)


def _result_totals(report):
    trials = []
    planned = 0
    for case in report["cases"]:
        if case["kind"] == "scenario" and case["status"] not in {"duplicate", "conflict"}:
            planned += len(STRATEGIES)
            trials.extend(trial for trial in case.get("strategies", {}).values() if trial.get("status") != "not_run")
    valid = [trial for trial in trials if _execution_valid(trial)]
    report["evaluation"] = {"planned_trials": planned, "attempted_trials": len(trials),
        "not_run_trials": planned - len(trials), "execution_valid_trials": len(valid),
        "execution_invalid_trials": len(trials) - len(valid),
        "db_actual_success": sum(t.get("actual_success") is True for t in valid),
        "db_actual_failure": sum(t.get("actual_success") is False for t in valid),
        "db_actual_unknown": sum(t.get("actual_success") is None for t in valid),
        "business_violation_trials": sum(bool(t.get("business_violations")) for t in valid),
        "reporting_violation_trials": sum(bool(t.get("reporting_violations")) for t in valid),
        "reported_outcomes": {outcome: sum(t.get("reported_outcome") == outcome for t in valid)
                              for outcome in ("success", "failure", "unknown")},
        "scope": "仅新生成的有效执行；重复、冲突、未运行、静态日志和执行错误不计入数据库结果统计"}


def execute_suite(plan, output_dir, progress_callback=None, should_stop=None, quick_runner=None, full_runner=None, *, agent_config=None, report_transform=None, stop_reason=None):
    """Execute a saved preview, isolating failures and persisting every boundary."""
    if plan.get("profile", "quick") not in PROFILES:
        raise ValueError("批量测试档位无效")
    report = public_plan(plan)
    if isinstance(plan.get('retest_source'), dict):
        report['retest_source'] = _safe(plan['retest_source'])
    report.pop("items", None)
    report.update(kind="test_suite", workflow_version=WORKFLOW_VERSION, status="running", started_at=_now())
    profile = report.get("profile", "quick")
    report["skipped_stages"] = SKIPPED_QUICK[:] if profile in ("quick", "agent") else []
    if profile == "agent" and (agent_config is None or agent_config.agent_mode != "llm"):
        raise ValueError("Agent 批量测试需要有效的模型配置")
    report["agent_mode"] = "llm" if profile == "agent" else "rules"
    output = Path(output_dir)
    if output.is_symlink():
        raise ValueError("批次输出目录不接受符号链接")
    output.mkdir(parents=True, exist_ok=True)
    cases_folder = output / "cases"
    if cases_folder.is_symlink():
        raise ValueError("批次用例目录不接受符号链接")
    cases_folder.mkdir(exist_ok=True)
    quick_runner = quick_runner or run_trial
    full_runner = full_runner or run_experiment
    stop = should_stop or (lambda: False)
    completed = 0
    total = sum(case["status"] == "ready" for case in report["cases"])
    # Large trace events live once in per-case JSON; polling/checkpoints retain
    # the already computed static judgments and an artifact reference.
    for case in report["cases"]:
        if case["kind"] != "trace":
            continue
        folder = cases_folder / case["case_id"]
        folder.mkdir(exist_ok=True)
        analysis = case["analysis"]
        _write_json(folder / "analysis.json", analysis)
        case["analysis"] = {key: analysis[key] for key in ("name", "task", "verdict", "summary", "stats",
            "evaluation_protocol", "actual_success", "business_failed", "reported_outcome", "report_assessment",
            "report_failed", "evidence_complete", "execution_status", "import_verification") if key in analysis}
        for key in ("business_violations", "reporting_violations", "execution_violations"):
            if key in analysis:
                case["analysis"][key] = [{k: item[k] for k in ("code", "severity", "message") if k in item}
                                          for item in analysis[key]]
        case["analysis"]["findings_count"] = len(analysis.get("findings", []))
        case["artifacts"] = {"analysis": f"cases/{case['case_id']}/analysis.json"}

    def publish():
        report["progress"] = {"completed": completed, "total": total,
                              "percent": int(completed * 100 / total) if total else 100}
        _refresh(report)
        _result_totals(report)
        if report_transform:
            report_transform(report)
        _write_json(output / "suite-report.json", report)
        if progress_callback:
            progress_callback(public_plan(report))

    publish()
    cancelled = False
    for case in report["cases"]:
        if case["status"] != "ready":
            folder = cases_folder / case["case_id"]
            folder.mkdir(exist_ok=True)
            _write_json(folder / "case.json", case)
            case.setdefault("artifacts", {})["case_json"] = f"cases/{case['case_id']}/case.json"
            continue
        if cancelled or stop():
            cancelled = True
            case.update(status="not_run", stopped_reason=stop_reason() if stop_reason else "cancelled")
            folder = cases_folder / case["case_id"]
            folder.mkdir(exist_ok=True)
            _write_json(folder / "case.json", case)
            case.setdefault("artifacts", {})["case_json"] = f"cases/{case['case_id']}/case.json"
            continue
        if case["kind"] == "trace":
            case.update(status="completed", execution_scope="static_import_only", completed_at=_now())
            folder = cases_folder / case["case_id"]
            folder.mkdir(exist_ok=True)
            _write_json(folder / "case.json", case)
            case.setdefault("artifacts", {})["case_json"] = f"cases/{case['case_id']}/case.json"
            # Preview analyses are already evaluated; runtime does no static recheck.
            completed += 1
            publish()
            continue
        folder = cases_folder / case["case_id"]
        if folder.exists():
            case.update(status="execution_error", error="用例输出目录已存在，拒绝覆盖")
            completed += 1
            publish()
            continue
        folder.mkdir()
        case.update(strategies={}, artifacts={}, execution_scope="generated_sqlite_agent" if profile == "agent" else "generated_sqlite_rules", started_at=_now())
        try:
            # Revalidate each input. Agent limits/prices come from the workbench;
            # the enclosing Agent suite owns the shared budget, never this case.
            cfg = Config.from_dict({**case["config"], "agent_mode": "llm" if profile == "agent" else "rules",
                "model_config": agent_config.model_config if profile == "agent" else {},
                **({"prompt_policy": agent_config.prompt_policy,
                    "max_requests": agent_config.max_requests,
                    "billing": agent_config.billing} if profile == "agent" else {})})
            if profile == "agent":
                case["input_config"] = case["config"]
                case["config"] = cfg.public()
            if profile in ("quick", "agent"):
                for strategy in STRATEGIES:
                    if stop():
                        cancelled = True
                        case["strategies"][strategy] = {"strategy": strategy, "status": "not_run", "completed": False,
                            "stopped_reason": stop_reason() if stop_reason else "cancelled", "actual_success": None, "reported_outcome": None,
                            "business_violations": [], "execution_violations": []}
                        continue
                    try:
                        fresh = Config.from_dict({**cfg.public(), "model_config": cfg.model_config})
                        trial = quick_runner(fresh, list(fresh.faults), strategy, cfg.agent_mode)
                        if not isinstance(trial, dict):
                            raise ValueError("策略未返回有效执行记录")
                        trial = decorate_retest_trial(trial, case, cfg)
                        case["artifacts"][strategy] = _save_generated_trial(folder, strategy, trial)
                        case["strategies"][strategy] = evaluation_summary(_safe(trial))
                        if "template_contract" in trial:
                            case["strategies"][strategy]["template_contract"] = _safe(trial["template_contract"])
                        cancelled = cancelled or bool(stop())
                    except Exception as exc:
                        case["strategies"][strategy] = {"strategy": strategy, "status": "infra_error", "completed": False,
                            "actual_success": None, "reported_outcome": None, "business_violations": [],
                            "execution_violations": [{"code": "SUITE_EXECUTION_ERROR", "message": "Agent 执行异常；已有请求用量已保留" if profile == "agent" else _label(exc)}]}
                    _write_json(folder / "case.json", case)
                    publish()
            else:
                generated = full_runner(cfg, cancel_check=stop)
                if not isinstance(generated, dict):
                    raise ValueError("完整实验未返回有效报告")
                _write_json(folder / "full-report.json", generated)
                case["artifacts"]["full_report"] = f"cases/{case['case_id']}/full-report.json"
                arms = generated.get("ablation", {}).get("arms", [])
                for strategy in STRATEGIES:
                    trial = next((arm["result"] for arm in arms if arm.get("strategy") == strategy and isinstance(arm.get("result"), dict)), None)
                    if trial is None and strategy in {"baseline", "verified"}:
                        trial = generated.get("baseline" if strategy == "baseline" else "fixed")
                    if isinstance(trial, dict):
                        trial = decorate_retest_trial(trial, case, cfg)
                        case["artifacts"][strategy] = _save_generated_trial(folder, strategy, trial)
                        case["strategies"][strategy] = evaluation_summary(_safe(trial))
                        if "template_contract" in trial:
                            case["strategies"][strategy]["template_contract"] = _safe(trial["template_contract"])
                cancelled = cancelled or generated.get("stopped_reason") == "cancelled" or bool(stop())
            values = list(case["strategies"].values())
            case["status"] = ("not_run" if values and all(t.get("status") == "not_run" for t in values)
                              else "execution_error" if any(not _execution_valid(t) and t.get("status") != "not_run" for t in values)
                              else "partial" if len(values) != len(STRATEGIES) or any(t.get("status") == "not_run" for t in values)
                              else "completed")
        except Exception as exc:
            partial = getattr(exc, "partial_result", None)
            if isinstance(partial, dict):
                _write_json(folder / "full-report.json", partial)
                case["artifacts"]["full_report"] = f"cases/{case['case_id']}/full-report.json"
                for strategy, key in (("baseline", "baseline"), ("verified", "fixed")):
                    if isinstance(partial.get(key), dict):
                        case["artifacts"][strategy] = _save_generated_trial(folder, strategy, partial[key])
                        case["strategies"][strategy] = evaluation_summary(_safe(partial[key]))
            case.update(status="execution_error", error=_label(exc))
        case["completed_at"] = _now()
        _write_json(folder / "case.json", case)
        case["artifacts"]["case_json"] = f"cases/{case['case_id']}/case.json"
        completed += 1
        publish()
    report.update(status="cancelled" if cancelled else "completed", completed=not cancelled,
                  cancelled=cancelled, finished_at=_now())
    report["artifacts"] = {"report": "suite-report.json", "summary_csv": "summary.csv", "zip": "suite-export.zip"}
    # Persist terminal metadata first, then create the ZIP, then notify the UI.
    callback = progress_callback
    progress_callback = None
    publish()
    export_suite(report, output)
    progress_callback = callback
    if callback:
        callback(public_plan(report))
    return public_plan(report)


def recover_suite(report, output_dir):
    """Make a stopped checkpoint exportable using saved data only.

    No parser, runner, uploaded SQL, or model is invoked during recovery. The
    engine-generated artifacts already present on disk remain unchanged.
    """
    recovered = public_plan(report)
    recovered.pop("items", None)
    recovered.update(kind="test_suite", workflow_version=WORKFLOW_VERSION, status="interrupted",
                     completed=False, cancelled=False, stopped_reason="interrupted", finished_at=_now())
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    folder_root = output / "cases"
    if output.is_symlink() or folder_root.is_symlink():
        raise ValueError("恢复输出目录不接受符号链接")
    folder_root.mkdir(exist_ok=True)
    for case in recovered["cases"]:
        preserve = case["status"] in {"duplicate", "conflict", "import_error"}
        if case["kind"] == "scenario" and not preserve:
            strategies = case.setdefault("strategies", {})
            for strategy in STRATEGIES:
                strategies.setdefault(strategy, {"strategy": strategy, "status": "not_run", "completed": False,
                    "stopped_reason": "interrupted", "actual_success": None, "reported_outcome": None,
                    "business_violations": [], "execution_violations": []})
            unrun = [trial for trial in strategies.values() if trial.get("status") == "not_run"]
            if unrun:
                case.update(status="not_run" if len(unrun) == len(STRATEGIES) else "partial", stopped_reason="interrupted")
        elif case["status"] == "ready" and not preserve:
            case.update(status="not_run", stopped_reason="interrupted")
        folder = folder_root / case["case_id"]
        if folder.is_symlink():
            raise ValueError("恢复用例目录不接受符号链接")
        folder.mkdir(exist_ok=True)
        case.setdefault("artifacts", {})["case_json"] = f"cases/{case['case_id']}/case.json"
        _write_json(folder / "case.json", case)
    _refresh(recovered)
    _result_totals(recovered)
    recovered["artifacts"] = {"report": "suite-report.json", "summary_csv": "summary.csv", "zip": "suite-export.zip"}
    export_suite(recovered, output)
    return public_plan(recovered)


CSV_FIELDS = ["workflow_version", "profile", "case_id", "kind", "name", "filename", "member", "source_index",
              "request_id", "source_sha256", "config_sha256", "stored_safe_sha256", "status", "duplicate_of", "strategy",
              "case_status", "execution_valid", "execution_scope", "actual_success", "business_failed", "business_violations",
              "reported_outcome", "report_assessment", "reporting_violations", "execution_violations", "static_verdict", "error", "skipped_stages"]


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    value = str(value)
    # Spreadsheet import can skip leading whitespace before interpreting formulae.
    return "'" + value if value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) else value


def export_suite(report, output_dir):
    """Write spreadsheet-safe CSV and a ZIP of local generated artifacts only."""
    output = Path(output_dir)
    rows = []
    for case in report["cases"]:
        origin = case.get("origin", {})
        base = {key: case.get(key) for key in CSV_FIELDS}
        base.update(workflow_version=WORKFLOW_VERSION, profile=report["profile"], filename=origin.get("filename"),
                    member=origin.get("member"), source_index=origin.get("index"),
                    skipped_stages=report.get("skipped_stages", []))
        strategies = list(STRATEGIES) if case["kind"] == "scenario" else [""]
        for strategy in strategies:
            row = {**base, "strategy": strategy, "case_status": case["status"]}
            if case["kind"] == "scenario":
                trial = case.get("strategies", {}).get(strategy)
                if trial is not None:
                    row.update({key: trial.get(key) for key in ("actual_success", "business_failed", "business_violations", "reported_outcome",
                                "report_assessment", "reporting_violations", "execution_violations")})
                    row["execution_valid"] = _execution_valid(trial)
                    row["status"] = trial.get("status") or ("completed" if _execution_valid(trial) else "execution_error")
                else:
                    row["execution_valid"] = False
            elif case["kind"] == "trace":
                analysis = case["analysis"]
                row.update({key: analysis.get(key) for key in ("business_failed", "business_violations", "reported_outcome", "report_assessment", "reporting_violations", "execution_violations")})
                # Uploaded final-state claims are not independently measured SQLite outcomes.
                row.update(actual_success=None, execution_valid=None, static_verdict=analysis.get("verdict"), execution_scope="static_import_only")
            rows.append(row)
    with (output / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: _cell(row.get(key)) for key in CSV_FIELDS} for row in rows)
    _write_json(output / "suite-report.json", report)
    allowed = [output / "suite-report.json", output / "summary.csv"]
    for case in report["cases"]:
        folder = output / "cases" / case["case_id"]
        if not folder.is_dir() or folder.is_symlink():
            continue
        allowed.extend(path for path in folder.iterdir() if path.name in {"case.json", "full-report.json", "analysis.json"} or
                       (path.suffix in {".sqlite", ".json"} and path.stem in STRATEGIES))
    with zipfile.ZipFile(output / "suite-export.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in allowed:
            if path.is_file() and not path.is_symlink():
                archive.write(path, path.relative_to(output).as_posix())
    return {"report": "suite-report.json", "summary_csv": "summary.csv", "zip": "suite-export.zip"}
