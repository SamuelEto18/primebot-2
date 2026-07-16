from datetime import datetime
import time

from config import MOVE_TO_BREAK_EVEN

from core.break_even import (
    STATUS_ALREADY_PROTECTED,
    STATUS_MOVED,
    STATUS_PENDING,
    apply_profitable_break_even,
    has_pending_break_even,
    record_automatic_pending,
    retry_pending_break_even_actions,
)

from core.lifecycle import wait_for_shutdown
from core.mt5_service import (
    POSITION_ABSENT,
    POSITION_OPEN,
    confirm_position_closed,
    confirm_position_closed_by_tp,
    modify_trade,  # Compatibility export; BE execution uses the shared helper.
    query_position,
    recover_pending_position_identity,
)
from core.notifier import (
    notify_break_even,
    notify_error,
    notify_identity_recovered,
    notify_position_closed,
    notify_signal_archived,
)
from core.logger import logger
from core.runtime import (
    mark_position_manager_error,
    mark_position_manager_heartbeat,
    mark_position_manager_started,
    mark_position_manager_stopped
)
from core.trade_storage import (
    archive_trade,
    finalize_pending_identity,
    has_pending_for_trade,
    load_pending_identities,
    load_trades,
    stored_position_for_pending,
    update_trade
)

PENDING_HISTORY_WARNING_INTERVAL_SECONDS = 30
PENDING_IDENTITY_RETRY_BASE_SECONDS = 15
PENDING_IDENTITY_RETRY_MAX_SECONDS = 300
PENDING_IDENTITY_WARNING_INTERVAL_SECONDS = 60
_pending_close_history_warnings = {}
_pending_identity_recovery_state = {}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _position_entry(position, stored_position):

    if position is not None and getattr(position, "price_open", None) is not None:
        return getattr(position, "price_open")

    for key in ("price_open", "fill_price", "entry"):
        value = stored_position.get(key)

        if value is not None:
            return value

    return stored_position.get("entry")


def _position_ticket(stored_position):

    return stored_position.get("position_ticket", stored_position["ticket"])


def _position_identifier(stored_position):

    return stored_position.get(
        "position_identifier",
        stored_position.get("position_id")
    )


def _identity_value(value):

    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _to_float(value):

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_number(left, right, tolerance=0.00001):

    left = _to_float(left)
    right = _to_float(right)

    if left is None or right is None:
        return False

    return abs(left - right) <= tolerance


def _position_expected_volume(stored_position):

    return (
        stored_position.get("original_volume")
        or stored_position.get("initial_volume")
        or stored_position.get("volume")
    )


def _sync_open_position_volume(stored_position, position):

    if position is None:
        return False

    live_volume = _to_float(getattr(position, "volume", None))

    if live_volume is None or live_volume <= 0:
        return False

    stored_volume = _to_float(stored_position.get("volume"))

    if stored_volume is not None and _same_number(stored_volume, live_volume):
        return False

    original_volume = _position_expected_volume(stored_position)

    if original_volume is not None and "original_volume" not in stored_position:
        stored_position["original_volume"] = original_volume

    stored_position["volume"] = live_volume
    stored_position["remaining_volume"] = live_volume
    return True


def _trade_side(trade):

    if trade is None:
        return None

    return (
        trade.get("side")
        or trade.get("final_side")
        or trade.get("textual_direction")
    )


def _close_confirmation_context(stored_position, trade=None):

    return {
        "symbol": stored_position.get("symbol") or (trade or {}).get("symbol"),
        "side": stored_position.get("side") or _trade_side(trade),
        "opening_deal_ticket": stored_position.get("deal_ticket"),
        "opening_order_ticket": stored_position.get("order_ticket"),
        "expected_magic": stored_position.get("magic"),
        "expected_comment": stored_position.get("comment"),
    }


def _clear_pending_close_history_warning(ticket):

    _pending_close_history_warnings.pop(ticket, None)


def _log_pending_close_history_warning(ticket, reason):

    now = time.monotonic()
    previous = _pending_close_history_warnings.get(ticket)

    if (
        previous is not None
        and previous.get("reason") == reason
        and now - previous.get("logged_at", 0) < PENDING_HISTORY_WARNING_INTERVAL_SECONDS
    ):
        return

    logger.warning(
        f"Position absent but close history pending | "
        f"Ticket={ticket} Reason={reason}"
    )
    _pending_close_history_warnings[ticket] = {
        "reason": reason,
        "logged_at": now,
    }


