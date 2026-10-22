"""Optional, job-local cost accounting using caller-supplied CNY token prices.

This module makes no network calls and stores no prompts or credentials.  The
reservation is a deliberately conservative local assumption, not a guarantee
about a provider's tokenizer or final invoice.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
import json
import threading
import uuid


MILLION = Decimal(1_000_000)
INPUT_PADDING = 8192
MAX_PRICE = Decimal(1_000_000)
PRICE_FIELDS = ("input_cache_hit_price_per_million", "input_cache_miss_price_per_million",
                "output_price_per_million")
ASSUMPTION = ("按用户填写的人民币单价执行本地额度保护。输入预留采用 ensure_ascii=True 的请求 JSON "
              "UTF-8 字节数加 8192，输出按 max_tokens 预留；输入采用命中与未命中单价中的较高值。"
              "这不是 tokenizer 上界保证，也不保证服务商最终扣款上界。失败或用量未知时保留完整预留；"
              "缓存分项缺失时按较高输入单价保守记账，不将未知费用写成精确费用。")


class RunBudgetError(RuntimeError):
    """Fixed safe errors, without request content or provider diagnostics."""

    def __init__(self, reason="run_budget_unavailable"):
        self.reason = reason if reason in ("cost_budget_exhausted", "run_budget_unavailable") else "run_budget_unavailable"
        super().__init__("达到本次金额上限，已停止新请求" if self.reason == "cost_budget_exhausted"
                         else "本次金额保护无法安全计量，已停止新请求")


def _decimal(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("金额和单价必须是有限数字")
    if isinstance(value, str) and len(value) > 100:
        raise ValueError("金额和单价格式无效")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or (positive and amount == 0):
            raise ValueError("金额上限必须大于零，单价必须是非负有限数字")
        if amount > MAX_PRICE or amount.normalize().as_tuple().exponent < -6:
            raise ValueError("金额和单价最多六位小数，且不得超过一百万元")
        return amount
    except DecimalException:
        raise ValueError("金额和单价格式无效") from None


def validate_billing(raw):
    """Normalize public settings; no effective configuration remains ``{}``."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("费用设置必须是对象")
    allowed = {"max_cost_cny", "currency", *PRICE_FIELDS}
    if set(raw) - allowed:
        raise ValueError("费用设置包含未知字段")
    if raw.get("currency", "CNY") != "CNY":
        raise ValueError("费用设置仅支持人民币 CNY")
    normalized = {"currency": "CNY"}
    for field in ("max_cost_cny", *PRICE_FIELDS):
        value = raw.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            normalized[field] = None
        else:
            normalized[field] = float(_decimal(value, positive=field == "max_cost_cny"))
    if all(normalized[field] is None for field in ("max_cost_cny", *PRICE_FIELDS)):
        return {}
    if normalized["max_cost_cny"] is not None and any(
            normalized[field] is None for field in ("input_cache_miss_price_per_million", "output_price_per_million")):
        raise ValueError("启用金额上限时须填写缓存未命中输入单价和输出单价")
    return normalized


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _prices(billing):
    result = {key: Decimal(str(billing[key])) if billing.get(key) is not None else None for key in PRICE_FIELDS}
    hit = result[PRICE_FIELDS[0]]
    miss = result[PRICE_FIELDS[1]]
    # An omitted cache-hit price is an explicit conservative pricing policy,
    # not a fabricated claim that the request had no cache hits.
    result["hit"] = miss if hit is None else hit
    result["miss"] = miss
    result["output"] = result[PRICE_FIELDS[2]]
    available = [value for value in (result["hit"], miss) if value is not None]
    result["max_input"] = max(available) if available else None
    return result


def _usage(record):
    if not isinstance(record, dict):
        record = {}
    values = record.get("usage") if isinstance(record.get("usage"), dict) else record
    prompt = _number(values.get("prompt_tokens"))
    completion = _number(values.get("completion_tokens"))
    reliable = prompt is not None and completion is not None and record.get("usage_available") is not False
    hit = miss = None
    if "prompt_cache_hit_tokens" in values or "prompt_cache_miss_tokens" in values:
        hit = _number(values.get("prompt_cache_hit_tokens"))
        miss = _number(values.get("prompt_cache_miss_tokens"))
    else:
        details = values.get("prompt_tokens_details")
        if isinstance(details, dict):
            hit = _number(details.get("cached_tokens"))
            if hit is not None and prompt is not None and hit <= prompt:
                miss = prompt - hit
    cache_complete = (prompt is not None and hit is not None and miss is not None and hit + miss == prompt)
    if not cache_complete:
        # Retain valid partial counters for inspection, but never price them as
        # if they were a complete cache partition.
        return prompt, completion, reliable, hit, miss, False
    return prompt, completion, reliable, hit, miss, True


