from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import RLock
import time

from telethon.tl.types import DocumentAttributeSticker

from config import (
    APPROVED_BE_STICKER_DOCUMENT_IDS,
    COMMENT,
    MAGIC_NUMBER,
    PRIMEBOT2_TELEGRAM_CHANNEL_ID,
    PROFITABLE_BREAK_EVEN_OFFSET,
    PROFITABLE_BREAK_EVEN_SYMBOL,
)
from core.break_even_storage import (
    load_break_even_state,
    save_break_even_state,
)
from core.logger import logger
from core.mt5_service import (
    POSITION_ABSENT,
    POSITION_OPEN,
    modify_trade,
    position_type_buy,
    position_type_sell,
    positions_get,
    query_position,
    symbol_info,
    symbol_info_tick,
    trade_retcode_invalid_stops,
)
from core.notifier import (
    notify_break_even_retry_summary,
    notify_break_even_summary,
    notify_error,
)
from core.runtime import is_auto_execute, is_paused


PENDING_RETRY_BASE_SECONDS = 15
PENDING_RETRY_MAX_SECONDS = 300
PENDING_WARNING_INTERVAL_SECONDS = 60

STATUS_MOVED = "moved"
STATUS_ALREADY_PROTECTED = "already_protected"
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"
STATUS_IGNORED = "ignored"
STATUS_SIMULATED = "simulated"
STATUS_CLOSED = "closed"

_ACTION_LOCK = RLock()
_pending_warnings = {}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _positive(value):
    numeric = _to_float(value)
    return numeric if numeric is not None and numeric > 0 else None


def _symbol_precision(info):
    digits = getattr(info, "digits", None)

    try:
        digits = int(digits)
    except (TypeError, ValueError):
        digits = None

    point = _positive(getattr(info, "point", None))

    if digits is None and point is not None:
        try:
            digits = max(0, -Decimal(str(point)).as_tuple().exponent)
        except InvalidOperation:
            digits = None

    if digits is None:
        digits = 2

    tick_size = (
        _positive(getattr(info, "trade_tick_size", None))
        or point
        or 10 ** -digits
    )
    point = point or 10 ** -digits
    return digits, tick_size, point


