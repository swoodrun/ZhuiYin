"""Loopback-only prototype server; Python standard library only."""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import closing
import json
import secrets
from pathlib import Path
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import uuid
import zipfile

from engine import APP_VERSION, EVALUATION_PROTOCOL, IncompleteBaselineError, Config, FAULTS, MAX_MODEL_REQUESTS, DEFAULT_EXPERIMENT_REQUESTS, model_request, run_experiment
from concurrency_lab import run_concurrency_suite
from engine import agent_spec, MODEL_CONTEXT, MODEL_CONTEXT_FIELDS
from run_billing import RunBudget, summarize_usage, validate_billing
from ingest import parse_upload
from job_store import JobStore, valid_id, job_metadata, sanitize
from study import run_study, validate_study_options
from agent_suite import execute_agent_suite
from runtime_paths import data_root, desktop_identity
from task_templates import TemplateStore, TemplateConflictError
from experiment_compare import compare_jobs, reusable_config, retest_candidates
from diagnostics import analyze_report
from product_workflows import (PRODUCT_VERSION, execution_config, public_execution_config,
                               attach_template, prepare_retest)
from retest_templates import attach_retest_templates
from test_suite import (WORKFLOW_VERSION, prepare_uploads, save_prepared, load_prepared,
                        public_plan, execute_suite, recover_suite)

ROOT = Path(__file__).resolve().parent
DATA = data_root() / "runs"
DESKTOP_TOKEN = None
JOBS = {}
LOCK = threading.Lock()
STOP_EVENTS = {}
SAVE_LOCK = threading.Lock()
STORE_LOCK = threading.Lock()
STORES = {}
TEMPLATE_STORES = {}
STOP_MESSAGES = {"request_budget_exhausted": "模型请求预算不足；已保留执行记录和当前故障组合。",
                 "cost_budget_exhausted": "本次费用保护额度不足以预留后续请求；已有结果及未运行记录已保存。",
                 "campaign_budget_exhausted": "本轮实验累计请求或费用保护已停止调用；已有样本已保存。",
                 "cancelled": "实验已停止；已保留完成的执行记录。",
                 "model_execution_error": "模型调用或任务报告未完成；已有记录已保存。",
                 "unstable_initial_failure": "原始业务错误复现不稳定；故障精简未完成，已有记录已保存。"}
STOP_MESSAGES["unstable_final_failure"] = "精简后的错误未通过独立重复复查；已保留全部记录，精简标记为未完成。"

SUITE_REQUEST_LIMIT = 32 * 1024 * 1024


def check_model_with_billing(model_config, billing):
    """A connection check gets its own one-request allowance, never a worker's budget."""
    pricing = validate_billing(billing)
    guard = RunBudget(pricing) if pricing else None
    sentinel = object()
    previous = {key: getattr(MODEL_CONTEXT, key, sentinel) for key in MODEL_CONTEXT_FIELDS}
    records = []
    MODEL_CONTEXT.remaining = MODEL_CONTEXT.request_limit = 1
    MODEL_CONTEXT.cancel_check = None
    MODEL_CONTEXT.usage_records = records
    MODEL_CONTEXT.budget_exhausted = False
    MODEL_CONTEXT.campaign_stopped_reason = None
    MODEL_CONTEXT.cost_stopped_reason = None
    MODEL_CONTEXT.run_budget = guard
    try:
        answer = model_request(model_config, [{"role": "user", "content": "Reply with OK."}])
        result = {"ok": True, "message": "接口连接成功；工具调用兼容性将由实际实验验证",
                  "response": str(answer.get("content", ""))[:80]}
        if pricing:
            result["model_usage"] = summarize_usage(records, pricing)
            result["run_budget"] = guard.summary()
        return result
    finally:
        for key, value in previous.items():
            if value is sentinel:
                if hasattr(MODEL_CONTEXT, key):
                    delattr(MODEL_CONTEXT, key)
            else:
                setattr(MODEL_CONTEXT, key, value)


def suite_root():
    return DATA.parent / "test-suites"


def decode_suite_files(body):
    files = body.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 200:
        raise ValueError("请一次选择 1—200 个输入文件")
    decoded = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError("文件列表格式无效")
        if "content_base64" in item:
            encoded = item["content_base64"]
            if not isinstance(encoded, str) or "content" in item:
                raise ValueError("文件内容编码无效")
            try:
                value = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("文件内容编码无效") from None
            decoded.append({"filename": item["filename"], "data": value})
        else:
            value = item.get("content")
            if not isinstance(value, str):
                raise ValueError("文本文件内容无效")
            decoded.append({"filename": item["filename"], "content": value})
    return decoded


