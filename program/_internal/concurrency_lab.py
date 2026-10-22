"""Two-client re-entry controls backed by real, shared SQLite transactions.

This is an arranged local interleaving, not a distributed-system simulator.
The existing Lab transaction, capability flags and database judge determine
every outcome; no strategy is assigned an expected result here.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import threading
import time
import traceback

from engine import Config, EVALUATION_PROTOCOL, Lab, STRATEGIES, report_provenance


SCHEDULES = {"sequential": "先后重复提交", "concurrent": "两客户端同时提交"}
CASE_TIMEOUT = 8.0
BARRIER_TIMEOUT = 5.0
DATABASE_TIMEOUT = 2.0
INTERNAL_ERROR = "并发检查运行中断；已保留完成的用例，详情见 data/server-error.log。"
LIMITATIONS = [
    "本组使用规则模式，两个客户端处理同一请求编号；不调用模型，也不叠加页面选择的其他故障。",
    "先后提交让第二个客户端读到第一条已提交记录。并发提交安排两个客户端先读到空记录，再分别写入，全部写完后才检查和报告。",
    "每组使用独立的本机 SQLite 文件；两个客户端各有连接，写入使用 BEGIN IMMEDIATE 事务、WAL 和限时锁等待。",
    "独立读取能发现重复记录；写入时的原子去重由幂等事务提供。结果取决于实际库存与记录，不保证任一策略始终成功。",
    "这组安排好的本机交错用于检验重复提交，不代表真实分布式故障、任意并发安排或真实模型的可靠性。",
    "停止时只统计完整用例；正在执行的用例可能留下本地数据库，但不计作通过或失败。",
]


class _Cancelled(Exception):
    pass


class _Timeline:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.perf_counter()
        self.events = []

    def append(self, client_id, event):
        with self.lock:
            item = copy.deepcopy(event)
            item.update(client_id=client_id, client_step=item["step"],
                        step=len(self.events) + 1, sequence=len(self.events) + 1,
                        elapsed_ms=round((time.perf_counter() - self.started) * 1000, 3))
            self.events.append(item)


class _ActorLab(Lab):
    def __init__(self, config, strategy, filename, client_id, schedule, timeline):
        super().__init__(config, [], strategy)
        self.db.close()
        self.db = _connect(filename)
        self.client_id, self.schedule, self.timeline = client_id, schedule, timeline
        self.write_attempts = 0

    def event(self, *args, **kwargs):
        super().event(*args, **kwargs)
        self.timeline.append(self.client_id, self.events[-1])

    def reserve(self):
        self.write_attempts += 1
        return super().reserve()


def _connect(filename):
    # Each actor opens its own connection inside its execution thread. Reads
    # stay outside a transaction; Lab.reserve owns the real write transaction.
    database = sqlite3.connect(filename, timeout=DATABASE_TIMEOUT, isolation_level=None)
    try:
        database.row_factory = sqlite3.Row
        database.execute(f"PRAGMA busy_timeout={int(DATABASE_TIMEOUT * 1000)}")
    except Exception:
        database.close()
        raise
    return database


def _initialize(config, strategy, filename):
    if filename.exists():
        raise FileExistsError("Refusing to replace an existing concurrency database")
    source = Lab(config, [], strategy)
    target = None
    try:
        target = _connect(filename)
        source.db.backup(target)
        target.execute("PRAGMA journal_mode=WAL")
    finally:
        source.db.close()
        if target is not None:
            target.close()


def _run_case(config, schedule, strategy, filename, cancel_check):
    _initialize(config, strategy, filename)
    timeline = _Timeline()
    stopped = threading.Event()
    barriers = [threading.Barrier(2), threading.Barrier(2)] if schedule == "concurrent" else []
    state_lock = threading.Lock()
    actors, client_results, errors = {}, {}, []

    def cancel_requested():
        return bool(cancel_check and cancel_check())

    def check_stop():
        if stopped.is_set() or cancel_requested():
            raise _Cancelled()

    def release_waiters():
        stopped.set()
        for barrier in barriers:
            barrier.abort()

    def wait_at(index):
        check_stop()
        try:
            barriers[index].wait(timeout=BARRIER_TIMEOUT)
        except threading.BrokenBarrierError:
            check_stop()
            raise
        check_stop()

    def actor(client_id):
        lab = None
        try:
            check_stop()
            lab = _ActorLab(config, strategy, filename, client_id, schedule, timeline)
            with state_lock:
                actors[client_id] = lab
            found = lab.query()[lab.record_key]
            initial_records = copy.deepcopy(found)
            if barriers:
                wait_at(0)
            check_stop()
            receipt = None
            if not found:
                receipt = lab.reserve()
            # Both commits must be visible before either client verifies or
            # reports. Atomic request-key de-duplication still happens inside
            # the existing Lab.reserve transaction, not at this barrier.
            if barriers:
                wait_at(1)
            check_stop()
            if lab.verification:
                verified = lab.query(strong=True)[lab.record_key]
                success = lab.healthy_records(verified)
                reason = "独立读取两次提交后的记录，核对是否恰好一条且业务字段正确"
            else:
                success = bool(found) if found else bool(receipt and receipt.get("ok"))
                reason = "根据首次查询和写入回执报告；未启用独立读取检查"
            check_stop()
            lab.finish(success, reason)
            with state_lock:
                client_results[client_id] = {
                    "client_id": client_id, "initial_records": initial_records,
                    "initial_record_count": len(initial_records), "receipt": receipt,
                    "reported_success": lab.reported, "reported_outcome": lab.reported_outcome,
                    "stats": {"tool_calls": lab.calls, "write_attempts": lab.write_attempts,
                              "physical_writes": lab.writes, "idempotency_hits": lab.idempotency_hits,
                              "verification_calls": lab.verification_calls},
                }
        except _Cancelled:
            with state_lock:
                errors.append("cancelled")
            release_waiters()
        except Exception:
            # Provider/user fields can appear in exceptions. Only local
            # diagnostics retain details; callers receive a fixed error.
            traceback.print_exc()
            with state_lock:
                errors.append("error")
            release_waiters()
        finally:
            if lab is not None:
                lab.db.close()

    if schedule == "sequential":
        for client_id in ("client-1", "client-2"):
            actor(client_id)
            if errors:
                break
    else:
        workers = [threading.Thread(target=actor, args=(client_id,), daemon=True,
                                    name=f"zhuiyin-{schedule}-{strategy}-{client_id}")
                   for client_id in ("client-1", "client-2")]
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + CASE_TIMEOUT
        timed_out = False
        while any(worker.is_alive() for worker in workers):
            if cancel_requested():
                release_waiters()
            if time.monotonic() >= deadline:
                timed_out = True
                release_waiters()
                # sqlite3.interrupt is expressly safe from another thread.
                with state_lock:
                    for lab in actors.values():
                        try:
                            lab.db.interrupt()
                        except sqlite3.Error:
                            pass
                break
            for worker in workers:
                worker.join(timeout=0.02)
        if timed_out:
            for worker in workers:
                worker.join(timeout=DATABASE_TIMEOUT + 0.2)
            traceback.print_exception(TimeoutError("Local concurrency case exceeded its time limit"))
            raise RuntimeError(INTERNAL_ERROR)
    if "error" in errors:
        raise RuntimeError(INTERNAL_ERROR)
    if errors or cancel_requested() or len(client_results) != 2:
        raise _Cancelled()

    # Reuse the database judge for each client's claim. Combining its existing
    # violation codes also catches a false report by either client; an aggregate
    # boolean alone could hide one client's incorrect claim.
    judge = Lab(config, [], strategy)
    judge.db.close()
    judge.db = _connect(filename)
    clients, violations, database_result = [], {}, None
    categorized = {key: {} for key in ("business_violations", "reporting_violations", "execution_violations")}
    try:
        for client_id in ("client-1", "client-2"):
            client = client_results[client_id]
            # Restore this client's exact report state. An explicit unknown is
            # completed; an absent report stays absent. Setting only the legacy
            # bool would leave the v2 judge without any recorded conclusion.
            judge.reported = client["reported_success"]
            judge.reported_outcome = client["reported_outcome"]
            checked = judge.result("rules")
            database_result = checked
            client.update({key: checked[key] for key in
                           ("evaluation_protocol", "completed", "report_assessment", "business_failed", "report_failed",
                            "business_violations", "reporting_violations", "execution_violations",
                            "failed", "failure_fingerprint")})
            clients.append(client)
            for violation in checked["violations"]:
                if violation["code"] not in violations:
                    violations[violation["code"]] = {**violation, "client_ids": []}
                violations[violation["code"]]["client_ids"].append(client_id)
            for key, group in categorized.items():
                for violation in checked[key]:
                    if violation["code"] not in group:
                        group[violation["code"]] = {**violation, "client_ids": []}
                    group[violation["code"]]["client_ids"].append(client_id)
    finally:
        judge.db.close()
    statistics = {field: sum(client["stats"][field] for client in clients)
                  for field in ("tool_calls", "write_attempts", "physical_writes", "idempotency_hits", "verification_calls")}
    statistics["clients"] = len(clients)
    return {"schedule": schedule, "schedule_label": SCHEDULES[schedule], "strategy": strategy,
            "label": STRATEGIES[strategy]["label"], "agent_mode": "rules", "mode": "rules",
            "evaluation_protocol": EVALUATION_PROTOCOL,
            "scenario": config.scenario, "request_id": config.request_id,
            "capabilities": {key: STRATEGIES[strategy][key] for key in ("idempotency", "verification")},
            "failed": bool(violations), "actual_success": database_result["actual_success"],
            "failure_fingerprint": "|".join(sorted(violations)), "violations": list(violations.values()),
            "business_failed": bool(categorized["business_violations"]),
            "report_failed": bool(categorized["reporting_violations"]),
            **{key: list(group.values()) for key, group in categorized.items()},
            "business_records": database_result["business_records"],
            "stock_initial": database_result["stock_initial"], "stock_remaining": database_result["stock_remaining"],
            "reported_success": (all(client["reported_success"] for client in clients)
                                 if all(isinstance(client["reported_success"], bool) for client in clients) else None),
            "events": timeline.events, "client_results": clients, "stats": statistics,
            "database_filename": filename.name}


def run_concurrency_suite(config, output_dir, on_progress=None, cancel_check=None, on_checkpoint=None):
    """Run four independent strategies under sequential and arranged re-entry.

    Completed cases are immutable after publication. Cancellation never publishes
    an unfinished case as a pass. The cancellation callback should be thread
    safe (e.g. threading.Event.is_set). Original model settings remain untouched.
    """
    try:
        effective = Config.from_dict({**config.public(), "agent_mode": "rules", "faults": []})
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        provenance = report_provenance(effective)
    except Exception:
        traceback.print_exc()
        raise RuntimeError(INTERNAL_ERROR) from None
    cases = []

    def report(status="partial", reason=None):
        return {"kind": "concurrency", "status": status, "mode": "rules", "config": effective.public(),
                "evaluation_protocol": EVALUATION_PROTOCOL,
                "cases": list(cases), "completed": len(cases), "planned": len(SCHEDULES) * len(STRATEGIES),
                "stopped_reason": reason, "limitations": list(LIMITATIONS), "provenance": provenance,
                "model_identity": None, "model_usage": {"requests": 0, "external_model_verified": False},
                "schedule_order": list(SCHEDULES), "strategy_order": list(STRATEGIES)}

    try:
        for schedule in SCHEDULES:
            for strategy in STRATEGIES:
                if cancel_check and cancel_check():
                    raise _Cancelled()
                if on_progress:
                    on_progress("concurrency", int(len(cases) / 8 * 100),
                                f"{SCHEDULES[schedule]} · {STRATEGIES[strategy]['label']}（{len(cases) + 1}/8）")
                filename = directory / f"{schedule}-{strategy}.sqlite"
                result = _run_case(effective, schedule, strategy, filename, cancel_check)
                cases.append(result)
                if on_checkpoint:
                    on_checkpoint(report())
        result = report("finished")
    except _Cancelled:
        result = report("partial", "cancelled")
    except Exception:
        traceback.print_exc()
        raise RuntimeError(INTERNAL_ERROR) from None
    try:
        if on_checkpoint:
            on_checkpoint(result)
        if on_progress:
            on_progress("done" if result["status"] == "finished" else "partial", int(len(cases) / 8 * 100),
                        "重复提交检查完成" if result["status"] == "finished" else "检查已停止；保留完成的用例")
    except Exception:
        traceback.print_exc()
        raise RuntimeError(INTERNAL_ERROR) from None
    return result