def normalize_price(price, info):
    digits, tick_size, _point = _symbol_precision(info)
    value = Decimal(str(price))
    step = Decimal(str(tick_size))
    ticks = (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    normalized = ticks * step
    quantum = Decimal("1").scaleb(-digits)
    return float(normalized.quantize(quantum, rounding=ROUND_HALF_UP))


def profitable_break_even_price(symbol, side, entry, info=None):
    if symbol != PROFITABLE_BREAK_EVEN_SYMBOL:
        raise ValueError(f"Profitable break-even is not approved for {symbol}")

    if info is None:
        info = symbol_info(symbol)

    if info is None:
        raise ValueError(f"Symbol information unavailable for {symbol}")

    normalized_entry = normalize_price(entry, info)

    if side == "BUY":
        target = normalized_entry + PROFITABLE_BREAK_EVEN_OFFSET
    elif side == "SELL":
        target = normalized_entry - PROFITABLE_BREAK_EVEN_OFFSET
    else:
        raise ValueError(f"Unknown position side: {side}")

    return normalize_price(target, info)


def _position_side(position):
    position_type = getattr(position, "type", None)

    try:
        if position_type == position_type_buy():
            return "BUY"

        if position_type == position_type_sell():
            return "SELL"
    except Exception:
        return None

    return None


def is_owned_primebot2_position(position):
    return (
        getattr(position, "symbol", None) == PROFITABLE_BREAK_EVEN_SYMBOL
        and getattr(position, "magic", None) == MAGIC_NUMBER
        and getattr(position, "comment", None) == COMMENT
    )


def _position_identity_key(position):
    ticket = _to_int(getattr(position, "ticket", None))
    identifier = _to_int(
        getattr(
            position,
            "identifier",
            getattr(position, "position_identifier", None),
        )
    )
    return f"{ticket}:{identifier if identifier is not None else ticket}"


def _protection_is_requested_or_better(side, current_sl, target_sl, tolerance):
    current_sl = _positive(current_sl)

    if current_sl is None:
        return False

    if side == "BUY":
        return current_sl >= target_sl - tolerance

    if side == "SELL":
        return current_sl <= target_sl + tolerance

    return False


def _broker_stop_is_valid(position, side, target_sl, info):
    symbol = getattr(position, "symbol", None)
    tick = symbol_info_tick(symbol)

    if tick is None:
        return False, "Current market price unavailable"

    _digits, tick_size, point = _symbol_precision(info)
    tolerance = max(tick_size / 2, 0.0000001)
    stops_level = max(_to_float(getattr(info, "trade_stops_level", 0)) or 0, 0)
    freeze_level = max(_to_float(getattr(info, "trade_freeze_level", 0)) or 0, 0)
    minimum_distance = max(stops_level, freeze_level) * point
    bid = _to_float(getattr(tick, "bid", None))
    ask = _to_float(getattr(tick, "ask", None))
    tp = _positive(getattr(position, "tp", None))

    if side == "BUY":
        if bid is None:
            return False, "Current bid unavailable"

        maximum_sl = bid - minimum_distance

        if target_sl > maximum_sl + tolerance:
            return (
                False,
                f"BUY stop {target_sl} is inside broker stop/freeze distance",
            )

        if tp is not None and target_sl >= tp - tolerance:
            return False, f"BUY stop {target_sl} is not below TP {tp}"

    elif side == "SELL":
        if ask is None:
            return False, "Current ask unavailable"

        minimum_sl = ask + minimum_distance

        if target_sl < minimum_sl - tolerance:
            return (
                False,
                f"SELL stop {target_sl} is inside broker stop/freeze distance",
            )

        if tp is not None and target_sl <= tp + tolerance:
            return False, f"SELL stop {target_sl} is not above TP {tp}"
    else:
        return False, "Unknown position side"

    return True, None


def _broker_rejected_stop(result):
    comment = str(result.get("comment", "")).lower()

    try:
        invalid_stops = trade_retcode_invalid_stops()
    except Exception:
        invalid_stops = None

    return (
        (
            invalid_stops is not None
            and result.get("retcode") == invalid_stops
        )
        or "invalid stop" in comment
        or "invalid sl" in comment
        or "freeze" in comment
        or "stop level" in comment
    )


def apply_profitable_break_even(position, dry_run=False):
    ticket = getattr(position, "ticket", None)

    if not is_owned_primebot2_position(position):
        return {
            "status": STATUS_IGNORED,
            "ticket": ticket,
            "reason": "Foreign/manual position",
        }

    side = _position_side(position)

    if side is None:
        return {
            "status": STATUS_FAILED,
            "ticket": ticket,
            "reason": "Unknown position side",
        }

    entry = _to_float(getattr(position, "price_open", None))

    if entry is None:
        return {
            "status": STATUS_FAILED,
            "ticket": ticket,
            "side": side,
            "reason": "MT5 open price unavailable",
        }

    try:
        info = symbol_info(PROFITABLE_BREAK_EVEN_SYMBOL)
        target_sl = profitable_break_even_price(
            PROFITABLE_BREAK_EVEN_SYMBOL,
            side,
            entry,
            info=info,
        )
    except (ArithmeticError, InvalidOperation, TypeError, ValueError) as exc:
        return {
            "status": STATUS_PENDING,
            "ticket": ticket,
            "side": side,
            "reason": str(exc),
        }

    _digits, tick_size, _point = _symbol_precision(info)
    current_sl = _to_float(getattr(position, "sl", None))

    if _protection_is_requested_or_better(
        side,
        current_sl,
        target_sl,
        tolerance=max(tick_size / 2, 0.0000001),
    ):
        return {
            "status": STATUS_ALREADY_PROTECTED,
            "ticket": ticket,
            "side": side,
            "target_sl": target_sl,
            "current_sl": current_sl,
        }

    if dry_run:
        try:
            valid, reason = _broker_stop_is_valid(position, side, target_sl, info)
        except Exception as exc:
            valid = False
            reason = f"Broker stop validation unavailable: {exc}"

        return {
            "status": STATUS_SIMULATED,
            "ticket": ticket,
            "side": side,
            "target_sl": target_sl,
            "current_sl": current_sl,
            "broker_valid_now": valid,
            "reason": reason,
        }

    try:
        valid, reason = _broker_stop_is_valid(position, side, target_sl, info)
    except Exception as exc:
        valid = False
        reason = f"Broker stop validation unavailable: {exc}"

    if not valid:
        return {
            "status": STATUS_PENDING,
            "ticket": ticket,
            "side": side,
            "target_sl": target_sl,
            "current_sl": current_sl,
            "reason": reason,
        }

    try:
        result = modify_trade(
            ticket,
            sl=target_sl,
            expected_symbol=PROFITABLE_BREAK_EVEN_SYMBOL,
            expected_magic=MAGIC_NUMBER,
            expected_comment=COMMENT,
        )
    except Exception as exc:
        return {
            "status": STATUS_FAILED,
            "ticket": ticket,
            "side": side,
            "target_sl": target_sl,
            "current_sl": current_sl,
            "reason": f"MT5 modification failed: {exc}",
        }

    if result.get("success"):
        return {
            "status": (
                STATUS_ALREADY_PROTECTED
                if result.get("noop")
                else STATUS_MOVED
            ),
            "ticket": ticket,
            "side": side,
            "target_sl": target_sl,
            "current_sl": current_sl,
            "mt5_result": result,
        }

    status = STATUS_PENDING if _broker_rejected_stop(result) else STATUS_FAILED
    return {
        "status": status,
        "ticket": ticket,
        "side": side,
        "target_sl": target_sl,
        "current_sl": current_sl,
        "reason": result.get("comment") or "MT5 modification failed",
        "mt5_result": result,
    }


def _event_document(event):
    direct = getattr(event, "document", None)

    if direct is not None:
        return direct

    message = getattr(event, "message", None)
    document = getattr(message, "document", None) if message is not None else None

    if document is not None:
        return document

    media = getattr(event, "media", None)
    return getattr(media, "document", None)


def _is_sticker_document(document):
    attributes = getattr(document, "attributes", None) or ()
    return any(isinstance(attribute, DocumentAttributeSticker) for attribute in attributes)


def approved_break_even_sticker(event):
    try:
        if int(getattr(event, "chat_id", 0)) != PRIMEBOT2_TELEGRAM_CHANNEL_ID:
            return None
    except (TypeError, ValueError):
        return None

    document = _event_document(event)

    if document is None or not _is_sticker_document(document):
        return None

    document_id = _to_int(getattr(document, "id", None))

    if document_id not in APPROVED_BE_STICKER_DOCUMENT_IDS:
        return None

    return document


def _action_key(chat_id, message_id):
    return f"{int(chat_id)}:{int(message_id)}"


def _pending_delay(attempts):
    return min(
        PENDING_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)),
        PENDING_RETRY_MAX_SECONDS,
    )