def suite_worker(run_id, plan, agent_config=None):
    """Run a prepared batch; private model settings remain in worker memory."""
    out = DATA / run_id

    def publish(report):
        snapshot = sanitize(report)
        snapshot["run_id"] = run_id
        artifacts = {}
        for key, name in (("bundle", "suite-export.zip"), ("summary_csv", "summary.csv")):
            if (out / name).is_file():
                artifacts[key] = f"/api/runs/{run_id}/{name}"
        snapshot["artifacts"] = artifacts
        # A durable, data-only report also survives loss of in-memory state.
        temporary = out / "report.json.tmp"
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(out / "report.json")
        progress = snapshot.get("progress", {})
        percent = progress.get("percent", 0)
        with LOCK:
            JOBS[run_id].update(phase="test_suite", progress=min(99, max(0, int(percent))), partial_result=snapshot)
        checkpoint(run_id)
        return snapshot

    try:
        out.mkdir(parents=True, exist_ok=True)
        if agent_config is None:
            report = execute_suite(plan, out, progress_callback=publish,
                                   should_stop=lambda: STOP_EVENTS[run_id].is_set())
        else:
            report = execute_agent_suite(plan, out, agent_config, progress_callback=publish,
                                         should_stop=lambda: STOP_EVENTS[run_id].is_set())
        report = publish(report)
        cancelled = bool(report.get("cancelled") or STOP_EVENTS[run_id].is_set())
        incomplete = cancelled or report.get("status") != "completed"
        with LOCK:
            JOBS[run_id].update(status="error" if incomplete else "done", phase="stopped" if incomplete else "done", progress=100, result=report)
            if incomplete:
                JOBS[run_id]["error"] = STOP_MESSAGES.get(report.get("stopped_reason"), "批量测试提前停止；已完成结果和未运行位置均已保留。")
    except Exception:
        traceback.print_exc()
        try:
            saved = JOBS[run_id].get("partial_result") or plan
            publish(recover_suite(saved, out))
        except Exception:
            traceback.print_exc()
        with LOCK:
            JOBS[run_id].update(status="error", phase="stopped", error="批量测试中断；已保存的阶段结果可从实验历史查看。详情见 data/server-error.log。")
    finally:
        if agent_config is not None:
            agent_config.model_config.clear()
        checkpoint(run_id)


