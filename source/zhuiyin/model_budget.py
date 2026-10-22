"""Optional, persistent request/cost guard for a single real-model campaign.

No messages, request payloads, API keys, or private paths enter the ledger or its
public summary. Monetary accounting uses integer millionths of a yuan. The
input reservation is deliberately conservative, not a tokenizer guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import urllib.parse
import uuid


POLICY_ENV = "ZHUIYIN_CAMPAIGN_POLICY"
MAX_CAMPAIGN_REQUESTS = 374
MICROS_PER_CNY = 1_000_000
INPUT_PADDING = 8192
OUTPUT_LIMIT = 1200
APPLICATION_ID = 0x5A594247
ASSUMPTION = ("输入预留采用 ensure_ascii=True 的请求 JSON UTF-8 字节数加 8192；"
              "这是保守计量假设，并非 tokenizer 上界保证。输出预留 1200 token；"
              "失败、未知用量和中断请求保留完整预留费用。")


class CampaignBudgetError(RuntimeError):
    """Safe, fixed reason codes; never include a path or provider response."""

    def __init__(self, reason="campaign_guard_unavailable"):
        self.reason = reason
        super().__init__(f"本轮模型预算门闸已停止请求（{reason}）")


@dataclass(frozen=True, repr=False)
class _Policy:
    ledger_path: str
    public_json: str
    fingerprint: str
    max_requests: int
    max_cost_micros: int
    input_price: Decimal
    output_price: Decimal


@dataclass(frozen=True, repr=False)
class Reservation:
    """Opaque handle held in memory, with no request data."""

    policy: _Policy
    reservation_id: str


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("invalid amount")
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("invalid amount")
    return result


def _load_policy():
    path = os.environ.get(POLICY_ENV)
    if path is None:
        return None
    try:
        if not path or not Path(path).is_absolute():
            raise ValueError("invalid policy path")
        raw_bytes = Path(path).read_bytes()
        if len(raw_bytes) > 65_536:
            raise ValueError("oversized policy")
        raw = json.loads(raw_bytes, parse_float=Decimal)
        if not isinstance(raw, dict):
            raise ValueError("invalid policy")
        origin = raw["allowed_origin"]
        model = raw["allowed_model"]
        if origin != "https://api.deepseek.com" or model != "deepseek-flash":
            raise ValueError("unsupported campaign identity")
        requests = raw["max_requests"]
        if (isinstance(requests, bool) or not isinstance(requests, int)
                or not 1 <= requests <= MAX_CAMPAIGN_REQUESTS):
            raise ValueError("invalid request limit")
        ceiling = _decimal(raw["max_cost_cny"])
        if ceiling > 10 or ceiling * MICROS_PER_CNY != (ceiling * MICROS_PER_CNY).to_integral_value():
            raise ValueError("invalid cost limit")
        input_price = _decimal(raw["input_price_per_million"])
        output_price = _decimal(raw["output_price_per_million"])
        if (input_price > 1_000_000 or output_price > 1_000_000
                or input_price.as_tuple().exponent < -6 or output_price.as_tuple().exponent < -6):
            raise ValueError("invalid price")
        ledger_path = raw["ledger_path"]
        if not isinstance(ledger_path, str) or not Path(ledger_path).is_absolute():
            raise ValueError("invalid ledger path")
        public = {"schema_version": 1, "allowed_origin": origin, "allowed_model": model,
                  "max_requests": requests, "max_cost_cny": format(ceiling, "f"),
                  "input_price_per_million": format(input_price, "f"),
                  "output_price_per_million": format(output_price, "f"),
                  "currency": "CNY", "input_padding": INPUT_PADDING, "max_output_tokens": OUTPUT_LIMIT}
        canonical = json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return _Policy(str(Path(ledger_path).resolve()), canonical,
                       hashlib.sha256(canonical.encode()).hexdigest(), requests,
                       int(ceiling * MICROS_PER_CNY), input_price, output_price)
    except (OSError, ValueError, KeyError, TypeError, DecimalException, OverflowError):
        raise CampaignBudgetError("policy_invalid") from None


def _identity_matches(config, payload):
    try:
        parsed = urllib.parse.urlsplit(config.get("base_url", ""))
        return (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and config.get("model") == "deepseek-flash"
                and payload.get("model") == "deepseek-flash"
                and type(payload.get("max_tokens")) is int and payload["max_tokens"] == OUTPUT_LIMIT)
    except (AttributeError, TypeError, ValueError):
        return False


def _connect(policy):
    db = None
    try:
        db = sqlite3.connect(policy.ledger_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("damaged ledger")
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables and app_id == 0:
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE requests (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "reservation_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, "
                       "reserved_prompt_tokens INTEGER NOT NULL, reserved_completion_tokens INTEGER NOT NULL, "
                       "reserved_micros INTEGER NOT NULL, charged_micros INTEGER NOT NULL, "
                       "actual_prompt_tokens INTEGER, actual_completion_tokens INTEGER)")
            db.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", [
                ("policy_sha256", policy.fingerprint), ("policy_json", policy.public_json),
                ("halted_reason", "")])
        elif app_id != APPLICATION_ID or tables != {"metadata", "requests", "sqlite_sequence"}:
            raise ValueError("incompatible ledger")
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if (metadata.get("policy_sha256") != policy.fingerprint
                or metadata.get("policy_json") != policy.public_json
                or set(metadata) != {"policy_sha256", "policy_json", "halted_reason"}
                or metadata["halted_reason"] not in ("", "usage_bound_exceeded")):
            raise ValueError("policy differs")
        rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY sequence")]
        sequence_row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()
        if (sequence_row[0] if sequence_row else 0) != len(rows):
            raise ValueError("ledger history removed")
        for index, row in enumerate(rows, 1):
            for key in ("reserved_prompt_tokens", "reserved_completion_tokens", "reserved_micros", "charged_micros",
                        "actual_prompt_tokens", "actual_completion_tokens"):
                if row[key] is not None:
                    row[key] = _read_integer(row[key])
            if row["sequence"] != index or row["state"] not in ("reserved", "settled", "unknown", "overrun"):
                raise ValueError("invalid ledger state")
            if any(type(row[key]) is not int or row[key] < 0 for key in (
                    "reserved_prompt_tokens", "reserved_completion_tokens", "reserved_micros", "charged_micros")):
                raise ValueError("invalid ledger amount")
            if row["reserved_completion_tokens"] != OUTPUT_LIMIT or row["reserved_prompt_tokens"] < INPUT_PADDING:
                raise ValueError("invalid reservation")
            expected = _cost(policy, row["reserved_prompt_tokens"], row["reserved_completion_tokens"])
            if row["reserved_micros"] != expected:
                raise ValueError("reservation cost differs")
            if row["state"] in ("reserved", "unknown"):
                if (row["charged_micros"] != expected or row["actual_prompt_tokens"] is not None
                        or row["actual_completion_tokens"] is not None):
                    raise ValueError("invalid unknown usage")
            else:
                if any(type(row[key]) is not int or row[key] < 0 for key in (
                        "actual_prompt_tokens", "actual_completion_tokens")):
                    raise ValueError("invalid settled usage")
                actual = _cost(policy, row["actual_prompt_tokens"], row["actual_completion_tokens"])
                overrun = (row["actual_prompt_tokens"] > row["reserved_prompt_tokens"]
                           or row["actual_completion_tokens"] > row["reserved_completion_tokens"])
                if (overrun != (row["state"] == "overrun")
                        or row["charged_micros"] != (max(actual, expected) if overrun else actual)):
                    raise ValueError("invalid settled amount")
        if (len(rows) > policy.max_requests
                or any(row["state"] == "overrun" for row in rows) != (metadata["halted_reason"] == "usage_bound_exceeded")):
            raise ValueError("invalid ledger totals")
        return db, rows, metadata
    except Exception:
        if db is not None:
            db.close()
        raise CampaignBudgetError("ledger_unavailable") from None


def _cost(policy, prompt, completion):
    # tokens * yuan per million tokens = millionths of a yuan.
    input_numerator, input_denominator = policy.input_price.as_integer_ratio()
    output_numerator, output_denominator = policy.output_price.as_integer_ratio()
    numerator = (prompt * input_numerator * output_denominator
                 + completion * output_numerator * input_denominator)
    denominator = input_denominator * output_denominator
    return (numerator + denominator - 1) // denominator


def _store_integer(value):
    # SQLite's INTEGER is signed 64 bit. Explicitly tagged text preserves even
    # malformed, enormous provider counts exactly without coercion to REAL.
    return value if value <= 2**63 - 1 else "i:" + str(value)


def _read_integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.startswith("i:"):
        number = int(value[2:])
        if number > 2**63 - 1 and str(number) == value[2:]:
            return number
    raise ValueError("invalid ledger integer")


def _money(micros):
    whole, remainder = divmod(micros, MICROS_PER_CNY)
    return f"{whole}.{remainder:06d}"


def reserve_request(config, payload):
    """Atomically reserve one possibly billable request before network I/O.

    A successful reservation counts as sent even if the process subsequently
    crashes. Rejected reservations do not increment the request count.
    """
    policy = _load_policy()
    if policy is None:
        return None
    if not _identity_matches(config, payload):
        raise CampaignBudgetError("campaign_identity_mismatch")
    try:
        prompt_bound = len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) + INPUT_PADDING
    except (TypeError, ValueError, OverflowError):
        raise CampaignBudgetError("request_unaccountable") from None
    reserved = _cost(policy, prompt_bound, OUTPUT_LIMIT)
    db, rows, metadata = _connect(policy)
    try:
        if metadata["halted_reason"]:
            raise CampaignBudgetError(metadata["halted_reason"])
        if len(rows) >= policy.max_requests:
            raise CampaignBudgetError("request_limit_reached")
        if sum(row["charged_micros"] for row in rows) + reserved > policy.max_cost_micros:
            raise CampaignBudgetError("cost_limit_reached")
        reservation_id = uuid.uuid4().hex
        db.execute("INSERT INTO requests(reservation_id,state,reserved_prompt_tokens,"
                   "reserved_completion_tokens,reserved_micros,charged_micros) VALUES (?,?,?,?,?,?)",
                   (reservation_id, "reserved", prompt_bound, OUTPUT_LIMIT, reserved, reserved))
        db.commit()
        return Reservation(policy, reservation_id)
    except CampaignBudgetError:
        raise
    except Exception:
        raise CampaignBudgetError("ledger_unavailable") from None
    finally:
        db.close()


def settle_request(reservation, record):
    """Settle complete valid successful usage; otherwise keep the full reserve.

    Calling this again for the same handle is harmless. Any usage exceeding its
    reservation is recorded conservatively, then permanently halts the ledger.
    """
    if reservation is None:
        return
    if not isinstance(reservation, Reservation):
        raise CampaignBudgetError("reservation_invalid")
    policy = reservation.policy
    db, rows, _ = _connect(policy)
    try:
        row = next((row for row in rows if row["reservation_id"] == reservation.reservation_id), None)
        if row is None:
            raise CampaignBudgetError("reservation_invalid")
        if row["state"] != "reserved":
            return
        complete = (isinstance(record, dict) and record.get("status") == "ok"
                    and record.get("usage_available") is True
                    and all(type(record.get(key)) is int and record[key] >= 0
                            for key in ("prompt_tokens", "completion_tokens")))
        if complete:
            prompt, completion = record["prompt_tokens"], record["completion_tokens"]
            overrun = (prompt > row["reserved_prompt_tokens"]
                       or completion > row["reserved_completion_tokens"])
            actual = _cost(policy, prompt, completion)
            charged = max(actual, row["reserved_micros"]) if overrun else actual
            state = "overrun" if overrun else "settled"
            db.execute("UPDATE requests SET state=?,charged_micros=?,actual_prompt_tokens=?,"
                       "actual_completion_tokens=? WHERE reservation_id=?",
                       (state, _store_integer(charged), _store_integer(prompt), _store_integer(completion),
                        reservation.reservation_id))
            if overrun:
                db.execute("UPDATE metadata SET value='usage_bound_exceeded' WHERE key='halted_reason'")
        else:
            overrun = False
            db.execute("UPDATE requests SET state='unknown' WHERE reservation_id=?", (reservation.reservation_id,))
        db.commit()
        if overrun:
            raise CampaignBudgetError("usage_bound_exceeded")
    except CampaignBudgetError:
        raise
    except Exception:
        raise CampaignBudgetError("ledger_unavailable") from None
    finally:
        db.close()


def public_summary():
    """Return safe audit totals; no private filesystem location or model text."""
    policy = _load_policy()
    if policy is None:
        return None
    db, rows, metadata = _connect(policy)
    try:
        total = sum(row["charged_micros"] for row in rows)
        unsettled = [row for row in rows if row["state"] == "reserved"]
        unknown = [row for row in rows if row["state"] == "unknown"]
        settled = [row for row in rows if row["state"] in ("settled", "overrun")]
        summary = json.loads(policy.public_json)
        summary.update({"policy_sha256": policy.fingerprint, "requests_made": len(rows),
                        "requests_remaining": max(0, policy.max_requests - len(rows)),
                        "accounted_cost_cny": _money(total),
                        "reserved_cost_cny": _money(sum(row["charged_micros"] for row in unsettled + unknown)),
                        "settled_cost_cny": _money(sum(row["charged_micros"] for row in settled)),
                        "remaining_cost_cny": _money(max(0, policy.max_cost_micros - total)),
                        "unsettled_requests": len(unsettled), "unknown_usage_requests": len(unknown),
                        "known_usage_requests": len(settled),
                        "known_prompt_tokens": sum(row["actual_prompt_tokens"] for row in settled),
                        "known_completion_tokens": sum(row["actual_completion_tokens"] for row in settled),
                        "overrun_requests": sum(row["state"] == "overrun" for row in rows),
                        "halted_reason": metadata["halted_reason"] or None,
                        "reservation_assumption": ASSUMPTION})
        db.commit()
        return summary
    except Exception:
        raise CampaignBudgetError("ledger_unavailable") from None
    finally:
        db.close()