def _record_cost(record, prices):
    prompt, completion, reliable, hit, miss, cache_complete = _usage(record)
    input_cost = output_cost = None
    if prompt is not None:
        if cache_complete and prices["hit"] is not None and prices["miss"] is not None:
            input_cost = (hit * prices["hit"] + miss * prices["miss"]) / MILLION
        elif prices["hit"] is not None and prices["hit"] == prices["miss"]:
            input_cost = prompt * prices["miss"] / MILLION
    if completion is not None and prices["output"] is not None:
        output_cost = completion * prices["output"] / MILLION
    known = sum((value for value in (input_cost, output_cost) if value is not None), Decimal(0))
    exact = input_cost + output_cost if reliable and input_cost is not None and output_cost is not None else None
    conservative = exact
    if (conservative is None and reliable and prices["max_input"] is not None
            and prices["miss"] is not None and prices["output"] is not None):
        conservative = (prompt * prices["max_input"] + completion * prices["output"]) / MILLION
    return exact, known, conservative


def _public(value):
    return None if value is None else float(value)


def _record_amount(record, field):
    value = record.get(field) if isinstance(record, dict) else None
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value >= 0 else None
    except (DecimalException, ValueError):
        return None


def summarize_usage(records, billing=None):
    """Report actual, partial-known, and conservative amounts separately."""
    billing = validate_billing(billing)
    prices = _prices(billing)
    records = list(records)
    usages = [_usage(record) for record in records]
    costs = [_record_cost(record, prices) for record in records]
    usage_complete = all(usage[2] for usage in usages)
    prompt_complete = all(usage[0] is not None and usage[2] for usage in usages)
    completion_complete = all(usage[1] is not None and usage[2] for usage in usages)
    cost_complete = all(cost[0] is not None for cost in costs) and prices["miss"] is not None and prices["output"] is not None
    known_prompt = sum(usage[0] or 0 for usage in usages)
    known_completion = sum(usage[1] or 0 for usage in usages)
    cache_complete = all(usage[5] and usage[2] for usage in usages)
    known_hit = sum(usage[3] or 0 for usage in usages)
    known_miss = sum(usage[4] or 0 for usage in usages)
    exact = sum((cost[0] for cost in costs), Decimal(0)) if cost_complete else None
    known = sum((cost[1] for cost in costs), Decimal(0))
    accounted = []
    reserved = []
    for record, cost in zip(records, costs):
        amount = _record_amount(record, "budget_accounted_cost_cny")
        accounted.append(cost[2] if amount is None else amount)
        reserved.append(_record_amount(record, "budget_reserved_cost_cny"))
    accounted_complete = all(value is not None for value in accounted) and prices["miss"] is not None and prices["output"] is not None
    reason = ("按已提供的缓存命中、未命中和输出单价及实际用量估算；不是服务商账单。" if cost_complete else
              "单价、用量或缓存分项缺失，精确费用未知；已知部分与保守记账费用另列。")
    return {
        "requests": len(records),
        "successful_requests": sum(isinstance(record, dict) and record.get("status") == "ok" for record in records),
        "failed_requests": sum(not isinstance(record, dict) or record.get("status") != "ok" for record in records),
        "prompt_tokens": known_prompt if prompt_complete else None,
        "completion_tokens": known_completion if completion_complete else None,
        "total_tokens": known_prompt + known_completion if prompt_complete and completion_complete else None,
        "known_prompt_tokens": known_prompt, "known_completion_tokens": known_completion,
        "prompt_cache_hit_tokens": known_hit if cache_complete else None,
        "prompt_cache_miss_tokens": known_miss if cache_complete else None,
        "known_prompt_cache_hit_tokens": known_hit, "known_prompt_cache_miss_tokens": known_miss,
        "usage_complete": usage_complete, "cache_usage_complete": cache_complete,
        "usage_unknown_requests": sum(not usage[2] for usage in usages),
        "cache_usage_unknown_requests": sum(not usage[5] or not usage[2] for usage in usages),
        "cost_complete": cost_complete, "estimated_cost_cny": _public(exact), "estimated_cost": _public(exact),
        "known_cost_cny": _public(known),
        "accounted_cost_cny": _public(sum(accounted, Decimal(0))) if accounted_complete else None,
        "known_accounted_cost_cny": _public(sum((value for value in accounted if value is not None), Decimal(0))),
        "accounted_cost_complete": accounted_complete,
        "reserved_cost_cny": _public(sum((value for value in reserved if value is not None), Decimal(0))) if any(value is not None for value in reserved) else None,
        "currency": "CNY", "cost_note": reason, "billing": billing,
    }