def _pending_record(position, result, source_action_key=None, source="sticker"):
    key = _position_identity_key(position)
    sources = [source_action_key] if source_action_key else []
    return key, {
        "ticket": _to_int(getattr(position, "ticket", None)),
        "identifier": _to_int(
            getattr(
                position,
                "identifier",
                getattr(position, "position_identifier", None),
            )
        ),
        "symbol": PROFITABLE_BREAK_EVEN_SYMBOL,
        "magic": MAGIC_NUMBER,
        "comment": COMMENT,
        "target_sl": result.get("target_sl"),
        "side": result.get("side"),
        "source": source,
        "source_action_keys": sources,
        "attempts": 1,
        "next_retry_at": time.time() + _pending_delay(1),
        "reason": result.get("reason"),
        "created_at": _now(),
        "updated_at": _now(),
    }


def _set_action_position_status(state, pending, result):
    for action_key in pending.get("source_action_keys", []):
        action = state.get("actions", {}).get(action_key)

        if action is None:
            continue

        position_result = action.get("position_results", {}).get(
            pending.get("position_key")
        )

        if position_result is not None:
            position_result.update(result)
            position_result["updated_at"] = _now()


def _merge_pending(state, position, result, source_action_key=None, source="sticker"):
    key, prepared = _pending_record(
        position,
        result,
        source_action_key=source_action_key,
        source=source,
    )
    prepared["position_key"] = key
    existing = state["pending"].get(key)

    if existing is not None:
        prepared["created_at"] = existing.get("created_at", prepared["created_at"])
        prepared["attempts"] = existing.get("attempts", prepared["attempts"])
        prepared["next_retry_at"] = existing.get(
            "next_retry_at",
            prepared["next_retry_at"],
        )
        prepared["source_action_keys"] = list(dict.fromkeys(
            list(existing.get("source_action_keys", []))
            + list(prepared.get("source_action_keys", []))
        ))

    state["pending"][key] = prepared
    return key