def _assigned_position_identities():
    tickets = set()
    identifiers = set()

    for trade in load_trades():
        for position in trade.get("positions", []):
            if position.get("closed"):
                continue

            ticket = _identity_value(
                position.get("position_ticket", position.get("ticket"))
            )
            identifier = _identity_value(
                position.get("position_identifier", position.get("position_id"))
            )

            if ticket is not None:
                tickets.add(ticket)

            if identifier is not None:
                identifiers.add(identifier)

    return tickets, identifiers


def _position_record_from_recovery(record, result):
    position = {
        "ticket": result["ticket"],
        "position_ticket": result.get("position_ticket", result["ticket"]),
        "position_identifier": result.get("position_identifier"),
        "position_id": result.get("position_id"),
        "order_ticket": result.get("order_ticket") or record.get("order_ticket"),
        "deal_ticket": result.get("deal_ticket") or record.get("deal_ticket"),
        "symbol": result.get("symbol", record.get("symbol")),
        "side": result.get("side", record.get("side")),
        "volume": result.get("volume", record.get("volume")),
        "requested_price": result.get("requested_price", record.get("requested_price")),
        "fill_price": result.get("fill_price", record.get("fill_price")),
        "price_open": result.get("price_open"),
        "entry": result["price"],
        "entry_source": result.get("entry_source"),
        "sl": result.get("sl", record.get("sl")),
        "tp": record.get("tp"),
        "magic": result.get("magic", record.get("magic")),
        "comment": result.get("comment", record.get("comment")),
        "chat_id": record.get("chat_id"),
        "message_id": record.get("message_id"),
        "tp_index": record.get("tp_index"),
        "order_attempt_started_at": (
            result.get("order_attempt_started_at")
            or record.get("order_attempt_started_at")
        ),
        "identity_status": "resolved",
        "identity_resolution": result.get("identity_resolution"),
        "recovered_pending_id": _pending_retry_key(record),
        "closed": bool(result.get("closed")),
        "break_even": result.get("break_even", False),
    }

    for key in (
        "opened_at",
        "close_deal_ticket",
        "close_order_ticket",
        "close_price",
        "close_volume",
        "close_reason",
        "close_profit",
        "profit",
        "closed_at",
        "take_profit_confirmed",
    ):
        if key in result:
            position[key] = result[key]

    return position


def _pending_retry_key(record):

    return record.get("pending_id") or ":".join(str(record.get(key)) for key in (
        "chat_id",
        "message_id",
        "tp_index",
        "order_ticket",
        "deal_ticket",
        "tp",
    ))


def _schedule_pending_identity_retry(record, reason):

    key = _pending_retry_key(record)
    now = time.monotonic()
    previous = _pending_identity_recovery_state.get(key, {})
    attempts = previous.get("attempts", 0) + 1
    delay = min(
        PENDING_IDENTITY_RETRY_BASE_SECONDS * (2 ** (attempts - 1)),
        PENDING_IDENTITY_RETRY_MAX_SECONDS,
    )
    last_warning_at = previous.get("last_warning_at", 0)
    previous_reason = previous.get("reason")

    if (
        previous_reason != reason
        or now - last_warning_at >= PENDING_IDENTITY_WARNING_INTERVAL_SECONDS
    ):
        logger.warning(
            "Pending position identity remains unresolved | "
            f"Order={record.get('order_ticket')} "
            f"Deal={record.get('deal_ticket')} "
            f"RetryIn={delay}s Reason={reason}"
        )
        last_warning_at = now

    _pending_identity_recovery_state[key] = {
        "attempts": attempts,
        "next_attempt_at": now + delay,
        "last_warning_at": last_warning_at,
        "reason": reason,
    }


def _pending_retry_is_due(record, now):

    state = _pending_identity_recovery_state.get(_pending_retry_key(record))
    return state is None or now >= state.get("next_attempt_at", 0)


