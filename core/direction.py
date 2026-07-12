from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Optional, Tuple


DIRECTION_SOURCE = "sl_tp_geometry"
BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class DirectionInference:
    valid: bool
    inferred_direction: Optional[str] = None
    final_side: Optional[str] = None
    direction_source: Optional[str] = None
    direction_conflict: bool = False
    reason: Optional[str] = None
    sl: Optional[float] = None
    tps: Tuple[float, ...] = ()
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None


@dataclass(frozen=True)
class NowPriceValidation:
    valid: bool
    price: Optional[float] = None
    nearest_tp: Optional[float] = None
    reason: Optional[str] = None


def _safe_float(value, label):
    if isinstance(value, bool):
        return None, f"{label} is not numeric"

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, f"{label} is not numeric"

    if not isfinite(number):
        return None, f"{label} must be finite"

    return number, None


def _normalize_direction(direction):
    if direction is None:
        return None

    value = str(direction).strip().upper()

    if value in (BUY, SELL):
        return value

    return None


def _normalize_tps(tps: Iterable):
    if tps is None:
        return (), "At least one valid TP is required"

    normalized = []
    seen = set()

    for index, value in enumerate(tps, start=1):
        tp, reason = _safe_float(value, f"TP{index}")

        if reason:
            return (), reason

        if tp in seen:
            return (), f"Duplicate TP value {tp} is invalid"

        normalized.append(tp)
        seen.add(tp)

    if not normalized:
        return (), "At least one valid TP is required"

    return tuple(normalized), None


def normalize_entry_range(entry_low=None, entry_high=None):
    if entry_low is None and entry_high is None:
        return None, None, None

    if entry_low is None:
        entry_low = entry_high

    if entry_high is None:
        entry_high = entry_low

    low, reason = _safe_float(entry_low, "Entry low")

    if reason:
        return None, None, reason

    high, reason = _safe_float(entry_high, "Entry high")

    if reason:
        return None, None, reason

    return min(low, high), max(low, high), None


def _infer_from_sl_and_tps(sl, tps):
    if all(sl < tp for tp in tps):
        return BUY, None

    if all(sl > tp for tp in tps):
        return SELL, None

    return (
        None,
        "SL/TP geometry is ambiguous: SL must be strictly below every TP "
        "for BUY or strictly above every TP for SELL"
    )


def _validate_entry_geometry(direction, sl, tps, entry_low, entry_high):
    low, high, reason = normalize_entry_range(entry_low, entry_high)

    if reason:
        return None, None, reason

    if low is None and high is None:
        return None, None, None

    if direction == BUY:
        nearest_tp = min(tps)

        if sl < low <= high < nearest_tp:
            return low, high, None

        return (
            low,
            high,
            "Entry geometry invalid for BUY: expected "
            "SL < entry_low <= entry_high < nearest TP"
        )

    nearest_tp = max(tps)

    if nearest_tp < low <= high < sl:
        return low, high, None

    return (
        low,
        high,
        "Entry geometry invalid for SELL: expected "
        "nearest TP < entry_low <= entry_high < SL"
    )


def infer_direction(sl, tps, entry_low=None, entry_high=None, textual_direction=None):
    normalized_sl, reason = _safe_float(sl, "SL")

    if reason:
        return DirectionInference(valid=False, reason=reason)

    normalized_tps, reason = _normalize_tps(tps)

    if reason:
        return DirectionInference(valid=False, reason=reason, sl=normalized_sl)

    inferred_direction, reason = _infer_from_sl_and_tps(
        normalized_sl,
        normalized_tps
    )

    if reason:
        return DirectionInference(
            valid=False,
            reason=reason,
            sl=normalized_sl,
            tps=normalized_tps,
            direction_source=DIRECTION_SOURCE,
        )

    normalized_text_direction = _normalize_direction(textual_direction)
    direction_conflict = (
        normalized_text_direction is not None
        and normalized_text_direction != inferred_direction
    )
    normalized_entry_low, normalized_entry_high, reason = _validate_entry_geometry(
        inferred_direction,
        normalized_sl,
        normalized_tps,
        entry_low,
        entry_high,
    )

    if reason:
        return DirectionInference(
            valid=False,
            inferred_direction=inferred_direction,
            reason=reason,
            sl=normalized_sl,
            tps=normalized_tps,
            entry_low=normalized_entry_low,
            entry_high=normalized_entry_high,
            direction_source=DIRECTION_SOURCE,
            direction_conflict=direction_conflict,
        )

    return DirectionInference(
        valid=True,
        inferred_direction=inferred_direction,
        final_side=inferred_direction,
        sl=normalized_sl,
        tps=normalized_tps,
        entry_low=normalized_entry_low,
        entry_high=normalized_entry_high,
        direction_source=DIRECTION_SOURCE,
        direction_conflict=direction_conflict,
    )


def validate_now_price(side, sl, tps, price):
    normalized_side = _normalize_direction(side)

    if normalized_side is None:
        return NowPriceValidation(
            valid=False,
            reason="NOW price validation requires a BUY or SELL side"
        )

    normalized_sl, reason = _safe_float(sl, "SL")

    if reason:
        return NowPriceValidation(valid=False, reason=reason)

    normalized_tps, reason = _normalize_tps(tps)

    if reason:
        return NowPriceValidation(valid=False, reason=reason)

    normalized_price, reason = _safe_float(price, "NOW executable price")

    if reason:
        return NowPriceValidation(valid=False, reason=reason)

    if normalized_side == BUY:
        nearest_tp = min(normalized_tps)

        if normalized_sl < normalized_price < nearest_tp:
            return NowPriceValidation(
                valid=True,
                price=normalized_price,
                nearest_tp=nearest_tp,
            )

        return NowPriceValidation(
            valid=False,
            price=normalized_price,
            nearest_tp=nearest_tp,
            reason=(
                "NOW executable price invalid for BUY: expected "
                "SL < ask < nearest TP"
            )
        )

    nearest_tp = max(normalized_tps)

    if nearest_tp < normalized_price < normalized_sl:
        return NowPriceValidation(
            valid=True,
            price=normalized_price,
            nearest_tp=nearest_tp,
        )

    return NowPriceValidation(
        valid=False,
        price=normalized_price,
        nearest_tp=nearest_tp,
        reason=(
            "NOW executable price invalid for SELL: expected "
            "nearest TP < bid < SL"
        )
    )
