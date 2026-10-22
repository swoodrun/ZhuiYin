"""Executable SQLite fault laboratory. No third-party packages required."""
from __future__ import annotations

import itertools
import hashlib
import inspect
import json
from pathlib import Path
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from model_budget import CampaignBudgetError, public_summary, reserve_request, settle_request
from run_billing import RunBudget, RunBudgetError, summarize_usage, validate_billing

FAULTS = {
    "commit_response_lost": "提交后响应丢失",
    "stale_read": "读取过期状态",
    "truncate_response": "响应内容缺失",
    "latency": "工具调用延迟",
    "precommit_timeout": "写入前超时",
    "ack_without_commit": "未提交却返回成功",
    "corrupt_payload": "业务内容损坏",
}
DEFAULT_FAULTS = ("commit_response_lost", "stale_read", "truncate_response", "latency")
STRATEGIES = {
    "baseline": {"label": "原始", "idempotency": False, "verification": False},
    "idempotency_only": {"label": "仅幂等", "idempotency": True, "verification": False},
    "verification_only": {"label": "仅核验", "idempotency": False, "verification": True},
    "verified": {"label": "幂等＋核验", "idempotency": True, "verification": True},
}
MODEL_CONTEXT = threading.local()
APP_VERSION = "0.7.0"
PROMPT_VERSION = "tool-assistant-v2"
EVALUATION_PROTOCOL = "business-report-v2"
MAX_MODEL_REQUESTS = 400
DEFAULT_EXPERIMENT_REQUESTS = 400
MAX_TRIAL_REQUESTS = 9
MODEL_CONTEXT_FIELDS = ("remaining", "request_limit", "cancel_check", "usage_records", "budget_exhausted", "campaign_stopped_reason", "run_budget", "cost_stopped_reason")

# The compact views use the same evaluation fields as a full trial. Only copy
# fields that were actually supplied: historical boolean reports retain their
# original protocol instead of being upgraded by presentation defaults.
EVALUATION_FIELDS = (
    "strategy", "scenario", "agent_mode", "evaluation_protocol", "prompt_policy",
    "completed", "status", "skipped", "stopped_reason", "infra_error",
    "reported_outcome", "report_assessment", "reported_success", "actual_success",
    "business_violations", "reporting_violations", "execution_violations",
    "business_failed", "report_failed", "failed", "failure_fingerprint",
    "violations", "stats",
)


def evaluation_summary(result: dict) -> dict:
    """Return detached, public evaluation fields without inventing a protocol.

    Tool events, database dumps, business records, model configuration and any
    unrecognized source fields are deliberately outside this compact contract.
    """
    return json.loads(json.dumps({key: result[key] for key in EVALUATION_FIELDS if key in result},
                                 ensure_ascii=False))


class IncompleteBaselineError(RuntimeError):
    """An unfinished attempt with evidence to persist, not a counterexample."""

    def __init__(self, message: str, partial_result: dict):
        super().__init__(message)
        self.partial_result = partial_result


@dataclass
class Config:
    equipment: str = "单反相机"
    stock: int = 3
    quantity: int = 1
    request_id: str = "req-demo-001"
    seed: int = 42
    faults: list[str] = field(default_factory=lambda: list(DEFAULT_FAULTS))
    agent_mode: str = "rules"
    model_config: dict = field(default_factory=dict, repr=False)
    scenario: str = "equipment"
    ticket_title: str = "教学楼门禁故障"
    ticket_priority: str = "normal"
    max_requests: int = DEFAULT_EXPERIMENT_REQUESTS
    prompt_policy: str = "neutral"
    billing: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        if not isinstance(data, dict):
            raise ValueError("参数必须是一个对象")
        values = {}
        for name, default in [("equipment", "单反相机"), ("request_id", "req-demo-001")]:
            value = data.get(name, default)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
                raise ValueError(f"{name} 需要填写 1—80 个字符")
            values[name] = value.strip()
        for name, default, low, high in [("stock", 3, 0, 1000000), ("quantity", 1, 1, 1000000),
                                        ("seed", 42, 0, 2147483647),
                                        ("max_requests", DEFAULT_EXPERIMENT_REQUESTS, 1, MAX_MODEL_REQUESTS)]:
            value = data.get(name, default)
            if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.isdecimal())):
                raise ValueError(f"{name} 必须是整数")
            value = int(value)
            if not low <= value <= high:
                raise ValueError(f"{name} 需要在 {low}—{high} 之间")
            values[name] = value
        scenario = data.get("scenario", "equipment")
        if scenario not in ("equipment", "ticket"):
            raise ValueError("业务场景仅支持 equipment 或 ticket")
        values["scenario"] = scenario
        title = data.get("ticket_title", "教学楼门禁故障")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            raise ValueError("ticket_title 需要填写 1—200 个字符")
        values["ticket_title"] = title.strip()
        priority = data.get("ticket_priority", "normal")
        if priority not in ("low", "normal", "high", "urgent"):
            raise ValueError("ticket_priority 仅支持 low、normal、high、urgent")
        values["ticket_priority"] = priority
        faults = data.get("faults", list(DEFAULT_FAULTS))
        if not isinstance(faults, list) or any(not isinstance(x, str) or x not in FAULTS for x in faults):
            raise ValueError("故障列表含不支持的类型")
        values["faults"] = list(dict.fromkeys(faults))
        mode = data.get("agent_mode", "rules")
        if mode not in ("rules", "llm"):
            raise ValueError("运行模式无效")
        values["agent_mode"] = mode
        policy = data.get("prompt_policy", "neutral")
        if policy not in ("neutral", "recovery"):
            raise ValueError("提示策略仅支持 neutral 或 recovery")
        values["prompt_policy"] = policy
        values["billing"] = validate_billing(data.get("billing"))
        mc = data.get("model_config") or {}
        if not isinstance(mc, dict):
            raise ValueError("模型配置无效")
        values["model_config"] = {k: str(mc.get(k, "")).strip() for k in ("base_url", "model", "api_key")}
        if mode == "llm":
            validate_model_config(values["model_config"])
        return cls(**values)

    def public(self):
        result = {k: getattr(self, k) for k in ("equipment", "stock", "quantity", "request_id", "seed", "faults", "agent_mode", "scenario", "ticket_title", "ticket_priority", "max_requests", "prompt_policy")}
        if self.billing:
            result["billing"] = dict(self.billing)
        return result


def validate_model_config(config: dict):
    if not isinstance(config, dict):
        raise ValueError("模型配置必须是对象")
    url = config.get("base_url", "")
    p = urllib.parse.urlsplit(url)
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("请填写不含账号、查询参数的模型 API 基础地址")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("模型地址需要 HTTPS；本机模型可使用 HTTP")
    if not config.get("model"):
        raise ValueError("请填写模型名称")
    key = config.get("api_key", "")
    if not isinstance(key, str) or any(ord(c) < 32 or ord(c) == 127 or ord(c) > 126 for c in key):
        raise ValueError("API 密钥包含不支持的字符，请重新输入")


