"""Versioned, data-only task templates and separate contract checks."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import re
import sqlite3
import uuid

from engine import Config
from run_billing import validate_billing


PROTOCOL = "task-template-v1"
CONTRACT_PROTOCOL = "template-contract-v1"
CONFIG_FIELDS = frozenset({"equipment", "stock", "quantity", "request_id", "seed", "faults",
                           "agent_mode", "scenario", "ticket_title", "ticket_priority",
                           "max_requests", "prompt_policy", "billing"})
PAYLOAD_FIELDS = frozenset({"request_id", "equipment", "quantity", "title", "priority", "status"})
SNAPSHOT_FIELDS = frozenset({"id", "name", "description", "version", "config", "contract",
                             "created_at", "updated_at", "protocol"})
ACTIVE_LIMIT = 500


class TemplateConflictError(ValueError):
    """An optimistic concurrency conflict; API callers should return 409."""


def _object(value, label, allowed):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(label + "必须是对象")
    if set(value) - set(allowed):
        raise ValueError(label + "包含不支持的字段；只能保存任务数据，不能保存连接、凭据或可执行内容")
    return value


def _text(value, label, maximum, *, empty=False):
    if not isinstance(value, str) or len(value.strip()) > maximum or (not empty and not value.strip()):
        raise ValueError(label + (f"最多{maximum}个字符" if empty else f"需要填写1—{maximum}个字符"))
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise ValueError(label + "包含无效字符")
    return value.strip()


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须是{low}—{high}之间的整数")
    return value


def normalize_config(raw):
    """Validate through Config without requiring or retaining a connection."""
    _object(raw, "模板配置", CONFIG_FIELDS)
    mode = raw.get("agent_mode", "llm")
    if mode not in ("rules", "llm"):
        raise ValueError("模板运行模式无效")
    checked = Config.from_dict({**copy.deepcopy(raw), "agent_mode": "rules"}).public()
    checked["agent_mode"] = mode
    checked["billing"] = validate_billing(raw.get("billing"))
    return checked


def default_contract(config):
    """Use explicit task fields; users can change or disable each check."""
    ticket = config.get("scenario") == "ticket"
    expected = {"request_id": config["request_id"], "status": "active"}
    expected.update({"title": config["ticket_title"], "priority": config["ticket_priority"]} if ticket else
                    {"equipment": config["equipment"], "quantity": config["quantity"]})
    return {"record_count": {"mode": "exactly", "value": 1}, "expected_fields": expected,
            "stock_delta": None if ticket else -config["quantity"]}


def normalize_contract(raw):
    _object(raw, "业务规则", {"record_count", "expected_fields", "stock_delta"})
    result = {"record_count": None, "expected_fields": {}, "stock_delta": None}
    count = raw.get("record_count")
    if count is not None:
        _object(count, "记录数量规则", {"mode", "value"})
        if count.get("mode") not in ("exactly", "at_most"):
            raise ValueError("记录数量规则仅支持恰好或最多")
        result["record_count"] = {"mode": count["mode"], "value": _integer(count.get("value"), "记录数量", 0, 20)}
    fields = raw.get("expected_fields", {})
    _object(fields, "期望业务字段", PAYLOAD_FIELDS)
    for key, value in fields.items():
        if key == "quantity":
            result["expected_fields"][key] = _integer(value, "期望数量", 1, 1000000)
        elif key == "priority":
            if value not in ("low", "normal", "high", "urgent"):
                raise ValueError("期望优先级无效")
            result["expected_fields"][key] = value
        elif key == "status":
            if value != "active":
                raise ValueError("有效记录的期望状态只能是active")
            result["expected_fields"][key] = value
        else:
            result["expected_fields"][key] = _text(value, "期望" + key, 200 if key == "title" else 80)
    if raw.get("stock_delta") is not None:
        result["stock_delta"] = _integer(raw["stock_delta"], "库存变化（结束减开始）", -1000000, 1000000)
    return result


def normalize_template(raw):
    """Normalize an editable body; stored metadata is never caller-controlled."""
    _object(raw, "任务模板", {"name", "description", "config", "contract", "expected_version"})
    config = normalize_config(raw.get("config", {}))
    contract = default_contract(config) if raw.get("contract") is None else normalize_contract(raw["contract"])
    return {"name": _text(raw.get("name"), "模板名称", 80),
            "description": _text(raw.get("description", ""), "模板说明", 1000, empty=True),
            "config": config, "contract": contract}


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _id(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{12}", value) is None:
        raise ValueError("模板编号无效")
    return value


def validate_snapshot(raw):
    """Keep a detached immutable version in reports without reading the store."""
    _object(raw, "模板快照", SNAPSHOT_FIELDS)
    if not {"id", "name", "version", "config", "contract"} <= raw.keys():
        raise ValueError("模板快照缺少编号、版本、配置或业务规则")
    clean = normalize_template({key: raw[key] for key in ("name", "description", "config", "contract") if key in raw})
    clean.update(id=_id(raw.get("id")), version=_integer(raw.get("version"), "模板版本", 1, 2147483647),
                 protocol=PROTOCOL)
    if raw.get("protocol", PROTOCOL) != PROTOCOL:
        raise ValueError("模板协议不支持")
    for key in ("created_at", "updated_at"):
        clean[key] = _text(raw.get(key, ""), "模板时间", 80, empty=True)
    return clean


class TemplateStore:
    """Store current records and prior versions in a dedicated SQLite file."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS task_templates (id TEXT PRIMARY KEY, version INTEGER NOT NULL, snapshot TEXT NOT NULL, deleted_at TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS task_template_versions (id TEXT NOT NULL, version INTEGER NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(id,version))")
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def list(self):
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT snapshot FROM task_templates WHERE deleted_at IS NULL ORDER BY rowid DESC")]

    def get(self, template_id, version=None):
        template_id = _id(template_id)
        with closing(self._connect()) as db:
            if version is None:
                row = db.execute("SELECT snapshot FROM task_templates WHERE id=? AND deleted_at IS NULL", (template_id,)).fetchone()
            else:
                version = _integer(version, "模板版本", 1, 2147483647)
                row = db.execute("SELECT snapshot FROM task_template_versions WHERE id=? AND version=?", (template_id, version)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _write(db, snapshot, *, create=False):
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        if create:
            db.execute("INSERT INTO task_templates(id,version,snapshot) VALUES (?,?,?)", (snapshot["id"], snapshot["version"], encoded))
        else:
            db.execute("UPDATE task_templates SET version=?,snapshot=? WHERE id=?", (snapshot["version"], encoded, snapshot["id"]))
        db.execute("INSERT INTO task_template_versions VALUES (?,?,?)", (snapshot["id"], snapshot["version"], encoded))

    def create(self, raw):
        body = normalize_template(raw)
        timestamp = _timestamp()
        snapshot = {**body, "id": uuid.uuid4().hex[:12], "version": 1, "created_at": timestamp,
                    "updated_at": timestamp, "protocol": PROTOCOL}
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM task_templates WHERE deleted_at IS NULL").fetchone()[0] >= ACTIVE_LIMIT:
                raise ValueError("模板数量已达500个，请先整理不再使用的模板")
            self._write(db, snapshot, create=True)
            db.commit()
        return copy.deepcopy(snapshot)

    def update(self, template_id, raw, expected_version=None):
        template_id = _id(template_id)
        body = normalize_template(raw)
        expected_version = raw.get("expected_version") if expected_version is None else expected_version
        if expected_version is not None:
            _integer(expected_version, "预期模板版本", 1, 2147483647)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT snapshot FROM task_templates WHERE id=? AND deleted_at IS NULL", (template_id,)).fetchone()
            if not row:
                raise ValueError("模板不存在或已删除")
            old = json.loads(row[0])
            if expected_version is not None and old["version"] != expected_version:
                raise TemplateConflictError("模板已被更新，请刷新后再保存")
            snapshot = {**body, "id": template_id, "version": old["version"] + 1,
                        "created_at": old["created_at"], "updated_at": _timestamp(), "protocol": PROTOCOL}
            self._write(db, snapshot)
            db.commit()
        return copy.deepcopy(snapshot)

    def delete(self, template_id, expected_version=None):
        template_id = _id(template_id)
        if expected_version is not None:
            _integer(expected_version, "预期模板版本", 1, 2147483647)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version FROM task_templates WHERE id=? AND deleted_at IS NULL", (template_id,)).fetchone()
            if not row:
                raise ValueError("模板不存在或已删除")
            if expected_version is not None and row[0] != expected_version:
                raise TemplateConflictError("模板已被更新，请刷新后再删除")
            db.execute("UPDATE task_templates SET deleted_at=? WHERE id=?", (_timestamp(), template_id))
            db.commit()
        return {"deleted": True, "id": template_id, "version": row[0]}


def evaluate_contract(trial, contract, config=None):
    """Evaluate explicit data rules, never replacing the database oracle."""
    contract = normalize_contract(contract)
    trial = trial if isinstance(trial, dict) else {}
    incomplete = trial.get("skipped") is True or trial.get("completed") is False or trial.get("status") in {
        "not_run", "running", "queued", "interrupted", "infra_error", "error", "cancelled"}
    records = trial.get("business_records", trial.get("reservations"))
    records_known = isinstance(records, list) and all(isinstance(row, dict) and row.get("status") in {
        "active", "cancelled", "canceled", "closed", "deleted"} for row in records)
    active = [row for row in records if row.get("status") == "active"] if records_known else None
    checks = []

    def add(rule, label, expected, actual, passed):
        state = "unknown" if incomplete or passed is None else "pass" if passed else "fail"
        checks.append({"rule": rule, "label": label, "status": state, "expected": expected,
                       "actual": None if incomplete else actual,
                       "message": "任务未完成或证据不完整，无法核对" if state == "unknown" else
                                  "符合模板规则" if state == "pass" else "不符合模板规则"})

    count = contract["record_count"]
    if count is not None:
        actual = len(active) if active is not None else None
        passed = None if actual is None else actual == count["value"] if count["mode"] == "exactly" else actual <= count["value"]
        add("record_count", "有效记录数量", count, actual, passed)
    for key, expected in contract["expected_fields"].items():
        values = [row.get(key) for row in active] if active is not None else None
        passed = None if not active or any(key not in row for row in active) else all(type(value) is type(expected) and value == expected for value in values)
        add("field:" + key, "有效记录字段 · " + key, expected, values, passed)
    if contract["stock_delta"] is not None:
        initial = trial.get("stock_initial", config.get("stock") if isinstance(config, dict) else None)
        remaining = trial.get("stock_remaining")
        actual = remaining - initial if type(initial) is int and type(remaining) is int else None
        add("stock_delta", "库存变化（结束减开始）", contract["stock_delta"], actual,
            None if actual is None else actual == contract["stock_delta"])
    status = "fail" if any(check["status"] == "fail" for check in checks) else "unknown" if not checks or any(
        check["status"] == "unknown" for check in checks) else "pass"
    return {"protocol": CONTRACT_PROTOCOL, "status": status, "checks": checks,
            "note": "模板规则检查单独列示，不替代数据库业务裁判；未运行或证据缺失不算通过。"}