def recover_test_suites():
    """Export interrupted suite checkpoints without rerunning any business step."""
    history = store()
    with closing(history.connect()) as database:
        ids = [row[0] for row in database.execute(
            "SELECT j.run_id FROM jobs j JOIN job_status s ON j.run_id=s.run_id "
            "WHERE j.kind='test_suite' AND s.status IN ('interrupted','error')")]
    recovered = 0
    for run_id in ids:
        job = history.get(run_id)
        report = job.get("result") or job.get("partial_result")
        if not report:
            try:
                report = load_prepared(suite_root(), job.get("config", {}).get("suite_id"))
            except (ValueError, OSError):
                continue
        if report.get("status") not in ("running", "prepared"):
            continue
        out = DATA / run_id
        try:
            report = recover_suite(report, out)
            report.update(run_id=run_id, artifacts={"bundle": f"/api/runs/{run_id}/suite-export.zip",
                                                   "summary_csv": f"/api/runs/{run_id}/summary.csv"})
            (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            job.pop("result", None)
            job.update(partial_result=report, result_revision=job.get("result_revision", 0) + 1)
            history.save(job)
            recovered += 1
        except Exception:
            traceback.print_exc()
    return recovered


def store():
    path = (DATA / "history.sqlite").resolve()
    with STORE_LOCK:
        if path not in STORES or not path.is_file():
            STORES[path] = JobStore(path)
        return STORES[path]


def template_store():
    path = (DATA.parent / 'task-templates.sqlite').resolve()
    with STORE_LOCK:
        if path not in TEMPLATE_STORES or not path.is_file():
            TEMPLATE_STORES[path] = TemplateStore(path)
        return TEMPLATE_STORES[path]


def checkpoint(run_id, payload_changed=True):
    with SAVE_LOCK:
        with LOCK:
            job = JOBS[run_id]
            if payload_changed:
                job["result_revision"] = job.get("result_revision", 0) + 1
            # Result/checkpoint objects are replaced as a unit and not mutated after publication.
            # Copy only the small job envelope and mutable status event list.
            snapshot = dict(job)
            snapshot["events"] = list(job.get("events", []))
        snapshot.setdefault("run_id", run_id)
        snapshot.setdefault("kind", "experiment")
        saved = store().save(snapshot, payload_changed=payload_changed)
        with LOCK:
            JOBS[run_id].update(created_at=saved["created_at"], updated_at=saved["updated_at"])


def get_job(run_id, lite=False):
    if not valid_id(run_id):
        return None
    with LOCK:
        current = JOBS.get(run_id)
        if current is not None:
            if lite:
                return job_metadata(current)
            job = dict(current)
            job["events"] = list(current.get("events", []))
            return sanitize(job)
    job = store().get(run_id, lite=lite)
    if job and not lite and job.get("status") != "running":
        # Full reads/downloads may recover the original report; never execute its SQL text.
        report_path = DATA / run_id / "report.json"
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(report, dict):
                    job["result" if job.get("has_result") else "partial_result"] = sanitize(report)
            except (ValueError, OSError, RecursionError):
                pass
    return job


def create_job(kind="experiment", config=None, label=None):
    with LOCK:
        if any(j["status"] == "running" for j in JOBS.values()):
            raise ValueError("已有实验运行中，请等待完成或先停止")
        run_id = uuid.uuid4().hex[:12]
        JOBS[run_id] = {"run_id": run_id, "kind": kind, "status": "running", "phase": "starting", "progress": 0, "events": [],
                        "config": config or {}, "label": label or {"study": "多轮统计", "batch": "批量场景检查"}.get(kind, "单次故障实验")}
        STOP_EVENTS[run_id] = threading.Event()
    try:
        checkpoint(run_id)
    except Exception:
        # No worker has started yet. A failed initial save must not leave a
        # phantom running job blocking every subsequent launch.
        with LOCK:
            JOBS.pop(run_id, None)
            STOP_EVENTS.pop(run_id, None)
        raise
    return run_id


def save_databases(result, out, run_id, prefix=""):
    """Only called with freshly executed engine results, never uploaded SQL."""
    artifacts = {}
    for name in ("baseline", "fixed"):
        if not result.get(name, {}).get("sqlite_dump"):
            continue
        with closing(sqlite3.connect(out / f"{prefix}{name}.sqlite")) as db:
            db.executescript(result[name]["sqlite_dump"])
        artifacts[name] = f"/api/runs/{run_id}/{prefix}{name}.sqlite"
    result["artifacts"] = artifacts
    for arm in result.get("ablation", {}).get("arms", []):
        strategy = arm["strategy"]
        if strategy not in ("idempotency_only", "verification_only") or not arm.get("result", {}).get("sqlite_dump"):
            continue
        name = f"{prefix}{strategy}.sqlite"
        with closing(sqlite3.connect(out / name)) as db:
            db.executescript(arm["result"]["sqlite_dump"])
        arm["result"]["artifacts"] = {"database": f"/api/runs/{run_id}/{name}"}


def validate_batch(body):
    scenarios = body.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 20:
        raise ValueError("批量验证需要 1—20 个场景")
    entries, ids = [], set()
    allowed = {"equipment", "stock", "quantity", "request_id", "seed", "faults", "agent_mode", "scenario", "ticket_title", "ticket_priority", "max_requests", "prompt_policy"}
    for i, item in enumerate(scenarios, 1):
        if not isinstance(item, dict) or not isinstance(item.get("config"), dict):
            raise ValueError(f"第 {i} 个场景缺少 config 参数")
        raw = item["config"]
        if set(raw) - allowed or raw.get("agent_mode", "rules") != "rules":
            raise ValueError(f"第 {i} 个场景含不支持的控制参数；批量验证只使用离线规则策略")
        try:
            config = Config.from_dict(raw)
        except ValueError as exc:
            raise ValueError(f"第 {i} 个场景：{exc}") from None
        if config.request_id in ids:
            raise ValueError(f"第 {i} 个场景的请求编号重复，请为每个独立实验设置不同编号")
        ids.add(config.request_id)
        name = item.get("name", f"场景 {i}")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ValueError(f"第 {i} 个场景的名称需要 1—200 个字符")
        entries.append({"name": name.strip(), "config": config})
    source = body.get("source") or {}
    if not isinstance(source, dict):
        raise ValueError("来源信息格式无效")
    filename = source.get("filename", "用户自定义场景")
    sha = source.get("sha256", "")
    if not isinstance(filename, str) or len(filename) > 255:
        raise ValueError("来源文件名过长或无效")
    if not isinstance(sha, str) or (sha and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha))):
        raise ValueError("来源文件标识无效")
    return entries, {"filename": filename.replace("\\", "/").rsplit("/", 1)[-1], "sha256": sha}