def recover_pending_identities_once(chat_id=None, message_id=None):
    recovered = 0
    unresolved = 0
    skipped = 0
    records = load_pending_identities()
    active_retry_keys = {_pending_retry_key(record) for record in records}

    for key in list(_pending_identity_recovery_state):
        if key not in active_retry_keys:
            _pending_identity_recovery_state.pop(key, None)

    for record in records:
        if chat_id is not None and record.get("chat_id") != chat_id:
            continue

        if message_id is not None and record.get("message_id") != message_id:
            continue

        retry_key = _pending_retry_key(record)
        if not _pending_retry_is_due(record, time.monotonic()):
            unresolved += 1
            skipped += 1
            continue

        stored_position = stored_position_for_pending(record)
        if stored_position is not None:
            trade, _added, finalized = finalize_pending_identity(
                record,
                stored_position,
            )

            if not finalized:
                unresolved += 1
                _schedule_pending_identity_retry(
                    record,
                    "pending cleanup after durable storage failed",
                )
                continue

            _pending_identity_recovery_state.pop(retry_key, None)
            recovered += 1

            if (
                stored_position.get("closed")
                and trade is not None
                and all(item.get("closed") for item in trade.get("positions", []))
                and not has_pending_for_trade(
                    trade.get("chat_id"),
                    trade.get("message_id"),
                )
            ):
                if archive_trade(trade):
                    notify_signal_archived(trade)

            continue

        assigned_tickets, assigned_identifiers = _assigned_position_identities()
        result = recover_pending_position_identity(
            record,
            excluded_position_tickets=assigned_tickets,
            excluded_position_identifiers=assigned_identifiers,
        )

        if not result.get("success"):
            unresolved += 1
            reason = (
                result.get("comment")
                or result.get("result_comment")
                or "broker identity/history not yet available"
            )
            _schedule_pending_identity_retry(record, reason)
            continue

        position = _position_record_from_recovery(record, result)
        trade, added, finalized = finalize_pending_identity(record, position)

        if not finalized:
            unresolved += 1
            _schedule_pending_identity_retry(record, "durable storage update failed")
            continue

        _pending_identity_recovery_state.pop(retry_key, None)

        if added:
            notify_identity_recovered(position)

        recovered += 1

        if (
            position.get("closed")
            and trade is not None
            and all(item.get("closed") for item in trade.get("positions", []))
            and not has_pending_for_trade(trade.get("chat_id"), trade.get("message_id"))
        ):
            if archive_trade(trade):
                notify_signal_archived(trade)

    return {
        "recovered": recovered,
        "unresolved": unresolved,
        "skipped": skipped,
    }


def _confirm_tp1_take_profit(tp1):

    ticket = _position_ticket(tp1)
    result = query_position(ticket)

    if result.status == POSITION_OPEN:
        return False

    if result.status != POSITION_ABSENT:
        logger.warning(
            f"TP1 close check skipped | "
            f"Ticket={ticket} Status={result.status} Error={result.error}"
        )
        return False

    confirmation = confirm_position_closed_by_tp(
        ticket,
        tp1.get("tp"),
        position_identifier=_position_identifier(tp1),
        expected_volume=_position_expected_volume(tp1),
        **_close_confirmation_context(tp1)
    )

    if confirmation["confirmed"]:
        return True

    logger.warning(
        f"TP1 absent but take-profit close not confirmed | "
        f"Ticket={ticket} Reason={confirmation['reason']}"
    )

    return False


def _apply_close_metadata(stored_position, confirmation):
    metadata = dict(confirmation.get("metadata") or {})
    close_reason = (
        metadata.get("close_reason")
        or confirmation.get("close_reason")
        or confirmation.get("reason")
        or "unknown_confirmed_close"
    )

    stored_position["closed"] = True
    stored_position["closed_at"] = metadata.get("closed_at") or _now()
    stored_position["close_price"] = metadata.get("close_price")
    stored_position["close_reason"] = close_reason
    stored_position["close_deal_ticket"] = metadata.get("close_deal_ticket")
    stored_position["close_order_ticket"] = metadata.get("close_order_ticket")
    stored_position["close_volume"] = metadata.get("close_volume")
    stored_position["take_profit_confirmed"] = (
        metadata.get("take_profit_confirmed")
        if "take_profit_confirmed" in metadata
        else close_reason == "take_profit"
    )