def _is_deepseek_official(config):
    """Vendor-specific request settings apply only to DeepSeek's HTTPS origin."""
    try:
        parsed = urllib.parse.urlsplit(config.get("base_url", ""))
        return (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except (AttributeError, TypeError, ValueError):
        return False


def model_identity(config):
    """Retain a model name and service origin, never a URL path or credential."""
    if config.agent_mode != "llm":
        return None
    raw = config.model_config
    origin = None
    try:
        parsed = urllib.parse.urlsplit(raw.get("base_url", ""))
        if parsed.scheme in ("http", "https") and parsed.hostname:
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            origin = f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
    except (TypeError, ValueError):
        pass
    identity = {"model": str(raw.get("model", ""))[:500], "api_origin": origin}
    if _is_deepseek_official(raw):
        identity["thinking"] = "disabled"
    return identity


def report_provenance(config):
    """Hashes describe saved source bytes and public inputs, not private settings.

    The prompt hash covers the llm_agent function definition (including its tool
    and instruction templates), rather than any user or model message contents.
    """
    root = Path(__file__).resolve().parent
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(root.glob("*.py")) if not path.name.startswith("test_")}
    canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")
    provenance = {"app_version": APP_VERSION, "prompt_version": PROMPT_VERSION,
            "evaluation_protocol": EVALUATION_PROTOCOL, "prompt_policy": config.prompt_policy,
            "prompt_definition_sha256": hashlib.sha256((inspect.getsource(agent_spec) + inspect.getsource(llm_agent)).encode("utf-8")).hexdigest(),
            "source_sha256": source_hashes,
            "source_tree_sha256": hashlib.sha256(canonical(source_hashes)).hexdigest(),
            "public_config_sha256": hashlib.sha256(canonical(config.public())).hexdigest()}
    if config.agent_mode == "llm" and _is_deepseek_official(config.model_config):
        provenance["model_request_settings"] = {"thinking": "disabled"}
    provenance["factor_design"] = {"instruction_policy": config.prompt_policy,
            "capability_factors": ["idempotency", "verification"],
            "system_prompt_shared_within_policy": True,
            "manifests": {strategy: agent_spec(config, strategy)["manifest"] for strategy in STRATEGIES}}
    return provenance


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _capture_usage(record, usage):
    """Normalize explicit provider usage; unknown cache usage never becomes zero."""
    if not isinstance(usage, dict):
        return

    def token(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        record[name] = token(usage.get(name))
    prompt = record["prompt_tokens"]
    hit = token(usage.get("prompt_cache_hit_tokens"))
    miss = token(usage.get("prompt_cache_miss_tokens"))
    malformed = any(key in usage and token(usage[key]) is None
                    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"))
    details = usage.get("prompt_tokens_details")
    if hit is None and "prompt_cache_hit_tokens" not in usage and isinstance(details, dict) and "cached_tokens" in details:
        hit = token(details["cached_tokens"])
        malformed = malformed or hit is None
        if not malformed and prompt is not None and hit is not None and miss is None and hit <= prompt:
            miss = prompt - hit
    cache_known = (not malformed and prompt is not None and hit is not None and miss is not None
                   and hit + miss == prompt)
    record["prompt_cache_hit_tokens"] = hit if cache_known else None
    record["prompt_cache_miss_tokens"] = miss if cache_known else None
    record["cache_usage_available"] = cache_known
    record["usage_available"] = prompt is not None and record["completion_tokens"] is not None


def model_request(config: dict, messages: list, tools: list | None = None) -> dict:
    validate_model_config(config)
    if getattr(MODEL_CONTEXT, "cancel_check", None) and MODEL_CONTEXT.cancel_check():
        raise RuntimeError("用户已停止本次实验")
    if getattr(MODEL_CONTEXT, "cost_stopped_reason", None):
        raise RuntimeError("本次费用保护额度已停止新请求；已有记录已保存。")
    if hasattr(MODEL_CONTEXT, "remaining"):
        if MODEL_CONTEXT.remaining <= 0:
            MODEL_CONTEXT.budget_exhausted = True
            limit = getattr(MODEL_CONTEXT, "request_limit", 40)
            raise RuntimeError(f"达到本次实验 {limit} 次模型请求上限；停止继续请求")
    base = config["base_url"].rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {"model": config["model"], "messages": messages, "temperature": 0, "max_tokens": 1200}
    if _is_deepseek_official(config):
        payload["thinking"] = {"type": "disabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    if config.get("api_key"):
        headers["Authorization"] = "Bearer " + config["api_key"]
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers=headers, method="POST")
    run_budget = getattr(MODEL_CONTEXT, "run_budget", None)
    local_reservation = None
    if run_budget is not None:
        try:
            local_reservation = run_budget.reserve(payload)
        except RunBudgetError:
            MODEL_CONTEXT.budget_exhausted = True
            MODEL_CONTEXT.cost_stopped_reason = "cost_budget_exhausted"
            raise RuntimeError("本次费用保护额度不足以预留下一次请求；已停止并保留已有记录。") from None
    try:
        reservation = reserve_request(config, payload)
    except CampaignBudgetError:
        if run_budget is not None:
            run_budget.cancel(local_reservation)
        MODEL_CONTEXT.budget_exhausted = True
        MODEL_CONTEXT.campaign_stopped_reason = "campaign_budget_exhausted"
        raise RuntimeError("本轮实验的累计请求或费用保护已停止新请求；已保留已有记录。") from None
    if hasattr(MODEL_CONTEXT, "remaining"):
        MODEL_CONTEXT.remaining -= 1
    started = time.perf_counter()
    record = {"status": "network_error", "prompt_tokens": None, "completion_tokens": None,
              "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None,
              "cache_usage_available": False,
              "total_tokens": None, "usage_available": False, "elapsed_ms": 0, "http_status": None}
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=35) as response:
            record["http_status"] = getattr(response, "status", 200)
            record["status"] = "invalid_response"
            body = json.loads(response.read(2_000_000))
        usage = body.get("usage") if isinstance(body, dict) else None
        _capture_usage(record, usage)
        message = body["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("invalid message")
        record["status"] = "ok"
        return message
    except urllib.error.HTTPError as exc:
        # Do not echo provider payloads, which can include credentials.
        record.update(status="http_error", http_status=exc.code)
        code = exc.code
        exc.close()
        raise RuntimeError(f"模型接口返回 HTTP {code}，请检查地址、模型名称和权限") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("模型连接失败或超时，请检查接口地址和网络") from None
    except (KeyError, ValueError, IndexError):
        raise RuntimeError("模型响应不符合 Chat Completions 格式") from None
    except Exception:
        raise RuntimeError("模型请求未完成，请检查配置和服务状态") from None
    finally:
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if isinstance(getattr(MODEL_CONTEXT, "usage_records", None), list):
            MODEL_CONTEXT.usage_records.append(record)
        try:
            if run_budget is not None:
                try:
                    run_budget.settle(local_reservation, record)
                except RunBudgetError:
                    MODEL_CONTEXT.budget_exhausted = True
                    MODEL_CONTEXT.cost_stopped_reason = "cost_budget_exhausted"
                    raise RuntimeError("本次费用用量无法安全结算；已停止新请求并保留预留。") from None
        finally:
            try:
                settle_request(reservation, record)
            except CampaignBudgetError:
                MODEL_CONTEXT.budget_exhausted = True
                MODEL_CONTEXT.campaign_stopped_reason = "campaign_budget_exhausted"
                raise RuntimeError("累计费用台账无法安全结算；已停止新请求并保留预算预留。") from None


class Lab:
    def __init__(self, config: Config, faults: list[str], strategy: str):
        if strategy not in STRATEGIES:
            raise ValueError("不支持的策略")
        self.config, self.faults, self.strategy = config, set(faults), strategy
        self.idempotency = STRATEGIES[strategy]["idempotency"]
        self.verification = STRATEGIES[strategy]["verification"]
        self.is_ticket = config.scenario == "ticket"
        self.record_key = "work_orders" if self.is_ticket else "reservations"
        self.write_tool = "create_ticket" if self.is_ticket else "reserve_device"
        self.query_tool = "find_ticket" if self.is_ticket else "query_reservations"
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        if self.is_ticket:
            self.db.executescript("""
                CREATE TABLE work_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                    title TEXT NOT NULL, priority TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE operation_keys(request_id TEXT PRIMARY KEY, work_order_id INTEGER NOT NULL);
            """)
        else:
            self.db.executescript("""
                CREATE TABLE inventory(equipment TEXT PRIMARY KEY, available INTEGER CHECK(available>=0));
                CREATE TABLE reservations(id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT,equipment TEXT,quantity INTEGER,status TEXT);
                CREATE TABLE operation_keys(request_id TEXT PRIMARY KEY,reservation_id INTEGER NOT NULL);
            """)
            self.db.execute("INSERT INTO inventory VALUES (?,?)", (config.equipment, config.stock))
        self.db.commit()
        self.events, self.writes, self.calls = [], 0, 0
        self.lost = self.stale_used = self.uncertain = False
        self.precommit_used = self.ack_used = self.corrupt_used = False
        self.reported = None
        self.reported_outcome = None
        self.idempotency_hits = self.verification_calls = 0

    def records(self):
        table = "work_orders" if self.is_ticket else "reservations"
        return [dict(r) for r in self.db.execute(f"SELECT * FROM {table} ORDER BY id")]

    def remaining(self):
        return None if self.is_ticket else self.db.execute("SELECT available FROM inventory").fetchone()[0]

    def event(self, tool, action, observed, actual=None, effect="", kind="tool"):
        self.events.append({"step": len(self.events) + 1, "tool": tool, "action": action,
                            "observed": observed, "actual": actual, "effect": effect, "kind": kind,
                            "request_id": self.config.request_id})

    def query(self, strong=False):
        if strong and not self.verification:
            raise ValueError("当前策略不支持独立检查")
        self.calls += 1
        if strong:
            self.verification_calls += 1
        rows = self.records()
        stale = "stale_read" in self.faults and self.uncertain and not self.stale_used and not strong
        seen = [] if stale else rows
        if stale:
            self.stale_used = True
        result = {self.record_key: seen}
        self.event("verify_receipt" if strong else self.query_tool,
                   "独立检查已提交工单" if strong and self.is_ticket else "独立检查已提交记录" if strong else "查询本次请求的工单" if self.is_ticket else "查询本次请求的预约",
                   result, {self.record_key: rows}, "返回过期空列表（故障注入）" if stale else "读取 SQLite 真实记录",
                   "fault" if stale else "tool")
        return result

    def payload_matches(self, row):
        if self.is_ticket:
            return row.get("title") == self.config.ticket_title and row.get("priority") == self.config.ticket_priority
        return row.get("equipment") == self.config.equipment and row.get("quantity") == self.config.quantity

    def healthy_records(self, rows):
        active = [r for r in rows if r.get("status") == "active" and r.get("request_id") == self.config.request_id]
        return len(active) == 1 and self.payload_matches(active[0])

    def reserve(self):
        """Execute a real business transaction; reserve is the compatibility API."""
        self.calls += 1
        cfg = self.config
        if "latency" in self.faults:
            time.sleep(0.015)
            self.event("transport", "注入 15ms 工具延迟", {"delay_ms": 15}, effect="真实等待，作为无关干扰项", kind="fault")
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        id_column = "work_order_id" if self.is_ticket else "reservation_id"
        if self.idempotency:
            old = cur.execute(f"SELECT {id_column} FROM operation_keys WHERE request_id=?", (cfg.request_id,)).fetchone()
            if old:
                self.db.commit()
                self.idempotency_hits += 1
                self.uncertain = False
                result = {"ok": True, "ticket_id" if self.is_ticket else "reservation_id": old[0], "deduplicated": True}
                self.event(self.write_tool, "使用相同请求编号重试", result, {self.record_key: self.records()},
                           "服务端幂等事务返回原凭证，未新增写入")
                return result
        if "precommit_timeout" in self.faults and not self.precommit_used:
            self.precommit_used, self.uncertain = True, True
            self.db.rollback()
            result = {"ok": False, "error": "工具超时，调用方无法确定提交结果", "code": "COMMIT_UNKNOWN"}
            self.event(self.write_tool, "首次写入前注入超时", result,
                       {"committed": False, self.record_key: self.records()}, "尚未写入；事务已回滚，客户端仅收到未知结果", "fault")
            return result
        if "ack_without_commit" in self.faults and not self.ack_used:
            self.ack_used, self.uncertain = True, False
            self.db.rollback()
            result = {"ok": True, "ticket_id" if self.is_ticket else "reservation_id": "uncommitted-ack"}
            self.event(self.write_tool, "首次返回成功但未提交", result,
                       {"committed": False, self.record_key: self.records()}, "成功回执由故障层伪造；SQLite 中没有新增记录", "fault")
            return result
        corrupt = "corrupt_payload" in self.faults and not self.corrupt_used
        if self.is_ticket:
            priority = ("urgent" if cfg.ticket_priority != "urgent" else "low") if corrupt else cfg.ticket_priority
            cur.execute("INSERT INTO work_orders(request_id,title,priority,status) VALUES (?,?,?,'active')",
                        (cfg.request_id, cfg.ticket_title, priority))
        else:
            changed = cur.execute("UPDATE inventory SET available=available-? WHERE equipment=? AND available>=?",
                                  (cfg.quantity, cfg.equipment, cfg.quantity)).rowcount
            if not changed:
                self.db.rollback()
                result = {"ok": False, "error": "库存不足", "code": "INSUFFICIENT_STOCK"}
                self.event(self.write_tool, "尝试预留设备", result, {"stock": self.remaining()}, "库存约束拒绝写入")
                return result
            stored_quantity = cfg.quantity + 1 if corrupt else cfg.quantity
            cur.execute("INSERT INTO reservations(request_id,equipment,quantity,status) VALUES (?,?,?,'active')",
                        (cfg.request_id, cfg.equipment, stored_quantity))
        rid = cur.lastrowid
        if self.idempotency:
            cur.execute("INSERT INTO operation_keys VALUES (?,?)", (cfg.request_id, rid))
        self.db.commit()
        self.writes += 1
        actual = {"committed": True, "ticket_id" if self.is_ticket else "reservation_id": rid,
                  self.record_key: self.records()}
        if not self.is_ticket:
            actual["stock"] = self.remaining()
        if corrupt:
            self.corrupt_used = True
            self.event("storage_boundary", "提交时注入业务字段损坏", {"ok": True}, actual,
                       "工单 priority 被改写，标题保持不变" if self.is_ticket else "预约 quantity 被改写，库存仍按原请求扣减", "fault")
        if "commit_response_lost" in self.faults and not self.lost:
            self.lost, self.uncertain = True, True
            result = {"ok": False, "error": "工具响应超时，提交结果未知", "code": "COMMIT_UNKNOWN"}
            self.event(self.write_tool, "创建工单，提交后丢失响应" if self.is_ticket else "预留设备，提交后丢失响应",
                       result, actual, "SQLite 已提交；成功响应被故障层丢弃", "fault")
            return result
        self.uncertain = False
        result = ({"ok": True, "ticket_id": rid, "title": cfg.ticket_title, "priority": cfg.ticket_priority} if self.is_ticket else
                  {"ok": True, "reservation_id": rid, "equipment": cfg.equipment, "quantity": cfg.quantity})
        if "truncate_response" in self.faults:
            result.pop("title" if self.is_ticket else "equipment")
            self.event("transport", "删除响应中的非关键显示字段", result.copy(),
                       effect="保留状态和凭证编号，不改变业务结果", kind="fault")
        self.event(self.write_tool, "创建工单" if self.is_ticket else "预留设备", result, actual,
                   "实际新增一条 work_orders 工单" if self.is_ticket else "实际新增一条预约并扣减库存")
        return result

    def finish(self, success=None, reason="", *, outcome=None):
        """A completed uncertain report is different from no report at all.

        Boolean calls remain supported for deterministic agents and old clients.
        New model tools publish the explicit three-state outcome schema.
        """
        if outcome is None:
            if not isinstance(success, bool):
                raise ValueError("success 必须是布尔值，或提供明确 outcome")
            outcome = "success" if success else "failure"
        if outcome not in ("success", "failure", "unknown"):
            raise ValueError("outcome 必须是 success、failure 或 unknown")
        expected = {"success": True, "failure": False, "unknown": None}[outcome]
        if success is not None and (not isinstance(success, bool) or success != expected):
            raise ValueError("success 与 outcome 不一致")
        self.reported_outcome, self.reported = outcome, expected
        self.event("finish_task", "报告任务结果",
                   {"outcome": outcome, "success": self.reported, "reason": reason}, kind="report")

    def result(self, agent_mode, execution_error=None):
        rows = self.records()
        active = [r for r in rows if r["status"] == "active"]
        actual_success = self.healthy_records(active)
        violations = []
        if len(active) > 1:
            violations.append({"code": "DUPLICATE_TICKET" if self.is_ticket else "DUPLICATE_RESERVATION",
                               "message": f"同一请求产生了 {len(active)} 条有效工单" if self.is_ticket else f"同一请求产生了 {len(active)} 条有效预约"})
        if any(not self.payload_matches(row) for row in active):
            violations.append({"code": "PAYLOAD_MISMATCH", "message": "已提交工单的标题或优先级与请求不一致" if self.is_ticket else "已提交预约的设备或数量与请求不一致"})
        if not self.is_ticket:
            if self.remaining() < 0:
                violations.append({"code": "NEGATIVE_STOCK", "message": "可借数量为负数"})
            if self.config.stock - self.remaining() != sum(r["quantity"] for r in active):
                violations.append({"code": "STOCK_INCONSISTENT", "message": "库存扣减与预约数量不一致"})
        if self.reported_outcome is None:
            violations.append({"code": "AGENT_DID_NOT_FINISH", "message": "助手没有提交明确结果"})
            assessment = "unreported"
        elif self.reported_outcome == "unknown":
            assessment = "uncertain"
        elif self.reported != actual_success:
            violations.append({"code": "REPORT_MISMATCH", "message": "报告结果与数据库实际状态不一致"})
            assessment = "inconsistent"
        else:
            assessment = "consistent"
        feasible = self.is_ticket or self.config.stock >= self.config.quantity
        if len(active) == 0 and feasible and not execution_error:
            violations.append({"code": "TASK_INCOMPLETE", "message": "任务具备执行条件，但没有产生有效业务记录"})
        if execution_error:
            violations.append({"code": "MODEL_EXECUTION_ERROR", "message": execution_error})
        reporting = [v for v in violations if v["code"] == "REPORT_MISMATCH"]
        execution = [v for v in violations if v["code"] in ("AGENT_DID_NOT_FINISH", "MODEL_EXECUTION_ERROR")]
        business = [v for v in violations if v not in reporting and v not in execution]
        evaluation = {"strategy": self.strategy, "scenario": self.config.scenario, "agent_mode": agent_mode,
                "evaluation_protocol": EVALUATION_PROTOCOL, "prompt_policy": self.config.prompt_policy,
                "completed": self.reported_outcome is not None and not execution_error,
                "reported_outcome": self.reported_outcome, "report_assessment": assessment,
                "business_violations": business, "reporting_violations": reporting, "execution_violations": execution,
                "business_failed": bool(business), "report_failed": bool(reporting),
                "failed": bool(violations), "failure_fingerprint": "|".join(sorted(x["code"] for x in violations)),
                "violations": violations,
                "reported_success": self.reported, "actual_success": actual_success,
                "stats": {"tool_calls": self.calls, "physical_writes": self.writes,
                          "idempotency_hits": self.idempotency_hits, "verification_calls": self.verification_calls}}
        return {**evaluation_summary(evaluation), "reservations": rows, "business_records": rows,
                "stock_initial": None if self.is_ticket else self.config.stock, "stock_remaining": self.remaining(),
                "events": self.events,
                "sqlite_dump": "\n".join(self.db.iterdump())}


def rules_agent(lab: Lab):
    found = lab.query()[lab.record_key]
    if found:
        if lab.verification:
            found = lab.query(strong=True)[lab.record_key]
        lab.finish(lab.healthy_records(found) if lab.verification else True, "核对本次请求已有业务记录")
        return
    result = lab.reserve()
    if result.get("code") == "COMMIT_UNKNOWN":
        # The two interventions are orthogonal: only verification-enabled arms
        # have a strong read; idempotency-only merely de-duplicates server writes.
        found = lab.query(strong=lab.verification)[lab.record_key]
        result = {"ok": True} if found else lab.reserve()
    if lab.verification:
        verified = lab.query(strong=True)[lab.record_key]
        if not verified and result.get("ok") is True:
            # One extra bounded write can recover a false positive acknowledgement.
            result = lab.reserve()
            if result.get("code") == "COMMIT_UNKNOWN":
                recovered = lab.query(strong=True)[lab.record_key]
                if not recovered:
                    lab.reserve()
            verified = lab.query(strong=True)[lab.record_key]
        lab.finish(lab.healthy_records(verified), "独立读取已提交凭证并核对数量和业务字段；损坏记录不会被声明为已修复")
    else:
        lab.finish(result.get("ok", False), "根据普通查询和写入回执报告；未启用独立读取检查")


def agent_spec(config: Config, strategy="baseline"):
    """Keep the instruction factor independent of the two tool capabilities.

    Within one policy the exact system message is identical for all four arms.
    Tool contracts disclose their real semantics; no arm gets extra operating
    steps just because an independent read is available.
    """
    capabilities = STRATEGIES[strategy]
    is_ticket = config.scenario == "ticket"
    query_name = "find_ticket" if is_ticket else "query_reservations"
    write_name = "create_ticket" if is_ticket else "reserve_device"
    def tool(name, desc, properties=None, required=None):
        return {"type": "function", "function": {"name": name, "description": desc,
                "parameters": {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}}
    contract = ("按请求编号提供幂等事务，重复写入不会新增同一请求的业务记录。" if capabilities["idempotency"] else
                "非幂等写入，重复调用可能新增业务记录。")
    tools = [tool(query_name, "查询当前请求的业务记录，读取可能受缓存影响。"),
             tool(write_name, "按用户请求创建业务记录。COMMIT_UNKNOWN 表示提交结果未知。" + contract),
             tool("finish_task", "报告任务结论后结束；无法确认时允许报告 unknown。",
                  {"outcome": {"type": "string", "enum": ["success", "failure", "unknown"]},
                   "reason": {"type": "string"}}, ["outcome", "reason"])]
    instruction = ("你是工单创建助手。目标是让同一请求恰好产生一条标题和优先级正确的有效工单。" if is_ticket else
                   "你是设备借用助手。目标是让同一请求恰好产生一条设备和数量正确的有效预约，不能重复预留。库存不足应报告失败。")
    instruction += "使用工具获取事实，不得虚构执行结果。最后必须调用finish_task，报告success、failure或unknown；unknown表示现有证据无法确认。"
    if config.prompt_policy == "recovery":
        instruction += " 写入结果未知时先读取证据再决定是否重试；若有独立核验工具，应优先使用它核对恰好一条有效记录及业务字段。成功回执但记录为空时最多再写入一次；字段损坏不能声明已修复。证据不足时报告unknown。"
    if capabilities["verification"]:
        tools.append(tool("verify_receipt", "独立读取本次请求的已提交业务凭证，不经过缓存。"))
    task_fields = ("ticket_title", "ticket_priority", "request_id") if is_ticket else ("equipment", "quantity", "request_id")
    task = {k: getattr(config, k) for k in task_fields}
    canonical = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest = {"prompt_policy": config.prompt_policy, "idempotency": capabilities["idempotency"],
                "verification": capabilities["verification"], "system_prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                "tool_schema_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                "parameter_scope": "server_supplied_business_fields", "verification_enforcement": "agent_choice"}
    return {"system": instruction, "task": task, "tools": tools, "manifest": manifest}


def llm_agent(lab: Lab, config: dict):
    spec = agent_spec(lab.config, lab.strategy)
    tools = spec["tools"]
    messages = [{"role": "system", "content": spec["system"]},
                {"role": "user", "content": json.dumps(spec["task"], ensure_ascii=False)}]
    for _ in range(MAX_TRIAL_REQUESTS):
        answer = model_request(config, messages, tools)
        calls = answer.get("tool_calls") or []
        assistant = {"role": "assistant", "content": answer.get("content"), **({"tool_calls": calls} if calls else {})}
        # Thinking-compatible services require this verbatim on subsequent tool
        # turns. It is transient context, never a public business event.
        if isinstance(answer.get("reasoning_content"), str):
            assistant["reasoning_content"] = answer["reasoning_content"]
        messages.append(assistant)
        if not calls:
            messages.append({"role": "user", "content": "请根据工具返回继续，完成时调用finish_task提交明确结果。"})
            continue
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments", "{}"))
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            if name == lab.query_tool:
                result = lab.query()
            elif name == lab.write_tool:
                result = lab.reserve()
            elif name == "verify_receipt" and lab.verification:
                result = lab.query(strong=True)
            elif name == "finish_task":
                try:
                    # Accept old boolean clients while the advertised tool
                    # schema uses the explicit three-state protocol.
                    lab.finish(args.get("success"), str(args.get("reason", ""))[:400], outcome=args.get("outcome"))
                    return
                except ValueError as exc:
                    result = {"error": str(exc)}
            else:
                result = {"error": "未知工具"}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
    raise RuntimeError(f"已用完 {MAX_TRIAL_REQUESTS} 次模型请求，助手仍未完成任务")


def run_trial(config: Config, faults: list[str], strategy="baseline", agent_mode="rules", model_config=None):
    if strategy not in STRATEGIES:
        raise ValueError("不支持的策略")
    lab = Lab(config, faults, strategy)
    error = None
    try:
        if agent_mode == "llm":
            llm_agent(lab, model_config or config.model_config)
        else:
            rules_agent(lab)
    except Exception as exc:
        error = str(exc)[:400]
    result = lab.result(agent_mode, error)
    result["factor_manifest"] = agent_spec(config, strategy)["manifest"] if agent_mode == "llm" else None
    lab.db.close()
    return result


def _cancelled():
    check = getattr(MODEL_CONTEXT, "cancel_check", None)
    return bool(check and check())


def _stop_reason(mode, required_requests=1, reserve_requests=0):
    if _cancelled():
        return "cancelled"
    if mode == "llm" and getattr(MODEL_CONTEXT, "cost_stopped_reason", None):
        return MODEL_CONTEXT.cost_stopped_reason
    if mode == "llm" and getattr(MODEL_CONTEXT, "campaign_stopped_reason", None):
        return MODEL_CONTEXT.campaign_stopped_reason
    if mode == "llm" and hasattr(MODEL_CONTEXT, "remaining"):
        if MODEL_CONTEXT.remaining < required_requests + reserve_requests:
            return "request_budget_exhausted"
    return None


def _trial_incomplete(result):
    codes = set(str(result.get("failure_fingerprint", "")).split("|"))
    codes.update(item.get("code") for item in result.get("violations", []) if isinstance(item, dict))
    return (bool(codes & {"MODEL_EXECUTION_ERROR", "AGENT_DID_NOT_FINISH"}) or
            ("reported_success" in result and result["reported_success"] is None
             and not (result.get("evaluation_protocol") == EVALUATION_PROTOCOL
                      and result.get("reported_outcome") == "unknown" and result.get("completed") is True)) or
            result.get("status") in ("not_run", "infra_error", "cancelled") or
            result.get("completed") is False)


def minimize(config: Config, faults: list[str], on_step: Callable | None = None, repeats=2,
             expected_fingerprint=None, reserve_requests=0):
    """Delete only fully repeated business counterexamples; stop before a partial candidate.

    A model trial can use at most MAX_TRIAL_REQUESTS requests. Each group is
    admitted only when all repeats and the caller's fixed-arm reserve fit.
    Standalone calls without a model context retain the historical behavior.
    """
    mode, repeats = config.agent_mode, max(2, repeats)
    original, remaining, steps = list(faults), list(faults), []
    initial_runs, final_runs = [], []
    final_revalidation = {"status": "not_run", "completed": False, "repeats": repeats,
                          "attempted": 0, "same_error_count": 0}
    trials, stable = 0, False
    fingerprint = expected_fingerprint or ""

    def report(completed, stopped_reason=None):
        reasons = {
            "request_budget_exhausted": "剩余预算无法完成下一组重复检查并为幂等＋核验留足请求；故障精简停止，保留当前组合。",
            "cost_budget_exhausted": "本次费用保护额度已停止新请求；故障精简未完成，已有记录已保存。",
            "campaign_budget_exhausted": "累计请求或费用保护已停止新调用；故障精简未完成，已保存执行证据。",
            "cancelled": "实验已停止；已完成的重复检查会保留。",
            "model_execution_error": "模型调用或任务报告未完成；运行错误不算业务失败，本次删除无效。",
            "unstable_initial_failure": "同一业务错误复现不稳定，或模型调用未完成；本次不精简故障。",
            "unstable_final_failure": "精简后的组合未能在独立复查中全部复现同一失败；结果保留但精简未完成。",
        }
        reason = reasons.get(stopped_reason) or ("已检查当前组合中的每一项；单独删除任何一项都会改变失败。结果可能还有更少的组合，也不涉及因果分析。" if mode == "rules" else
                "模型模式在有限重复次数内保留同一失败；更少的组合仍有可能。")
        last_accepted = max((i for i, step in enumerate(steps) if step["accepted"]), default=-1)
        return {"faults": remaining[:], "steps": steps, "original_count": len(original),
                "evaluation_protocol": EVALUATION_PROTOCOL, "prompt_policy": config.prompt_policy,
                "initial_runs": initial_runs, "final_runs": final_runs,
                "observed_initial_fingerprints": [r.get("failure_fingerprint", "") for r in initial_runs],
                "final_revalidation": final_revalidation,
                "boundary_step_indexes": list(range(last_accepted + 1, len(steps))),
                "final_count": len(remaining), "trials": trials, "one_minimal": completed and stable and mode == "rules",
                "stable": stable, "failure_fingerprint": fingerprint, "reason": reason,
                "completed": completed, "stopped_reason": stopped_reason,
                "reserve_requests": reserve_requests if mode == "llm" else 0}

    def repeated(fault_set):
        nonlocal trials
        reason = _stop_reason(mode, repeats * MAX_TRIAL_REQUESTS, reserve_requests)
        if reason:
            return [], reason
        runs = []
        for _ in range(repeats):
            reason = _stop_reason(mode, MAX_TRIAL_REQUESTS, reserve_requests)
            if reason:
                return runs, reason
            usage_start = len(getattr(MODEL_CONTEXT, "usage_records", []))
            result = dict(run_trial(config, fault_set, agent_mode=mode))
            usage = getattr(MODEL_CONTEXT, "usage_records", [])[usage_start:]
            result["evaluated_faults"] = list(fault_set)
            result["repetition"] = len(runs) + 1
            result["model_usage"] = {"requests": len(usage), "records": usage[:],
                                     "external_model_verified": mode == "llm" and bool(usage)}
            trials += 1
            runs.append(result)
            if _cancelled():
                return runs, "cancelled"
            if _trial_incomplete(result):
                reason = (getattr(MODEL_CONTEXT, "cost_stopped_reason", None) or getattr(MODEL_CONTEXT, "campaign_stopped_reason", None) or
                          ("request_budget_exhausted" if getattr(MODEL_CONTEXT, "budget_exhausted", False) else "model_execution_error"))
                return runs, reason
        return runs, None

    initial_runs, stopped = repeated(faults)
    if stopped:
        if initial_runs and not fingerprint:
            fingerprint = initial_runs[0].get("failure_fingerprint", "")
        return report(False, stopped)
    if expected_fingerprint is None:
        fingerprint = initial_runs[0]["failure_fingerprint"]
    stable = bool(fingerprint) and all(r["failure_fingerprint"] == fingerprint for r in initial_runs)
    if not stable:
        return report(False, "unstable_initial_failure")
    order = original[:]
    random.Random(config.seed).shuffle(order)
    # Restart after a successful deletion: later deletions may change relevance.
    while True:
        removed = False
        for fault in order:
            if fault not in remaining:
                continue
            candidate = [f for f in remaining if f != fault]
            runs, stopped = repeated(candidate)
            if not runs:
                return report(False, stopped)
            # Target reproduction is distinct from the absence of every other
            # business error. Each attempted run has exactly one outcome.
            outcomes = {"same_error": 0, "other_error": 0, "no_error": 0, "infra_error": 0}
            for run in runs:
                if _trial_incomplete(run):
                    outcome = "infra_error"
                elif run.get("failure_fingerprint", "") == fingerprint:
                    outcome = "same_error"
                elif run.get("failed") or run.get("failure_fingerprint"):
                    outcome = "other_error"
                else:
                    outcome = "no_error"
                outcomes[outcome] += 1
            reproduced = outcomes["same_error"]
            accepted = not stopped and reproduced == repeats
            if accepted:
                remaining = candidate
                removed = True
            step = {"candidate": candidate, "removed_fault": fault, "failed": reproduced > 0,
                    "same_error_reproduced": reproduced > 0, "same_error_count": reproduced,
                    "other_error_count": outcomes["other_error"], "no_error_count": outcomes["no_error"],
                    "infra_error_count": outcomes["infra_error"], "outcome_counts": outcomes,
                    "reproduced": reproduced, "repeats": repeats, "attempted": len(runs), "accepted": accepted,
                    "completed": not bool(stopped), "stopped_reason": stopped,
                    "remaining": remaining[:], "fingerprints": [r["failure_fingerprint"] for r in runs],
                    "trial_evidence": runs}
            steps.append(step)
            if on_step:
                on_step(step)
            if stopped:
                return report(False, stopped)
            if accepted:
                break
        if not removed:
            break
    # This group is separate from all deletion decisions, so a complete record
    # contains independent confirmation of the final counterexample as well as
    # the final pass of single-deletion boundary checks.
    final_runs, stopped = repeated(remaining)
    same = sum(not _trial_incomplete(r) and r.get("failure_fingerprint") == fingerprint for r in final_runs)
    final_revalidation = {"status": "completed" if not stopped else "partial",
                          "completed": not bool(stopped), "repeats": repeats, "attempted": len(final_runs),
                          "same_error_count": same, "stopped_reason": stopped,
                          "fingerprints": [r.get("failure_fingerprint", "") for r in final_runs]}
    if stopped:
        return report(False, stopped)
    if same != repeats:
        stable = False
        final_revalidation["status"] = "unstable"
        return report(False, "unstable_final_failure")
    return report(True)


def run_experiment(config: Config, on_progress=None, cancel_check=None, *, include_rule_checks=True):
    """Run one isolated experiment, restoring any enclosing study context.

    An unfinished baseline still raises: it is not a business counterexample.
    Once a valid baseline exists, budget/cancellation yields an explicit partial
    report, including the last verified reduction and any completed fixed arm.
    """
    sentinel = object()
    previous = {key: getattr(MODEL_CONTEXT, key, sentinel) for key in MODEL_CONTEXT_FIELDS}
    run_budget = RunBudget(config.billing) if config.agent_mode == "llm" and config.billing else None
    MODEL_CONTEXT.remaining = config.max_requests
    MODEL_CONTEXT.request_limit = config.max_requests
    MODEL_CONTEXT.cancel_check = cancel_check
    MODEL_CONTEXT.usage_records = []
    MODEL_CONTEXT.budget_exhausted = False
    MODEL_CONTEXT.campaign_stopped_reason = None
    MODEL_CONTEXT.run_budget = run_budget
    MODEL_CONTEXT.cost_stopped_reason = None
    try:
        return _run_experiment(config, on_progress, include_rule_checks=include_rule_checks)
    except IncompleteBaselineError as exc:
        if not include_rule_checks:
            _mark_agent_only(exc.partial_result, config)
        raise
    finally:
        for key, value in previous.items():
            if value is sentinel:
                if hasattr(MODEL_CONTEXT, key):
                    delattr(MODEL_CONTEXT, key)
            else:
                setattr(MODEL_CONTEXT, key, value)


def _mark_agent_only(result, config):
    result["execution_profile"] = "agent-only-v1"
    result["agent_specs"] = {strategy: agent_spec(config, strategy) for strategy in ("baseline", "verified")}
    for name in ("regression", "ablation", "expanded_regression"):
        result[name].update(status="not_run", skipped=True, completed=False, stopped_reason="separate_rule_checks")


def _skipped_trial(config, reason):
    return {"strategy": "verified", "scenario": config.scenario, "agent_mode": config.agent_mode,
            "status": "not_run", "completed": False, "skipped": True, "stopped_reason": reason,
            "failed": None, "failure_fingerprint": "", "violations": [],
            "evaluation_protocol": EVALUATION_PROTOCOL, "prompt_policy": config.prompt_policy,
            "reported_outcome": None, "report_assessment": "unreported",
            "business_violations": [], "reporting_violations": [], "execution_violations": [],
            "business_failed": None, "report_failed": None,
            "reservations": [], "business_records": [], "stock_initial": None, "stock_remaining": None,
            "reported_success": None, "actual_success": None, "events": [],
            "stats": {"tool_calls": 0, "physical_writes": 0, "idempotency_hits": 0, "verification_calls": 0}}


def _incomplete_baseline_report(config, baseline, reason):
    """Keep the attempted trial and usage without starting downstream phases."""
    baseline = {**baseline, "completed": False, "status": "infra_error", "stopped_reason": reason,
                "failed": None}
    report = {"config": config.public(), "mode": config.agent_mode,
              "evaluation_protocol": EVALUATION_PROTOCOL,
              "model_identity": model_identity(config), "provenance": report_provenance(config),
              "status": "partial", "completed": False, "stopped_reason": reason,
              "baseline": baseline, "fixed": _skipped_trial(config, reason),
              "minimized_faults": config.faults[:],
              "minimization": {"faults": config.faults[:], "steps": [], "initial_runs": [], "final_runs": [],
                  "original_count": len(config.faults), "final_count": len(config.faults), "trials": 0,
                  "one_minimal": False, "stable": False, "failure_fingerprint": "",
                  "completed": False, "stopped_reason": reason, "status": "not_run",
                  "reason": "主试验未完成，没有有效反例；未开始故障精简。"},
              "regression": {"agent_mode": "rules", "cases": [], "total": 0,
                  "planned": 2 ** len(DEFAULT_FAULTS), "completed": False,
                  "baseline_failures": 0, "fixed_failures": 0},
              "ablation": {"agent_mode": "rules", "arms": [], "completed": False},
              "expanded_regression": {"agent_mode": "rules", "cases": [], "total": 0,
                  "planned": 8, "completed": False, "failures_by_strategy": {}},
              "limitations": ["主试验未完成，只保存已有执行证据与调用用量；没有把它计为业务反例。后续精简、能力组合及规则回归均未运行。"],
              "model_usage": {"requests": len(MODEL_CONTEXT.usage_records), "limit": config.max_requests,
                  "remaining": MODEL_CONTEXT.remaining, "records": MODEL_CONTEXT.usage_records[:],
                  "requests_by_phase": {"baseline": len(MODEL_CONTEXT.usage_records), "minimization": 0, "fixed": 0},
                  "external_model_verified": config.agent_mode == "llm" and bool(MODEL_CONTEXT.usage_records)}}
    _attach_run_billing(report, config)
    try:
        campaign = public_summary()
    except CampaignBudgetError:
        campaign = {"status": "unavailable", "note": "累计台账无法安全读取；新请求将被保护门闸拒绝。"}
    if campaign is not None:
        report["campaign_budget"] = campaign
    return report


def _attach_run_billing(report, config):
    """Only newly configured pricing adds cost fields; old report contracts stay intact."""
    if config.agent_mode != "llm" or not config.billing:
        return
    report["model_usage"].update(summarize_usage(MODEL_CONTEXT.usage_records, config.billing))
    guard = getattr(MODEL_CONTEXT, "run_budget", None)
    if guard is not None:
        report["run_budget"] = guard.summary()


def _run_experiment(config: Config, on_progress=None, *, include_rule_checks=True):
    def progress(phase, amount, message):
        if on_progress:
            on_progress(phase, amount, message)
    progress("baseline", 5, "运行原始策略，记录数据库变化")
    if _cancelled():
        raise RuntimeError("用户已停止本次实验；主试验未开始")
    baseline = run_trial(config, config.faults, agent_mode=config.agent_mode)
    if _trial_incomplete(baseline):
        reason = ("cancelled" if _cancelled() else MODEL_CONTEXT.cost_stopped_reason or MODEL_CONTEXT.campaign_stopped_reason or
                  ("request_budget_exhausted" if MODEL_CONTEXT.budget_exhausted else "model_execution_error"))
        partial = _incomplete_baseline_report(config, baseline, reason)
        message = ("用户已停止本次实验；主试验未完成" if reason == "cancelled" else
                   "主试验未完成：" + "; ".join(v["message"] for v in baseline["violations"]))
        raise IncompleteBaselineError(message, partial)
    baseline["completed"] = True
    phase_requests = {"baseline": len(MODEL_CONTEXT.usage_records)}
    progress("minimizing", 15, "逐项删除故障并重跑，剔除不必要的")
    counter = 0
    def on_step(step):
        nonlocal counter
        counter += 1
        progress("minimizing", min(65, 18 + counter * 4),
                 f"尝试删除 {FAULTS[step['removed_fault']]}：同一失败复现 {step['reproduced']}/{step['repeats']}，" + ("保留删除" if step["accepted"] else "恢复该条件"))
    if baseline["failed"]:
        reduced = minimize(config, config.faults, on_step=on_step, expected_fingerprint=baseline["failure_fingerprint"],
                           reserve_requests=MAX_TRIAL_REQUESTS if config.agent_mode == "llm" else 0)
    else:
        reduced = {"faults": config.faults[:], "steps": [], "original_count": len(config.faults),
                   "final_count": len(config.faults), "trials": 0, "one_minimal": False, "stable": False,
                   "failure_fingerprint": "", "reason": "原始执行没有业务错误，无需精简故障。",
                   "completed": True, "stopped_reason": None}
    phase_requests["minimization"] = len(MODEL_CONTEXT.usage_records) - phase_requests["baseline"]
    progress("fixed", 70, "在相同故障下运行幂等＋核验能力组合")
    fixed_stop = _stop_reason(config.agent_mode, MAX_TRIAL_REQUESTS)
    if fixed_stop:
        fixed = _skipped_trial(config, fixed_stop)
    else:
        fixed = run_trial(config, config.faults, "verified", config.agent_mode)
        fixed["completed"] = not _trial_incomplete(fixed)
        if not fixed["completed"]:
            fixed["status"] = "infra_error"
            fixed["stopped_reason"] = ("cancelled" if _cancelled() else MODEL_CONTEXT.cost_stopped_reason or MODEL_CONTEXT.campaign_stopped_reason or
                                      ("request_budget_exhausted" if MODEL_CONTEXT.budget_exhausted else "model_execution_error"))
            # The execution evidence remains available; this is not a measured
            # business failure of the protection mechanism.
            fixed["failed"] = None
    phase_requests["fixed"] = len(MODEL_CONTEXT.usage_records) - sum(phase_requests.values())
    stopped_reason = "cancelled" if _cancelled() else reduced.get("stopped_reason") or fixed.get("stopped_reason")
    cases = []
    regression_total = 2 ** len(DEFAULT_FAULTS)
    # Deterministic exhaustive regression is reported separately in LLM mode.
    for size in (range(len(DEFAULT_FAULTS) + 1) if include_rule_checks else ()):
        for combo in itertools.combinations(DEFAULT_FAULTS, size):
            if _cancelled():
                break
            old = run_trial(config, list(combo))
            if _cancelled():
                break
            new = run_trial(config, list(combo), "verified")
            cases.append({"name": " + ".join(FAULTS[f] for f in combo) or "无故障", "faults": list(combo),
                          "baseline_failed": old["failed"], "fixed_failed": new["failed"],
                          "baseline_fingerprint": old["failure_fingerprint"], "fixed_fingerprint": new["failure_fingerprint"]})
            progress("regression", 75 + int(len(cases) / regression_total * 23),
                     f"规则策略全组合回归：{len(cases)}/{regression_total}")
        if _cancelled():
            break
    if include_rule_checks:
        progress("ablation", 98, "同条件下运行四组策略对比")
    arms = []
    for strategy, capabilities in (STRATEGIES.items() if include_rule_checks else ()):
        if _cancelled():
            break
        if config.agent_mode == "rules" and strategy in ("baseline", "verified"):
            trial = baseline if strategy == "baseline" else fixed
        else:
            trial = run_trial(config, config.faults, strategy, "rules")
        arms.append({"strategy": strategy, **capabilities, "result": trial})
    ablation = {"agent_mode": "rules", "arms": arms, "completed": len(arms) == len(STRATEGIES),
                "interpretation": "四组使用相同场景参数和故障。幂等靠服务端按请求编号去重；检查靠独立读取已提交记录并核对字段。业务内容损坏可能使四组都失败。"}
    presets = [
        ("equipment", ["precommit_timeout"], "设备：提交前超时"),
        ("equipment", ["ack_without_commit"], "设备：假成功未提交"),
        ("equipment", ["corrupt_payload"], "设备：数量字段损坏"),
        ("equipment", ["precommit_timeout", "ack_without_commit", "corrupt_payload"], "设备：三类新增故障叠加"),
        ("ticket", list(DEFAULT_FAULTS), "工单：原四类故障迁移"),
        ("ticket", ["precommit_timeout", "stale_read"], "工单：提交前超时与缓存读取"),
        ("ticket", ["ack_without_commit", "truncate_response"], "工单：假成功与响应缺字段"),
        ("ticket", ["corrupt_payload", "commit_response_lost"], "工单：优先级损坏且响应丢失"),
    ]
    expanded_cases = []
    for index, (scenario, faults, name) in enumerate(presets if include_rule_checks else []):
        if _cancelled():
            break
        fixture = Config.from_dict({**config.public(), "scenario": scenario, "stock": 6, "quantity": 2,
                                    "request_id": f"preset-{index + 1:03d}", "faults": faults, "agent_mode": "rules"})
        strategies = {}
        for strategy in STRATEGIES:
            if _cancelled():
                break
            trial = run_trial(fixture, faults, strategy, "rules")
            strategies[strategy] = evaluation_summary(trial)
        if len(strategies) != len(STRATEGIES):
            break
        expanded_cases.append({"name": name, "scenario": scenario, "faults": faults,
                               "config": fixture.public(), "strategies": strategies})
        progress("expanded_regression", 99, f"运行设备与工单用例：{index + 1}/{len(presets)}")
    expanded = {"agent_mode": "rules", "cases": expanded_cases, "total": len(expanded_cases), "preset": True,
                "planned": len(presets), "completed": len(expanded_cases) == len(presets),
                "scope": "内置 8 个设备与工单故障组合，设备库存固定6、请求数量2，覆盖原四类及三类新增故障中的部分组合。",
                "failures_by_strategy": {strategy: sum(case["strategies"][strategy]["failed"] for case in expanded_cases) for strategy in STRATEGIES}}
    limitations = [
        "规则模式是本地模拟，只验证工具本身的机制。真实模型的效果需另测。",
        "故障在本机工具中模拟。SQLite 写入和 15ms 延迟实际执行；响应丢失和过期读取由工具模拟。",
        "四组对比和回归都用规则模式。真实模型需要另做多轮统计。",
        "故障精简保留同一失败，并重新运行删减后的组合。单项不可删只针对当前组合；更少的组合仍有可能，也不涉及因果分析。",
        f"{regression_total} 组全组合回归覆盖默认 {len(DEFAULT_FAULTS)} 类故障。新增故障和工单使用另外{len(presets)}个内置用例，只覆盖部分组合。",
        "业务内容损坏会实际写入错误字段。四种策略都不会自动修复损坏，失败会保留。",
        "独立读取与幂等操作由本地服务提供。接入真实系统时需要它提供对应能力。",
        "评价协议 business-report-v2 分别检查业务状态、报告断言与执行完成。unknown 是已结束但无法确认，不等于执行未完成，也不自动清除业务违规；旧版本记录保留原评价。",
        "模型的业务参数由服务端按场景自动填写；本实验只测工具异常恢复，不检验模型抽取或生成业务参数的能力。",
        "四种工具能力共用同一提示策略；neutral 与 recovery 是单独的提示因素，需在多轮统计中分层对照。核验工具可用不代表后端强制核验。",
        "种子决定故障精简的尝试顺序；模型输出仍可能每次不同。",
        f"本次请求预算 {config.max_requests} 次，上限 {MAX_MODEL_REQUESTS} 次。每轮最多 {MAX_TRIAL_REQUESTS} 次请求。每个删减组合至少重复两次，并为幂等＋核验留足 {MAX_TRIAL_REQUESTS} 次请求。预算不足会停止并保留结果，故障精简标为未完成。",
    ]
    if not include_rule_checks:
        limitations = [text for text in limitations if not any(term in text for term in ("四组对比和回归", "组全组合回归", "规则模式是本地模拟"))]
        limitations.append("本次为真实 Agent 专用流程：原始执行、故障精简与幂等＋核验使用所配模型；未运行规则四组对比、规则回归或内置扩展场景。四种真实 Agent 能力组的完整多轮对照需另行在统计页配置。")
    if _cancelled():
        stopped_reason = "cancelled"
    result = {"config": config.public(), "mode": config.agent_mode,
              "evaluation_protocol": EVALUATION_PROTOCOL,
              "model_identity": model_identity(config), "provenance": report_provenance(config),
              "baseline": baseline, "minimized_faults": reduced["faults"],
              "minimization": reduced, "fixed": fixed,
              "regression": {"agent_mode": "rules", "cases": cases, "total": len(cases),
                             "planned": regression_total, "completed": len(cases) == regression_total,
                             "fault_scope": list(DEFAULT_FAULTS),
                             "baseline_failures": sum(c["baseline_failed"] for c in cases),
                             "fixed_failures": sum(c["fixed_failed"] for c in cases)},
              "ablation": ablation, "expanded_regression": expanded,
              "limitations": limitations,
              "status": "partial" if stopped_reason else "finished", "completed": not bool(stopped_reason),
              "stopped_reason": stopped_reason,
              "model_usage": {"requests": len(MODEL_CONTEXT.usage_records), "limit": config.max_requests,
                              "remaining": MODEL_CONTEXT.remaining, "records": MODEL_CONTEXT.usage_records[:],
                              "requests_by_phase": phase_requests,
                              "external_model_verified": config.agent_mode == "llm" and bool(MODEL_CONTEXT.usage_records)}}
    if not include_rule_checks:
        _mark_agent_only(result, config)
    _attach_run_billing(result, config)
    try:
        campaign = public_summary()
    except CampaignBudgetError:
        campaign = {"status": "unavailable", "note": "累计台账无法安全读取；新请求将被保护门闸拒绝。"}
    if campaign is not None:
        result["campaign_budget"] = campaign
    progress("partial" if stopped_reason else "done", 100,
             "实验提前停止，已保留执行记录和当前故障组合" if stopped_reason else "实验完成，可查看记录并下载报告")
    return result