@dataclass(frozen=True, repr=False)
class _Reservation:
    owner: str
    reservation_id: str
    prompt_tokens: int | None
    output_tokens: int | None
    cost: Decimal | None


class RunBudget:
    """A single job's in-memory reserve/settle guard, safe across threads."""

    def __init__(self, billing=None):
        self.billing = validate_billing(billing)
        self._prices = _prices(self.billing)
        ceiling = self.billing.get("max_cost_cny")
        self._limit = Decimal(str(ceiling)) if ceiling is not None else None
        self._owner = uuid.uuid4().hex
        self._pending = {}
        self._accounted = Decimal(0)
        self._unknown = 0
        self._settled = 0
        self._halted_reason = None
        self._lock = threading.RLock()

    def reserve(self, payload):
        with self._lock:
            if self._halted_reason:
                raise RunBudgetError("run_budget_unavailable")
            prompt = output = cost = None
            priced = self._prices["miss"] is not None and self._prices["output"] is not None
            if priced:
                try:
                    if not isinstance(payload, dict):
                        raise ValueError()
                    output = payload.get("max_tokens", payload.get("max_completion_tokens"))
                    if type(output) is not int or output <= 0:
                        raise ValueError()
                    prompt = len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) + INPUT_PADDING
                    cost = (prompt * self._prices["max_input"] + output * self._prices["output"]) / MILLION
                except (TypeError, ValueError, OverflowError, DecimalException):
                    if self._limit is not None:
                        raise RunBudgetError("run_budget_unavailable") from None
            pending = sum((item.cost for item in self._pending.values() if item.cost is not None), Decimal(0))
            if self._limit is not None and (cost is None or self._accounted + pending + cost > self._limit):
                raise RunBudgetError("cost_budget_exhausted")
            reservation = _Reservation(self._owner, uuid.uuid4().hex, prompt, output, cost)
            self._pending[reservation.reservation_id] = reservation
            return reservation

    def _require(self, reservation):
        if (not isinstance(reservation, _Reservation) or reservation.owner != self._owner
                or self._pending.get(reservation.reservation_id) != reservation):
            raise RunBudgetError("run_budget_unavailable")

    def cancel(self, reservation):
        """Release a reservation for a request that was never sent."""
        with self._lock:
            self._require(reservation)
            del self._pending[reservation.reservation_id]

    def settle(self, reservation, record):
        with self._lock:
            self._require(reservation)
            if not isinstance(record, dict):
                raise RunBudgetError("run_budget_unavailable")
            exact, _known, conservative = _record_cost(record, self._prices)
            if exact is not None:
                accounted, basis = exact, "actual_usage"
            elif conservative is not None:
                accounted, basis = conservative, "conservative_missing_cache_usage"
            else:
                accounted, basis = reservation.cost, "reserved_unknown_usage" if reservation.cost is not None else "unpriced"
            record["budget_reserved_cost_cny"] = _public(reservation.cost)
            record["budget_accounted_cost_cny"] = _public(accounted)
            record["budget_cost_basis"] = basis
            del self._pending[reservation.reservation_id]
            self._settled += 1
            if accounted is None:
                self._unknown += 1
            else:
                self._accounted += accounted
                if reservation.cost is not None and accounted > reservation.cost:
                    self._halted_reason = "actual_cost_exceeded_reservation"

    def summary(self):
        with self._lock:
            pending = sum((item.cost for item in self._pending.values() if item.cost is not None), Decimal(0))
            priced = self._prices["miss"] is not None and self._prices["output"] is not None
            accounted = self._accounted if not self._unknown and priced else None
            remaining = max(Decimal(0), self._limit - self._accounted - pending) if self._limit is not None else None
            return {"enabled": self._limit is not None, "currency": "CNY", "billing": dict(self.billing),
                    "max_cost_cny": _public(self._limit), "accounted_cost_cny": _public(accounted),
                    "spent_cost_cny": _public(accounted), "known_accounted_cost_cny": _public(self._accounted),
                    "reserved_cost_cny": _public(pending), "remaining_cost_cny": _public(remaining),
                    "settled_requests": self._settled, "pending_requests": len(self._pending),
                    "unpriced_requests": self._unknown, "halted_reason": self._halted_reason,
                    "assumption": ASSUMPTION}
