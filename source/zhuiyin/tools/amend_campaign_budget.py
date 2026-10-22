"""One authorized 360 -> 374 amendment, copying the frozen ledger offline.

The source policy, ledger and archived evidence are never opened for writing.
This command performs no network activity and cannot reserve a model request.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import model_budget

DEFAULT_SOURCE_POLICY = ROOT / "data/evaluations/v061-review-20261017/campaign-policy.json"
SOURCE_POLICY_SHA256 = "b674e9cb1bcb2d5b4bd0d166f0726647cd5cfb9aa6e2331ae389b4c4b91cfd12"
SOURCE_REQUESTS_SHA256 = "08a79a195da40603294b61cc0f0de6f76c725e1ab7bb312bbdd2e063faf93afd"
SOURCE_REQUESTS = 230
SOURCE_COST_MICROS = 612582
AUTHORIZATION = "批准提高至374次，费用仍累计不超过10元"
VERSION = "campaign-amendment-v1"
POLICY_FIELDS = {"allowed_origin", "allowed_model", "max_requests", "max_cost_cny",
                 "input_price_per_million", "output_price_per_million", "ledger_path",
                 "price_basis", "price_source", "price_checked_date", "billing_limitation"}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


@contextmanager
def _policy_environment(path):
    previous = os.environ.get(model_budget.POLICY_ENV)
    os.environ[model_budget.POLICY_ENV] = str(Path(path).resolve())
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(model_budget.POLICY_ENV, None)
        else:
            os.environ[model_budget.POLICY_ENV] = previous


def _load_policy(path):
    with _policy_environment(path):
        return model_budget._load_policy()


def _write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def _read_database(db):
    db.row_factory = sqlite3.Row
    _require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "源台账完整性检查失败")
    _require(db.execute("PRAGMA application_id").fetchone()[0] == model_budget.APPLICATION_ID,
             "源台账应用标识不符")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    _require(tables == {"metadata", "requests", "sqlite_sequence"}, "源台账结构不符")
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    _require(set(metadata) == {"policy_sha256", "policy_json", "halted_reason"}, "源台账元数据不符")
    _require(metadata["halted_reason"] == "", "源台账已停止，不允许迁移")
    rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY sequence")]
    sequence = [list(row) for row in db.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name")]
    return metadata, rows, sequence


def _summary(public, rows, metadata):
    amount = sum(row["charged_micros"] for row in rows)
    summary = dict(public)
    summary.update({"policy_sha256": metadata["policy_sha256"], "requests_made": len(rows),
                    "requests_remaining": public["max_requests"] - len(rows),
                    "accounted_cost_cny": model_budget._money(amount),
                    "remaining_cost_cny": model_budget._money(int(Decimal(public["max_cost_cny"])
                                                                 * model_budget.MICROS_PER_CNY) - amount),
                    "known_usage_requests": len(rows), "unsettled_requests": 0,
                    "unknown_usage_requests": 0, "overrun_requests": 0, "halted_reason": None,
                    "known_prompt_tokens": sum(row["actual_prompt_tokens"] for row in rows),
                    "known_completion_tokens": sum(row["actual_completion_tokens"] for row in rows)})
    return summary


def amend_campaign_budget(output, *, source_policy=DEFAULT_SOURCE_POLICY,
                          max_requests=model_budget.MAX_CAMPAIGN_REQUESTS):
    """Create one new ledger/policy pair; reject an existing output directory.

    Only the frozen 230 settled requests and their original pricing are accepted.
    A failed amendment removes its own fresh output directory and leaves inputs
    byte-for-byte intact. The caller must explicitly activate the new policy.
    """
    _require(type(max_requests) is int and max_requests == 374
             and max_requests <= model_budget.MAX_CAMPAIGN_REQUESTS, "本轮仅授权累计 374 次请求")
    source_policy = Path(source_policy).resolve()
    output = Path(output).resolve()
    _require(not output.exists(), "输出目录已存在，禁止覆盖")
    _require(output.parent.is_dir(), "输出目录的父目录不存在")
    source_policy_bytes = source_policy.read_bytes()
    _require(len(source_policy_bytes) <= 65_536, "源政策过大")
    raw = json.loads(source_policy_bytes)
    _require(isinstance(raw, dict) and set(raw) == POLICY_FIELDS, "源政策字段不符")
    old_policy = _load_policy(source_policy)
    _require(old_policy is not None and old_policy.fingerprint == SOURCE_POLICY_SHA256,
             "源政策不是已验收的累计 360 次、10 元政策")
    public_before = json.loads(old_policy.public_json)
    _require(public_before["max_requests"] == 360 and public_before["max_cost_cny"] == "10",
             "源政策上限不符")
    source_ledger = Path(old_policy.ledger_path).resolve()
    _require(source_ledger.is_file(), "源台账不存在")
    _require(not source_policy.is_relative_to(output) and not source_ledger.is_relative_to(output),
             "输出目录不得包含源材料")
    _require(not Path(str(source_ledger) + "-wal").exists(), "源台账含活动 WAL，不能迁移")
    source_ledger_sha256 = digest(source_ledger)
    with closing(_readonly(source_ledger)) as source:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        metadata_before, rows_before, sequence_before = _read_database(source)
        _require(metadata_before["policy_json"] == old_policy.public_json
                 and metadata_before["policy_sha256"] == old_policy.fingerprint, "源政策与台账不一致")
        _require(len(rows_before) == SOURCE_REQUESTS
                 and sequence_before == [["requests", SOURCE_REQUESTS]], "源台账次数或序列不符")
        _require(all(row["state"] == "settled" for row in rows_before), "源台账含未结算、未知或超限请求")
        _require(hashlib.sha256(canonical(rows_before)).hexdigest() == SOURCE_REQUESTS_SHA256,
                 "源台账不符已验收的 230 条请求")
        _require(sum(row["charged_micros"] for row in rows_before) == SOURCE_COST_MICROS,
                 "源台账费用不符")
        created = False
        try:
            output.mkdir(exist_ok=False)
            created = True
            target_ledger = output / "campaign-ledger.sqlite"
            target_policy = output / "campaign-policy.json"
            with closing(sqlite3.connect(target_ledger)) as target:
                source.backup(target)
            # Validate every old row using the same guard before changing policy.
            copied_old_policy = model_budget._Policy(str(target_ledger), old_policy.public_json,
                                                     old_policy.fingerprint, old_policy.max_requests,
                                                     old_policy.max_cost_micros, old_policy.input_price,
                                                     old_policy.output_price)
            validated, _, _ = model_budget._connect(copied_old_policy)
            validated.close()
            raw_after = dict(raw, max_requests=max_requests, ledger_path=str(target_ledger))
            _write_json(target_policy, raw_after)
            new_policy = _load_policy(target_policy)
            public_after = json.loads(new_policy.public_json)
            _require({**public_before, "max_requests": max_requests} == public_after,
                     "政策除请求上限外发生变化")
            with closing(sqlite3.connect(target_ledger)) as target:
                try:
                    target.execute("BEGIN IMMEDIATE")
                    target.execute("UPDATE metadata SET value=? WHERE key='policy_json'", (new_policy.public_json,))
                    target.execute("UPDATE metadata SET value=? WHERE key='policy_sha256'", (new_policy.fingerprint,))
                    metadata_after, rows_after, sequence_after = _read_database(target)
                    _require(rows_after == rows_before, "副本请求行发生变化")
                    _require(sequence_after == sequence_before, "副本 sqlite_sequence 发生变化")
                    _require(metadata_after == {**metadata_before, "policy_json": new_policy.public_json,
                                                "policy_sha256": new_policy.fingerprint}, "副本元数据异常")
                    target.commit()
                except Exception:
                    target.rollback()
                    raise
            validated, _, _ = model_budget._connect(new_policy)
            validated.close()
            _require(source_policy.read_bytes() == source_policy_bytes
                     and digest(source_ledger) == source_ledger_sha256, "迁移期间源材料发生变化")
            summary_before = _summary(public_before, rows_before, metadata_before)
            summary_after = _summary(public_after, rows_after, metadata_after)
            amendment = {
                "schema_version": 1, "amendment_version": VERSION,
                "created_utc": datetime.now(timezone.utc).isoformat(), "status": "completed",
                "authorization": {"source": "本聊天用户明确授权", "client_date": "2026-10-18",
                                  "text": AUTHORIZATION, "previous_max_requests": 360,
                                  "max_requests": max_requests, "max_cost_cny": "10"},
                "source": {"policy_file": str(source_policy), "policy_file_sha256":
                           hashlib.sha256(source_policy_bytes).hexdigest(),
                           "ledger_file": str(source_ledger), "ledger_file_sha256": source_ledger_sha256},
                "target": {"policy_file": str(target_policy), "policy_file_sha256": digest(target_policy),
                           "ledger_file": str(target_ledger), "ledger_file_sha256": digest(target_ledger)},
                "before": summary_before, "after": summary_after,
                "preservation": {"request_rows": SOURCE_REQUESTS, "requests_equal": rows_after == rows_before,
                                 "source_requests_sha256": SOURCE_REQUESTS_SHA256,
                                 "target_requests_sha256": hashlib.sha256(canonical(rows_after)).hexdigest(),
                                 "sqlite_sequence_before": sequence_before, "sqlite_sequence_after": sequence_after,
                                 "source_files_unchanged": True,
                                 "changed_metadata_keys": ["policy_json", "policy_sha256"],
                                 "changed_public_policy_fields": ["max_requests"]},
                "new_model_requests": 0,
                "activation": "迁移完成但未激活；后续执行必须显式使用本目录 campaign-policy.json。",
                "limitation": "本文件记录本地授权及账本连续性，不是服务商账单或外部认证。"}
            _write_json(output / "campaign-initial.json", summary_after)
            _write_json(output / "amendment.json", amendment)
            return amendment
        except Exception:
            if created:
                shutil.rmtree(output)
            raise
        finally:
            source.rollback()


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线复制旧账，仅将累计请求上限从 360 提高为 374；不调用模型。")
    parser.add_argument("--source-policy", type=Path, default=DEFAULT_SOURCE_POLICY)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=374)
    args = parser.parse_args(argv)
    result = amend_campaign_budget(args.output, source_policy=args.source_policy, max_requests=args.max_requests)
    print(json.dumps({"status": result["status"], "requests_made": result["after"]["requests_made"],
                      "requests_remaining": result["after"]["requests_remaining"],
                      "max_requests": result["after"]["max_requests"],
                      "max_cost_cny": result["after"]["max_cost_cny"], "new_model_requests": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