def _resolve_existing_pending(state, position, result):
    key = _position_identity_key(position)
    pending = state["pending"].pop(key, None)

    if pending is not None:
        _set_action_position_status(state, pending, result)


def _action_summary(action):
    results = list(action.get("position_results", {}).values())
    status_counts = {}

    for result in results:
        status = result.get("status")
        status_counts[status] = status_counts.get(status, 0) + 1

    ignored = status_counts.get(STATUS_IGNORED, 0)
    owned = max(action.get("positions_discovered", 0) - ignored, 0)
    return {
        "mode": action.get("mode"),
        "chat_id": action.get("chat_id"),
        "message_id": action.get("message_id"),
        "document_id": action.get("document_id"),
        "positions_discovered": action.get("positions_discovered", 0),
        "primebot2_positions": owned,
        "moved": status_counts.get(STATUS_MOVED, 0),
        "already_protected": status_counts.get(STATUS_ALREADY_PROTECTED, 0),
        "pending": status_counts.get(STATUS_PENDING, 0),
        "failed": status_counts.get(STATUS_FAILED, 0),
        "ignored": ignored,
        "simulated": status_counts.get(STATUS_SIMULATED, 0),
        "closed": status_counts.get(STATUS_CLOSED, 0),
        "no_primebot2_positions": (
            owned == 0
            and action.get("mode") != "paused"
            and not action.get("query_failed", False)
        ),
        "paused": action.get("mode") == "paused",
    }


def _record_paused_action(state, key, event, document, message_id):
    action = {
        "chat_id": int(event.chat_id),
        "message_id": int(message_id),
        "document_id": int(document.id),
        "mode": "paused",
        "positions_discovered": 0,
        "position_results": {},
        "created_at": _now(),
        "completed_at": _now(),
    }
    state["actions"][key] = action
    return action


def handle_break_even_sticker(event):
    document = approved_break_even_sticker(event)

    if document is None:
        return False

    message_id = getattr(event, "id", getattr(event, "message_id", None))

    if message_id is None:
        logger.warning("Approved break-even sticker has no Telegram message ID")
        return True

    key = _action_key(event.chat_id, message_id)

    with _ACTION_LOCK:
        state = load_break_even_state()

        if state is None:
            notify_error("Break-even sticker ignored: durable action state is unreadable")
            return True

        if key in state["actions"]:
            logger.info(
                "Duplicate break-even sticker delivery ignored | "
                f"ChatID={event.chat_id} Message={message_id}"
            )
            return True

        if is_paused():
            action = _record_paused_action(
                state,
                key,
                event,
                document,
                message_id,
            )

            if not save_break_even_state(state):
                notify_error("Break-even sticker result could not be stored durably")
                return True

            notify_break_even_summary(_action_summary(action))
            return True

        live_mode = is_auto_execute()
        action = {
            "chat_id": int(event.chat_id),
            "message_id": int(message_id),
            "document_id": int(document.id),
            "mode": "live" if live_mode else "dry_run",
            "positions_discovered": 0,
            "position_results": {},
            "created_at": _now(),
        }

        try:
            open_positions = positions_get()

            if open_positions is None:
                raise RuntimeError("MT5 open-position query failed")

            open_positions = list(open_positions)
            action["positions_discovered"] = len(open_positions)

            for position in open_positions:
                result = apply_profitable_break_even(
                    position,
                    dry_run=not live_mode,
                )
                position_key = _position_identity_key(position)
                action["position_results"][position_key] = dict(result)

                if live_mode and result.get("status") == STATUS_PENDING:
                    _merge_pending(
                        state,
                        position,
                        result,
                        source_action_key=key,
                        source="sticker",
                    )
                elif live_mode and result.get("status") in (
                    STATUS_MOVED,
                    STATUS_ALREADY_PROTECTED,
                ):
                    _resolve_existing_pending(state, position, result)
        except Exception as exc:
            logger.exception("Break-even sticker position processing failed")
            action["query_failed"] = True
            action["position_results"]["query"] = {
                "status": STATUS_FAILED,
                "ticket": None,
                "reason": str(exc),
            }

        action["completed_at"] = _now()
        state["actions"][key] = action

        if not save_break_even_state(state):
            notify_error("Break-even sticker result could not be stored durably")
            return True

        notify_break_even_summary(_action_summary(action))
        return True