def batch_worker(run_id, entries, source):
    cases = []
    out = DATA / run_id
    try:
        out.mkdir(parents=True, exist_ok=True)
        total = len(entries)
        for index, entry in enumerate(entries):
            config = entry["config"]
            def update(phase, progress, message):
                with LOCK:
                    if STOP_EVENTS[run_id].is_set():
                        raise RuntimeError("用户已停止批量验证；已完成的场景仍可查看")
                    JOBS[run_id].update(phase="batch", progress=int((index + progress / 100) / total * 95))
                    JOBS[run_id]["events"].append({"time": time.strftime("%H:%M:%S"), "message": f"[{index + 1}/{total}] {entry['name']}：{message}"})
                checkpoint(run_id, payload_changed=False)
            result = run_experiment(config, update, lambda: STOP_EVENTS[run_id].is_set())
            save_databases(result, out, run_id, f"case-{index + 1:02d}-")
            cases.append({"name": entry["name"], "config": config.public(), "baseline_failed": result["baseline"]["failed"],
                          "fixed_failed": result["fixed"]["failed"], "minimized_faults": result["minimized_faults"], "result": result})
            with LOCK:
                JOBS[run_id]["partial_result"] = {"kind": "batch", "cases": list(cases), "completed": len(cases), "total": total, "source": source,
                    "summary": {"baseline_failures": sum(c["baseline_failed"] for c in cases), "fixed_failures": sum(c["fixed_failed"] for c in cases)},
                    "limitations": ["运行中的已保存结果，包含已完成场景。"]}
            checkpoint(run_id)
        report = {"kind": "batch", "total": total, "completed": len(cases), "cases": cases, "source": source,
                  "summary": {"baseline_failures": sum(c["baseline_failed"] for c in cases),
                              "fixed_failures": sum(c["fixed_failed"] for c in cases)},
                  "limitations": ["每个场景独立运行，库存互不影响。", "批量检查使用离线规则模式；导入文件不会调用模型。"],
                  "artifacts": {"bundle": f"/api/runs/{run_id}/artifacts.zip"}}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with zipfile.ZipFile(out / "artifacts.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
            for file in sorted(out.iterdir()):
                if file.suffix in (".json", ".sqlite"):
                    bundle.write(file, file.name)
        with LOCK:
            JOBS[run_id].update(status="done", phase="done", progress=100, result=report)
    except Exception:
        traceback.print_exc()
        cancelled = STOP_EVENTS[run_id].is_set()
        message = ("批量运行已停止；已保留完成的场景。" if cancelled else
                   "批量实验运行中断；已保留完成的场景，详情见 data/server-error.log。")
        partial = {"kind": "batch", "cases": cases, "completed": len(cases), "total": len(entries), "source": source,
                   "summary": {"baseline_failures": sum(c["baseline_failed"] for c in cases),
                               "fixed_failures": sum(c["fixed_failed"] for c in cases)},
                   "cancelled": cancelled, "error": message,
                   "limitations": ["批量运行已停止，这里保留已完成的场景。", "批量检查使用离线规则模式。"]}
        try:
            if out.is_dir():
                (out / "report.json").write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            traceback.print_exc()
        with LOCK:
            JOBS[run_id].update(status="error", error=message, partial_result=partial)
    finally:
        for entry in entries:
            entry["config"].model_config.clear()
        checkpoint(run_id)


def worker(run_id, config, agent_only=False):
    def update(phase, progress, message):
        with LOCK:
            job = JOBS[run_id]
            job.update(phase=phase, progress=progress)
            job["events"].append({"time": time.strftime("%H:%M:%S"), "message": message})
        checkpoint(run_id, payload_changed=False)
    try:
        if agent_only:
            result = run_experiment(config, update, lambda: STOP_EVENTS[run_id].is_set(), include_rule_checks=False)
        else:
            result = run_experiment(config, update, lambda: STOP_EVENTS[run_id].is_set())
        result = attach_template(result, config)
        out = DATA / run_id
        out.mkdir(parents=True, exist_ok=True)
        save_databases(result, out, run_id)
        (out / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with LOCK:
            complete = result.get("status", "finished") == "finished"
            JOBS[run_id].update(status="done" if complete else "error", phase="done" if complete else "stopped", progress=100, result=result)
            if not complete:
                JOBS[run_id].update(partial_result=result, error=STOP_MESSAGES.get(result.get("stopped_reason"), "实验未全部完成；已保留已验证结果。"))
    except IncompleteBaselineError as exc:
        # This object was produced by our engine, never by an uploaded report.
        # Preserve the attempted baseline, usage and DB even without a valid
        # business counterexample; do not continue calling the model.
        result = attach_template(exc.partial_result, config)
        out = DATA / run_id
        out.mkdir(parents=True, exist_ok=True)
        save_databases(result, out, run_id)
        (out / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with LOCK:
            JOBS[run_id].update(status="error", phase="stopped", progress=100, partial_result=result,
                error=STOP_MESSAGES.get(result.get("stopped_reason"), "主试验未完成；已保留执行记录与用量。"))
    except Exception:
        traceback.print_exc()
        message = (STOP_MESSAGES["cancelled"] if STOP_EVENTS[run_id].is_set() else
                   "实验运行中断；详情见 data/server-error.log。")
        with LOCK:
            JOBS[run_id].update(status="error", error=message)
    finally:
        # Config and API key are never attached to the job or written to reports.
        config.model_config.clear()
        checkpoint(run_id)


def study_worker(run_id, config, options):
    def update(phase, progress, message):
        with LOCK:
            JOBS[run_id].update(phase=phase, progress=progress)
            JOBS[run_id]["events"].append({"time": time.strftime("%H:%M:%S"), "message": message})
        checkpoint(run_id, payload_changed=False)

    def save_partial(report):
        report = attach_template(report, config)
        with LOCK:
            JOBS[run_id]["partial_result"] = report
        checkpoint(run_id)

    try:
        report = attach_template(run_study(config, options, on_progress=update, cancel_check=lambda: STOP_EVENTS[run_id].is_set(), on_checkpoint=save_partial), config)
        out = DATA / run_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        complete = report.get("status") == "finished"
        with LOCK:
            JOBS[run_id].update(status="done" if complete else "error", phase="done" if complete else "stopped", progress=100,
                                result=report, **({} if complete else {"error": STOP_MESSAGES.get(report.get("stopped_reason"), "多轮实验未全部完成；已有样本已保存。 ")}))
    except Exception:
        traceback.print_exc()
        with LOCK:
            JOBS[run_id].update(status="error", error="多轮实验未完成；已保存最后检查点。")
    finally:
        config.model_config.clear()
        checkpoint(run_id)


def concurrency_worker(run_id, config):
    def with_downloads(report):
        return {**report, "cases": [{**case, "database_url": f"/api/runs/{run_id}/{case['database_filename']}"}
                                    for case in report["cases"]]}

    def update(phase, progress, message):
        with LOCK:
            JOBS[run_id].update(phase=phase, progress=progress)
            JOBS[run_id]["events"].append({"time": time.strftime("%H:%M:%S"), "message": message})
        checkpoint(run_id, payload_changed=False)

    def save_partial(report):
        with LOCK:
            JOBS[run_id]["partial_result"] = with_downloads(report)
        checkpoint(run_id)

    try:
        out = DATA / run_id
        out.mkdir(parents=True, exist_ok=True)
        report = run_concurrency_suite(config, out, on_progress=update,
            cancel_check=lambda: STOP_EVENTS[run_id].is_set(), on_checkpoint=save_partial)
        report = sanitize(with_downloads(report))
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        complete = report["status"] == "finished"
        with LOCK:
            JOBS[run_id].update(status="done" if complete else "error", phase="done" if complete else "stopped",
                progress=100, result=report, **({} if complete else {"error": "并发对照已停止，已保存完成的组合。"}))
    except Exception:
        traceback.print_exc()
        with LOCK:
            JOBS[run_id].update(status="error", error="并发对照运行中断；已保存的结果可在历史中查看。")
    finally:
        config.model_config.clear()
        checkpoint(run_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "Zhuiyin/" + APP_VERSION

    def log_message(self, *_):
        pass

    def allowed(self):
        host = self.headers.get("Host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")

    def send_bytes(self, status, body, mime="application/json; charset=utf-8", filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, value):
        self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def read_json(self, limit=32768):
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("请求需要使用 JSON 格式")
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ValueError("请求长度无效") from None
        if not 0 < size <= limit:
            raise ValueError("请求为空或超过此接口大小上限")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("请求需要是 JSON 对象")
        return value

    def do_GET(self):
        if not self.allowed():
            return self.json(403, {"error": "仅允许本机访问"})
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.json(200, {"ok": True, "version": APP_VERSION, "desktop": desktop_identity(), "default_mode": "rules", "model_available": False,
                                   "evaluation_protocol": EVALUATION_PROTOCOL, "prompt_policies": ["neutral", "recovery"],
                                   "faults": FAULTS, "model_request_limit": MAX_MODEL_REQUESTS, "model_request_default": DEFAULT_EXPERIMENT_REQUESTS,
                                   "study_request_limit": MAX_MODEL_REQUESTS, "history": True, "lite_polling": True, "concurrency": True,
                                   "uploads": True, "upload_limit": 2097152, "agent_workbench": "agent-ui-v1",
                                   "workbench_billing": "run-billing-v1", "product_workbench": PRODUCT_VERSION,
                                   "diagnostics": True, "history_comparison": True, "task_templates": True,
                                   "test_suites": True, "agent_batch": "agent-batch-v1", "test_workflow_version": WORKFLOW_VERSION,
                                   "test_suite_limits": {"files": 200, "cases": 500, "request_bytes": SUITE_REQUEST_LIMIT,
                                                         "zip_bytes": 20 * 1024 * 1024, "expanded_bytes": 32 * 1024 * 1024}})
        if path == "/api/history":
            try:
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                limit = int(query.get("limit", ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
                if offset < 0:
                    raise ValueError
            except ValueError:
                return self.json(400, {"error": "历史条数应为整数，偏移量应为非负整数"})
            return self.json(200, store().list(limit, offset))
        if path == '/api/task-templates':
            return self.json(200, {'templates': template_store().list()})
        if path.startswith('/api/task-templates/'):
            try:
                template = template_store().get(path.rsplit('/', 1)[-1])
            except ValueError as exc:
                return self.json(400, {'error': str(exc)[:400]})
            return self.json(200, template) if template else self.json(404, {'error': '任务模板不存在。'})
        if path.startswith("/api/templates/"):
            name = path.rsplit("/", 1)[-1]
            if name not in ("scenarios.csv", "scenario.json", "ticket.json", "trace.jsonl", "trace.json",
                            "batch-example.zip", "batch-equipment.csv", "batch-tickets.csv"):
                return self.json(404, {"error": "模板不存在"})
            file = ROOT / "templates" / name
            if not file.is_file():
                return self.json(503, {"error": "模板尚未准备好"})
            mime = ("application/zip" if name.endswith(".zip") else
                    "text/csv; charset=utf-8" if name.endswith(".csv") else "application/json; charset=utf-8")
            return self.send_bytes(200, file.read_bytes(), mime, name)
        if path.startswith("/api/runs/"):
            parts = path.strip("/").split("/")
            if len(parts) not in (3, 4):
                return self.json(404, {"error": "实验不存在"})
            run_id = parts[2]
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            lite = query.get("lite", ["0"])[0]
            if lite not in ("0", "1"):
                return self.json(400, {"error": "lite 仅支持 0 或 1"})
            job = get_job(run_id, lite=len(parts) == 3 and lite == "1")
            if not job:
                return self.json(404, {"error": "实验不存在"})
            if len(parts) == 3:
                return self.json(200, job)
            name = parts[3]
            if name == 'retest-plan':
                return self.json(200, {'run_id': run_id, 'config': reusable_config(job),
                                       'candidates': retest_candidates(job)})
            if name == 'diagnostics':
                return self.json(200, analyze_report(job.get('result') or job.get('partial_result') or {}))
            if job["status"] != "done" and not job.get("partial_result"):
                return self.json(409, {"error": "实验尚未完成"})
            if name == "export":
                return self.send_bytes(200, json.dumps(sanitize(job.get("result") or job["partial_result"]), ensure_ascii=False, indent=2).encode(), filename=f"zhuiyin-{run_id}.json")
            if job.get("kind") == "test_suite" and name in ("suite-export.zip", "summary.csv"):
                file = DATA / run_id / name
                if not file.is_file():
                    return self.json(404, {"error": "结果文件尚未生成"})
                mime = "application/zip" if name.endswith(".zip") else "text/csv; charset=utf-8"
                return self.send_bytes(200, file.read_bytes(), mime, name)
            strategies = ("baseline", "fixed", "idempotency_only", "verification_only")
            database_names = {f"{s}.sqlite" for s in strategies} | {f"case-{i:02d}-{s}.sqlite" for i in range(1, 21) for s in strategies}
            database_names |= {f"{schedule}-{strategy}.sqlite" for schedule in ("sequential", "concurrent") for strategy in ("baseline", "idempotency_only", "verification_only", "verified")}
            if name in database_names:
                if name.startswith(("sequential-", "concurrent-")):
                    report = job.get("result") or job.get("partial_result") or {}
                    completed_files = {case.get("database_filename") for case in report.get("cases", []) if isinstance(case, dict)}
                    if name not in completed_files:
                        return self.json(404, {"error": "该组合尚未完成，数据库暂不提供下载"})
                file = DATA / run_id / name
                if not file.is_file():
                    return self.json(404, {"error": "数据库文件不存在"})
                return self.send_bytes(200, file.read_bytes(), "application/vnd.sqlite3", name)
            if name == "artifacts.zip":
                file = DATA / run_id / name
                if file.is_file():
                    return self.send_bytes(200, file.read_bytes(), "application/zip", name)
            return self.json(404, {"error": "下载内容不存在"})
        files = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js", "/uploads.js": "uploads.js", "/research.js": "research.js", "/ui.js": "ui.js", "/concurrency.js": "concurrency.js", "/outcome.js": "outcome.js", "/testcenter.js": "testcenter.js", "/testcenter.css": "testcenter.css", "/agentui.js": "agentui.js", "/history.js": "history.js"}
        files.update({"/assets/zhuiyin-logo.png": "assets/zhuiyin-logo.png", "/assets/favicon.png": "assets/favicon.png", "/assets/favicon.ico": "assets/favicon.ico"})
        files.update({'/' + name: name for name in ('diagnostics.js', 'diagnostics.css',
            'experiment_compare.js', 'experiment_compare.css', 'task_templates.js', 'task_templates.css',
            'product_workflows.js', 'product_workflows.css')})
        if path in files:
            file = ROOT / "static" / files[path]
            if not file.is_file():
                return self.json(503, {"error": "界面正在准备，请稍后刷新"})
            mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon"}[file.suffix]
            return self.send_bytes(200, file.read_bytes(), mime)
        return self.json(404, {"error": "页面不存在"})

    def do_POST(self):
        if not self.allowed():
            return self.json(403, {"error": "不允许跨站请求"})
        path = urlsplit(self.path).path
        try:
            limit = SUITE_REQUEST_LIMIT if path == "/api/test-suites/preview" else 4 * 1024 * 1024 if path == "/api/import" else 65536
            body = self.read_json(limit=limit)
            if path == '/api/task-templates':
                return self.json(201, template_store().create(body))
            if path == '/api/compare':
                if set(body) != {'left_run_id', 'right_run_id'}:
                    raise ValueError('请选择两份历史记录。')
                left, right = (get_job(body[key]) for key in ('left_run_id', 'right_run_id'))
                if not left or not right:
                    return self.json(404, {'error': '所选历史记录不存在。'})
                return self.json(200, compare_jobs(left, right))
            if path == '/api/retest/prepare':
                if set(body) != {'run_id', 'candidate_ids'}:
                    raise ValueError('重测预览只接收来源记录和选中的任务。')
                source = get_job(body.get('run_id'))
                if not source:
                    return self.json(404, {'error': '来源历史记录不存在。'})
                files, lineage = prepare_retest(source, body['candidate_ids'], retest_candidates(source))
                plan = prepare_uploads(files)
                plan = attach_retest_templates(plan, body['candidate_ids'], retest_candidates(source))
                plan['retest_source'] = lineage
                suite_id = save_prepared(suite_root(), plan)
                preview = public_plan(plan)
                preview.update(suite_id=suite_id, summary=plan.get('counts', {}), items=preview.get('cases', []), retest_source=lineage)
                return self.json(200, preview)
            if path == '/api/desktop/stop':
                token = body.get('token')
                if not DESKTOP_TOKEN or not isinstance(token, str) or not secrets.compare_digest(token, DESKTOP_TOKEN):
                    return self.json(403, {'error': '本机服务退出凭据无效'})
                with LOCK:
                    if any(job.get('status') == 'running' for job in JOBS.values()):
                        return self.json(409, {'error': '仍有实验运行，请先停止或等待完成'})
                self.json(200, {'message': '追因本机服务已退出，实验历史已保留。'})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/agent-preview":
                raw = body.get("config", {})
                if not isinstance(raw, dict) or "model_config" in raw:
                    raise ValueError("预览只接收业务参数，不接收密钥或模型连接配置")
                config = Config.from_dict({**raw, "agent_mode": "rules"})
                return self.json(200, {"name": "追因内置" + ("工单创建" if config.scenario == "ticket" else "设备借用") + " Agent",
                    "framework": "模型＋追因工具执行循环", "variants": {s: agent_spec(config, s) for s in ("baseline", "verified")},
                    "scope": "业务字段由服务端填写；模型决定工具选择、恢复行动与最终报告。非外部任意 Agent 接入。"})
            if path == "/api/run-agent":
                config = execution_config(body)
                if config.agent_mode != "llm":
                    raise ValueError("Agent 工作台需要真实模型；规则检查请进入使用方法")
                run_id = create_job(config=public_execution_config(config), label="Agent 实验 · " + ("工单创建" if config.scenario == "ticket" else "设备借用"))
                threading.Thread(target=worker, args=(run_id, config, True), daemon=True).start()
                return self.json(202, {"run_id": run_id})
            if path == "/api/test-suites/preview":
                if set(body) - {"files"}:
                    raise ValueError("批量导入只接收文件；不接收模型密钥或运行控制参数")
                plan = prepare_uploads(decode_suite_files(body))
                suite_id = save_prepared(suite_root(), plan)
                preview = public_plan(plan)
                preview.update(suite_id=suite_id, summary=plan.get("counts", {}), items=preview.get("cases", []))
                return self.json(200, preview)
            if path == "/api/test-suites/start":
                if set(body) - {"suite_id", "profile", "agent_config"}:
                    raise ValueError("批量测试只接收已预览编号和测试方式")
                profile = body.get("profile", "quick")
                if profile not in ("quick", "full", "agent"):
                    raise ValueError("测试方式仅支持 agent、quick 或 full")
                agent_config = None
                if profile == "agent":
                    raw = body.get("agent_config")
                    if not isinstance(raw, dict):
                        raise ValueError("请先在工作台配置模型连接")
                    agent_config = Config.from_dict({**raw, "agent_mode": "llm"})
                elif "agent_config" in body:
                    raise ValueError("规则检查不接收模型连接")
                plan = load_prepared(suite_root(), body.get("suite_id"))
                plan["profile"] = profile
                job_config = {"profile": profile, "suite_id": body["suite_id"], "agent_mode": "llm" if profile == "agent" else "rules"}
                if agent_config is not None:
                    job_config["max_requests"] = agent_config.max_requests
                    if agent_config.billing:
                        job_config["billing"] = dict(agent_config.billing)
                run_id = create_job("test_suite", job_config, "批量测试 · " + ("真实 Agent" if profile == "agent" else "快速规则检查" if profile == "quick" else "完整规则检查"))
                threading.Thread(target=suite_worker, args=(run_id, plan, agent_config) if agent_config else (run_id, plan), daemon=True).start()
                return self.json(202, {"run_id": run_id, "workflow_version": WORKFLOW_VERSION})
            if path == "/api/import":
                parsed = parse_upload(body.get("filename", ""), body.get("content", ""))
                import_id = uuid.uuid4().hex[:12]
                parsed["import_id"] = import_id
                parsed["export_url"] = f"/api/runs/{import_id}/export"
                with LOCK:
                    JOBS[import_id] = {"run_id": import_id, "kind": "import", "status": "done", "phase": "imported", "progress": 100, "events": [], "result": parsed,
                                       "label": "导入 · " + parsed.get("source", {}).get("filename", "用户数据")[:200]}
                out = DATA / import_id
                out.mkdir(parents=True, exist_ok=True)
                (out / "report.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
                checkpoint(import_id)
                return self.json(200, parsed)
            if path == "/api/run-batch":
                entries, source = validate_batch(body)
                run_id = create_job("batch", label="批量 · " + source["filename"][:200])
                threading.Thread(target=batch_worker, args=(run_id, entries, source), daemon=True).start()
                return self.json(202, {"run_id": run_id})
            if path == "/api/run":
                config = execution_config(body)
                run_id = create_job(config=public_execution_config(config), label="工单创建" if config.scenario == "ticket" else "设备借用")
                threading.Thread(target=worker, args=(run_id, config), daemon=True).start()
                return self.json(202, {"run_id": run_id})
            if path == "/api/study":
                config = execution_config(body.get("config", {}))
                options = validate_study_options(body.get("options", {}))
                run_id = create_job("study", public_execution_config(config), "多轮统计 · " + ("模型模式" if config.agent_mode == "llm" else "规则模式"))
                threading.Thread(target=study_worker, args=(run_id, config, options), daemon=True).start()
                return self.json(202, {"run_id": run_id})
            if path == "/api/concurrency":
                raw = body.get("config", body)
                if not isinstance(raw, dict):
                    raise ValueError("并发对照需要场景参数")
                if raw.get("model_config") or raw.get("agent_mode", "rules") != "rules":
                    raise ValueError("并发对照只使用本机规则模式")
                config = Config.from_dict({**raw, "faults": []})
                run_id = create_job("concurrency", config.public(), "并发重复提交 · " + ("工单" if config.scenario == "ticket" else "设备"))
                threading.Thread(target=concurrency_worker, args=(run_id, config), daemon=True).start()
                return self.json(202, {"run_id": run_id})
            if path == "/api/check-model":
                config = body.get("model_config", body)
                return self.json(200, check_model_with_billing(config, body.get("billing")))
            if path.startswith("/api/runs/") and path.endswith("/cancel"):
                run_id = path.split("/")[3]
                with LOCK:
                    stop = STOP_EVENTS.get(run_id)
                    if stop is not None:
                        stop.set()
                if stop is None:
                    return self.json(404, {"error": "实验不存在"})
                return self.json(200, {"ok": True})
            return self.json(404, {"error": "接口不存在"})
        except (ValueError, TypeError) as exc:
            return self.json(400, {"error": str(exc)[:400]})
        except Exception as exc:
            if path.startswith('/api/task-templates') or path in ('/api/compare', '/api/retest/prepare'):
                traceback.print_exc()
                return self.json(502, {'error': '操作暂时无法完成，已有实验记录未更改，请稍后重试。'})
            if path.startswith("/api/test-suites/"):
                traceback.print_exc()
                return self.json(502, {"error": "批量测试服务暂时无法完成请求，详情见 data/server-error.log。"})
            return self.json(502, {"error": str(exc)[:400]})

    def template_mutation(self, delete=False):
        if not self.allowed():
            return self.json(403, {'error': '不允许跨站请求。'})
        path = urlsplit(self.path).path
        if not path.startswith('/api/task-templates/') or path.count('/') != 3:
            return self.json(404, {'error': '接口不存在。'})
        try:
            body = self.read_json(limit=65536)
            template_id = path.rsplit('/', 1)[-1]
            if not template_store().get(template_id):
                return self.json(404, {'error': '任务模板不存在。'})
            expected = body.pop('expected_version', None)
            if delete:
                if body:
                    raise ValueError('删除模板只接收版本号。')
                template_store().delete(template_id, expected_version=expected)
                return self.json(200, {'ok': True})
            return self.json(200, template_store().update(template_id, body, expected_version=expected))
        except TemplateConflictError as exc:
            return self.json(409, {'error': str(exc)[:400]})
        except (ValueError, TypeError) as exc:
            return self.json(400, {'error': str(exc)[:400]})
        except Exception:
            traceback.print_exc()
            return self.json(502, {'error': '任务模板暂时无法保存，请稍后重试。'})

    def do_PUT(self):
        return self.template_mutation()

    def do_DELETE(self):
        return self.template_mutation(delete=True)


def main(on_ready=None):
    parser = argparse.ArgumentParser(description="追因本地可行性验证初版")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    history = store()
    history.import_legacy_reports(DATA)
    history.recover_interrupted()
    recover_test_suites()
    if on_ready:
        on_ready()
    print(f"Zhuiyin ready: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