def _reconcile_position(stored_position, trade=None):
    ticket = _position_ticket(stored_position)
    result = query_position(ticket)

    if result.status == POSITION_OPEN:
        _clear_pending_close_history_warning(ticket)
        return _sync_open_position_volume(stored_position, result.position)

    if result.status != POSITION_ABSENT:
        logger.warning(
            f"Position close check skipped | "
            f"Ticket={ticket} Status={result.status} Error={result.error}"
        )
        return False

    confirmation = confirm_position_closed(
        ticket,
        tp=stored_position.get("tp"),
        position_identifier=_position_identifier(stored_position),
        expected_volume=_position_expected_volume(stored_position),
        **_close_confirmation_context(stored_position, trade)
    )

    if confirmation.get("pending"):
        _log_pending_close_history_warning(ticket, confirmation.get("reason"))
        return False

    _clear_pending_close_history_warning(ticket)

    if not confirmation.get("confirmed"):
        logger.warning(
            f"Position absent but close not confirmed | "
            f"Ticket={ticket} Reason={confirmation.get('reason')}"
        )
        return False

    _apply_close_metadata(stored_position, confirmation)
    notify_position_closed(stored_position)
    return True


def _move_remaining_to_break_even(trade):
    changed = False

    if not MOVE_TO_BREAK_EVEN:
        return changed

    if len(trade.get("positions", [])) < 2:
        return changed

    tp1 = trade["positions"][0]

    if not tp1.get("take_profit_confirmed"):
        return changed

    for position in trade["positions"][1:]:
        if position.get("closed"):
            continue

        if position.get("break_even"):
            continue

        ticket = _position_ticket(position)
        position_result = query_position(ticket)

        if position_result.status != POSITION_OPEN:
            logger.warning(
                f"Break-even skipped for ticket {ticket} | "
                f"Status={position_result.status} Error={position_result.error}"
            )
            continue

        if has_pending_break_even(position_result.position):
            continue

        result = apply_profitable_break_even(
            position_result.position,
            dry_run=False,
        )

        source_key = (
            f"{trade.get('chat_id')}:{trade.get('message_id')}:"
            f"{ticket}"
        )
        record_automatic_pending(position_result.position, result, source_key)

        if result.get("status") in (STATUS_MOVED, STATUS_ALREADY_PROTECTED):

            position["break_even"] = True
            position["sl"] = result.get("target_sl", position.get("sl"))
            changed = True

            if result.get("status") == STATUS_MOVED:
                notify_break_even(ticket)
        elif result.get("status") == STATUS_PENDING:
            logger.warning(
                "Automatic TP1 profitable break-even is pending | "
                f"Ticket={ticket} Reason={result.get('reason')}"
            )

    return changed


def _trade_ready_to_archive(trade):
    positions = trade.get("positions", [])

    if not positions:
        return False

    if has_pending_for_trade(trade.get("chat_id"), trade.get("message_id")):
        return False

    return all(position.get("closed") for position in positions)


def process_trades_once(trades):

    for trade in trades:
        changed = False

        for position in trade.get("positions", []):
            if position.get("closed"):
                continue

            if _reconcile_position(position, trade):
                changed = True

        if _move_remaining_to_break_even(trade):
            changed = True

        if _trade_ready_to_archive(trade):
            archived = archive_trade(trade)

            if archived:
                notify_signal_archived(trade)

            continue

        if changed:
            update_trade(trade)


def run_startup_recovery():

    try:
        recover_pending_identities_once()
        retry_pending_break_even_actions()
        process_trades_once(load_trades())
    except Exception as exc:
        logger.warning(f"Startup recovery skipped: {exc}")


def monitor_positions():

    logger.info("Position Manager started")

    mark_position_manager_started()

    try:

        while not wait_for_shutdown(0):

            try:

                mark_position_manager_heartbeat()

                recover_pending_identities_once()
                retry_pending_break_even_actions()
                trades = load_trades()

                process_trades_once(trades)

                wait_for_shutdown(1)

            except Exception as e:

                logger.exception(e)

                mark_position_manager_error(str(e))

                notify_error(str(e))

                wait_for_shutdown(5)

    finally:
        mark_position_manager_stopped()