def record_automatic_pending(position, result, source_key):
    with _ACTION_LOCK:
        state = load_break_even_state()

        if state is None:
            return False

        if result.get("status") == STATUS_PENDING:
            _merge_pending(
                state,
                position,
                result,
                source_action_key=None,
                source=f"automatic_tp1:{source_key}",
            )
        elif result.get("status") in (STATUS_MOVED, STATUS_ALREADY_PROTECTED):
            _resolve_existing_pending(state, position, result)
        else:
            return True

        return save_break_even_state(state)


def has_pending_break_even(position):
    state = load_break_even_state()
    return bool(
        state is not None
        and _position_identity_key(position) in state.get("pending", {})
    )


def _warn_pending(key, reason):
    now = time.monotonic()
    previous = _pending_warnings.get(key)

    if (
        previous is not None
        and previous.get("reason") == reason
        and now - previous.get("logged_at", 0) < PENDING_WARNING_INTERVAL_SECONDS
    ):
        return

    logger.warning(
        "Profitable break-even remains pending | "
        f"Position={key} Reason={reason}"
    )
    _pending_warnings[key] = {"reason": reason, "logged_at": now}


def _retry_result_summary():
    return {
        "retried": 0,
        "moved": 0,
        "already_protected": 0,
        "pending": 0,
        "closed": 0,
        "failed": 0,
        "skipped": 0,
    }


def retry_pending_break_even_actions(force=False):
    summary = _retry_result_summary()

    with _ACTION_LOCK:
        state = load_break_even_state()

        if state is None or not state.get("pending"):
            return summary

        if is_paused() or not is_auto_execute():
            summary["skipped"] = len(state["pending"])
            summary["pending"] = len(state["pending"])
            return summary

        for key in list(state["pending"]):
            pending = state["pending"].get(key)

            if pending is None:
                continue

            if not force and time.time() < pending.get("next_retry_at", 0):
                summary["skipped"] += 1
                summary["pending"] += 1
                continue

            summary["retried"] += 1
            query = query_position(pending.get("ticket"))

            if query.status == POSITION_ABSENT:
                result = {
                    "status": STATUS_CLOSED,
                    "ticket": pending.get("ticket"),
                    "reason": "Position closed before break-even retry",
                }
                _set_action_position_status(state, pending, result)
                state["pending"].pop(key, None)
                _pending_warnings.pop(key, None)
                summary["closed"] += 1
            elif query.status != POSITION_OPEN:
                reason = query.error or f"Position query status {query.status}"
                attempts = int(pending.get("attempts", 0)) + 1
                pending["attempts"] = attempts
                pending["next_retry_at"] = time.time() + _pending_delay(attempts)
                pending["reason"] = reason
                pending["updated_at"] = _now()
                _warn_pending(key, reason)
                summary["pending"] += 1
            else:
                live_position = query.position

                if not is_owned_primebot2_position(live_position):
                    result = {
                        "status": STATUS_FAILED,
                        "ticket": pending.get("ticket"),
                        "reason": "Ownership mismatch during pending retry",
                    }
                    _set_action_position_status(state, pending, result)
                    state["pending"].pop(key, None)
                    _pending_warnings.pop(key, None)
                    summary["failed"] += 1
                else:
                    result = apply_profitable_break_even(live_position, dry_run=False)
                    status = result.get("status")

                    if status == STATUS_PENDING:
                        attempts = int(pending.get("attempts", 0)) + 1
                        pending["attempts"] = attempts
                        pending["next_retry_at"] = (
                            time.time() + _pending_delay(attempts)
                        )
                        pending["reason"] = result.get("reason")
                        pending["target_sl"] = result.get("target_sl")
                        pending["updated_at"] = _now()
                        _warn_pending(key, pending["reason"])
                        summary["pending"] += 1
                    else:
                        _set_action_position_status(state, pending, result)
                        state["pending"].pop(key, None)
                        _pending_warnings.pop(key, None)

                        if status == STATUS_MOVED:
                            summary["moved"] += 1
                        elif status == STATUS_ALREADY_PROTECTED:
                            summary["already_protected"] += 1
                        else:
                            summary["failed"] += 1

            if not save_break_even_state(state):
                logger.error("Pending break-even retry result was not stored durably")
                break

    if summary["retried"]:
        notify_break_even_retry_summary(summary)

    return summary
