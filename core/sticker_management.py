from dataclasses import dataclass
from datetime import datetime
from threading import RLock
import time

from telethon.tl.types import DocumentAttributeSticker

from config import (
    ALLOWED_SYMBOLS,
    COMMENT,
    MAGIC_NUMBER,
    PRIMEBOT2_TELEGRAM_CHANNEL_ID,
    PROFITABLE_BREAK_EVEN_SYMBOL,
)
from core.break_even import (
    STATUS_ALREADY_PROTECTED,
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_MOVED,
    STATUS_PENDING,
    STATUS_SIMULATED,
    apply_profitable_break_even,
)
from core.logger import logger
from core.mt5_service import (
    account_info,
    close_trade,
    position_type_buy,
    position_type_sell,
    positions_get,
)
from core.notifier import notify_sticker_management_event
from core.runtime import is_auto_execute, is_paused
from core.settings import ConfigurationError, load_settings
from core.sticker_management_storage import (
    RECOVERABLE_STATUSES,
    STATUS_COMPLETED,
    STATUS_DRY_RUN_CONSUMED,
    STATUS_FAILED as OPERATION_FAILED,
    STATUS_IGNORED as OPERATION_IGNORED,
    STATUS_IN_PROGRESS,
    STATUS_PARTIAL_FAILURE,
    STATUS_RECEIVED,
    STATUS_VALIDATED,
    TERMINAL_STATUSES,
    load_operation,
    load_operations,
    prepare_operation_positions,
    receive_operation,
    record_position_outcome,
    transition_operation,
)
from core.trade_storage import load_trades, update_trade


COMMAND_BREAK_EVEN = "break_even"
COMMAND_CLOSE_ALL = "close_all"
COMMAND_IGNORED = "ignored"

OUTCOME_UPDATED = "UPDATED"
OUTCOME_ALREADY_PROTECTED = "ALREADY_PROTECTED"
OUTCOME_CLOSED = "CLOSED"
OUTCOME_ALREADY_ABSENT = "ALREADY_ABSENT"
OUTCOME_SKIPPED = "SKIPPED"
OUTCOME_PENDING = "PENDING"
OUTCOME_FAILED = "FAILED"

_FINAL_POSITION_OUTCOMES = frozenset({
    OUTCOME_UPDATED,
    OUTCOME_ALREADY_PROTECTED,
    OUTCOME_CLOSED,
    OUTCOME_ALREADY_ABSENT,
    OUTCOME_SKIPPED,
})
_OPERATION_LOCK = RLock()


@dataclass
class VerifiedPosition:
    live_position: object
    trade: dict = None
    stored_position: dict = None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    return str(value)


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


def _sticker_attribute(document):
    for attribute in getattr(document, "attributes", None) or ():
        if isinstance(attribute, DocumentAttributeSticker):
            return attribute

    return None


def is_sticker_event(event):
    document = _event_document(event)
    return document is not None and _sticker_attribute(document) is not None


def _sticker_set_metadata(attribute):
    sticker_set = getattr(attribute, "stickerset", None)
    sticker_set_id = _to_int(getattr(sticker_set, "id", None))
    short_name = getattr(sticker_set, "short_name", None)

    if sticker_set_id is None and isinstance(sticker_set, int):
        sticker_set_id = sticker_set

    return sticker_set_id, short_name


def sticker_metadata(event, document=None):
    document = document or _event_document(event)
    attribute = _sticker_attribute(document)
    message = getattr(event, "message", None)
    sticker_set_id, sticker_set_short_name = _sticker_set_metadata(attribute)
    mime_type = getattr(document, "mime_type", None)
    mime_lower = str(mime_type or "").lower()
    animated_attribute = any(
        item.__class__.__name__ == "DocumentAttributeAnimated"
        for item in (getattr(document, "attributes", None) or ())
    )
    animated = animated_attribute or mime_lower == "application/x-tgsticker"
    video = mime_lower.startswith("video/") or mime_lower == "video/webm"
    sender_id = getattr(event, "sender_id", None)

    if sender_id is None and message is not None:
        sender_id = getattr(message, "sender_id", None)

    date = getattr(event, "date", None)
    edit_date = getattr(event, "edit_date", None)

    if message is not None:
        date = date or getattr(message, "date", None)
        edit_date = edit_date or getattr(message, "edit_date", None)

    return {
        "message_id": _to_int(
            getattr(event, "id", getattr(event, "message_id", None))
        ),
        "source_chat_id": _to_int(getattr(event, "chat_id", None)),
        "sender_id": _to_int(sender_id),
        "document_id": _to_int(getattr(document, "id", None)),
        "access_hash": _to_int(getattr(document, "access_hash", None)),
        "sticker_set_id": sticker_set_id,
        "sticker_set_short_name": sticker_set_short_name,
        "sticker_emoji": getattr(attribute, "alt", None),
        "mime_type": mime_type,
        "is_animated": animated,
        "is_video": video,
        "is_static": not animated and not video,
        "message_date": _iso(date),
        "edit_date": _iso(edit_date),
    }


def _log_fields(fields):
    return " ".join(
        f"{key}={value!r}"
        for key, value in fields.items()
        if value is not None
    )


def _emit(event_name, notify=False, level="info", **fields):
    message = f"STICKER MANAGEMENT | Event={event_name}"
    detail = _log_fields(fields)

    if detail:
        message += f" | {detail}"

    getattr(logger, level, logger.info)(message)

    if notify:
        notify_sticker_management_event(event_name, fields)


def _command_for_document(document_id, settings):
    if document_id in settings.sticker_break_even_document_ids:
        return COMMAND_BREAK_EVEN

    if document_id in settings.sticker_close_all_document_ids:
        return COMMAND_CLOSE_ALL

    return None


def _position_ticket(position):
    if isinstance(position, dict):
        return _to_int(position.get("position_ticket", position.get("ticket")))

    return _to_int(getattr(position, "ticket", None))


def _position_identifier(position):
    if isinstance(position, dict):
        return _to_int(
            position.get("position_identifier", position.get("position_id"))
        )

    return _to_int(
        getattr(
            position,
            "identifier",
            getattr(position, "position_identifier", None),
        )
    )


def _stored_side(trade, stored_position):
    return (
        stored_position.get("side")
        or trade.get("final_side")
        or trade.get("side")
        or trade.get("inferred_direction")
    )


def _live_side(live_position):
    position_type = getattr(live_position, "type", None)

    try:
        if position_type == position_type_buy():
            return "BUY"

        if position_type == position_type_sell():
            return "SELL"
    except Exception:
        # MetaTrader 5's documented position type values are stable (0/1).
        if position_type == 0:
            return "BUY"

        if position_type == 1:
            return "SELL"

    return None


def _approved_symbol(command, symbol):
    if command == COMMAND_BREAK_EVEN:
        return symbol == PROFITABLE_BREAK_EVEN_SYMBOL

    return symbol in ALLOWED_SYMBOLS


def _skip(position, reason, ownership_failure=False):
    return {
        "ticket": _position_ticket(position),
        "identifier": _position_identifier(position),
        "symbol": getattr(position, "symbol", None),
        "reason": reason,
        "ownership_failure": bool(ownership_failure),
    }


def _durable_records(trades):
    records = []

    for trade in trades:
        for stored_position in trade.get("positions", []):
            records.append((trade, stored_position))

    return records


def _matching_durable_records(live_position, records):
    ticket = _position_ticket(live_position)
    identifier = _position_identifier(live_position)
    matches = []

    for trade, stored_position in records:
        stored_ticket = _position_ticket(stored_position)
        stored_identifier = _position_identifier(stored_position)

        if (
            ticket is not None
            and stored_ticket is not None
            and ticket == stored_ticket
        ) or (
            identifier is not None
            and stored_identifier is not None
            and identifier == stored_identifier
        ):
            matches.append((trade, stored_position))

    return matches


def _durable_contradiction(trade, stored_position, live_position):
    checks = (
        ("ticket", _position_ticket(stored_position), _position_ticket(live_position)),
        (
            "identifier",
            _position_identifier(stored_position),
            _position_identifier(live_position),
        ),
        (
            "symbol",
            stored_position.get("symbol") or trade.get("symbol"),
            getattr(live_position, "symbol", None),
        ),
        ("magic", stored_position.get("magic"), getattr(live_position, "magic", None)),
        (
            "comment",
            stored_position.get("comment"),
            getattr(live_position, "comment", None),
        ),
        ("side", _stored_side(trade, stored_position), _live_side(live_position)),
    )

    for field, stored_value, live_value in checks:
        if stored_value is not None and stored_value != live_value:
            return f"Durable trade record contradicts live {field}"

    if stored_position.get("closed") is True:
        return "Durable trade record marks the live position closed"

    identity_status = stored_position.get("identity_status")

    if identity_status not in (None, "resolved"):
        return "Durable trade record has unresolved position identity"

    return None


def _validate_live_ownership(live_position, command, durable_records):
    ticket = _position_ticket(live_position)
    identifier = _position_identifier(live_position)
    symbol = getattr(live_position, "symbol", None)

    if ticket is None or identifier is None:
        return None, "Live MT5 ticket or position identifier is unavailable"

    if not _approved_symbol(command, symbol):
        return None, "Unsupported broker symbol"

    if getattr(live_position, "magic", None) != MAGIC_NUMBER:
        return None, "Manual or different magic number"

    if getattr(live_position, "comment", None) != COMMENT:
        return None, "Foreign PrimeBot comment"

    if _live_side(live_position) not in ("BUY", "SELL"):
        return None, "Unsupported MT5 position side"

    matches = _matching_durable_records(live_position, durable_records)

    if len(matches) > 1:
        return None, "Multiple durable trade records match the live position"

    if not matches:
        return VerifiedPosition(live_position=live_position), None

    trade, stored_position = matches[0]
    contradiction = _durable_contradiction(trade, stored_position, live_position)

    if contradiction:
        return None, contradiction

    return VerifiedPosition(
        live_position=live_position,
        trade=trade,
        stored_position=stored_position,
    ), None


def discover_verified_positions(expected_account_login, command):
    expected_login = _to_int(expected_account_login)

    if expected_login is None:
        return {
            "targets": [],
            "skipped": [],
            "positions_discovered": 0,
            "account_login": None,
            "error": "MT5_LOGIN is not configured; ownership cannot be proven",
        }

    try:
        current_account = account_info()
    except Exception as exc:
        current_account = None
        account_error = str(exc)
    else:
        account_error = None

    current_login = _to_int(getattr(current_account, "login", None))

    if current_login is None:
        return {
            "targets": [],
            "skipped": [],
            "positions_discovered": 0,
            "account_login": None,
            "error": account_error or "Current MT5 account login is unavailable",
        }

    if current_login != expected_login:
        return {
            "targets": [],
            "skipped": [],
            "positions_discovered": 0,
            "account_login": current_login,
            "error": "Current MT5 account does not match configured MT5_LOGIN",
        }

    try:
        live_positions = positions_get()
    except Exception as exc:
        live_positions = None
        positions_error = str(exc)
    else:
        positions_error = None

    if live_positions is None:
        return {
            "targets": [],
            "skipped": [],
            "positions_discovered": 0,
            "account_login": current_login,
            "error": positions_error or "MT5 open-position query failed",
        }

    live_positions = list(live_positions)
    records = _durable_records(load_trades())
    targets = []
    skipped = []

    for live_position in live_positions:
        verified, reason = _validate_live_ownership(
            live_position,
            command,
            records,
        )

        if verified is None:
            ownership_failure = (
                getattr(live_position, "magic", None) == MAGIC_NUMBER
                and getattr(live_position, "comment", None) == COMMENT
                and _approved_symbol(command, getattr(live_position, "symbol", None))
            )
            skipped.append(_skip(
                live_position,
                reason,
                ownership_failure=ownership_failure,
            ))
        else:
            targets.append(verified)

    return {
        "targets": targets,
        "skipped": skipped,
        "positions_discovered": len(live_positions),
        "account_login": current_login,
        "error": None,
    }


def _position_key(account_login, position, command):
    return ":".join(str(value) for value in (
        int(account_login),
        _position_ticket(position),
        _position_identifier(position),
        command,
    ))


def _position_snapshot(target, account_login, command):
    position = target.live_position
    return {
        "account_login": int(account_login),
        "ticket": _position_ticket(position),
        "identifier": _position_identifier(position),
        "operation": command,
        "symbol": getattr(position, "symbol", None),
        "side": _live_side(position),
        "open_price": getattr(position, "price_open", None),
        "volume": getattr(position, "volume", None),
        "durable_record_present": target.stored_position is not None,
        "attempts": [],
    }


def _discovery_record(discovery):
    return {
        "account_login": discovery.get("account_login"),
        "positions_discovered": discovery.get("positions_discovered", 0),
        "eligible": len(discovery.get("targets", [])),
        "skipped": list(discovery.get("skipped", [])),
    }


def _record_trade_metadata(target, command, message_id, result):
    if target is None or target.trade is None or target.stored_position is None:
        return

    stored = target.stored_position
    stored["sticker_management_message_id"] = message_id
    stored["sticker_management_action"] = command
    stored["sticker_management_result"] = dict(result)

    if command == COMMAND_BREAK_EVEN and result.get("status") in {
        OUTCOME_UPDATED,
        OUTCOME_ALREADY_PROTECTED,
    }:
        stored["sl"] = result.get("target_sl", stored.get("sl"))
        stored["break_even"] = True
    elif command == COMMAND_CLOSE_ALL and result.get("status") == OUTCOME_CLOSED:
        stored["close_requested_by_sticker"] = True

    update_trade(target.trade)


def _live_positions_for_resume(expected_login):
    try:
        current_account = account_info()
    except Exception as exc:
        return None, None, str(exc)

    current_login = _to_int(getattr(current_account, "login", None))

    if current_login != _to_int(expected_login):
        return (
            current_login,
            None,
            "Current MT5 account does not match the operation account",
        )

    try:
        positions = positions_get()
    except Exception as exc:
        return current_login, None, str(exc)

    if positions is None:
        return current_login, None, "MT5 open-position query failed"

    return current_login, list(positions), None


def _target_from_live(snapshot, live_positions, command, durable_records):
    ticket = _to_int(snapshot.get("ticket"))
    identifier = _to_int(snapshot.get("identifier"))
    same_ticket = [
        position for position in live_positions
        if _position_ticket(position) == ticket
    ]

    if not same_ticket:
        return None, None

    exact = [
        position for position in same_ticket
        if _position_identifier(position) == identifier
    ]

    if len(exact) != 1:
        return None, "Live position identity changed or is ambiguous"

    position = exact[0]
    verified, reason = _validate_live_ownership(position, command, durable_records)

    if verified is None:
        return None, reason

    if (
        getattr(position, "symbol", None) != snapshot.get("symbol")
        or _live_side(position) != snapshot.get("side")
    ):
        return None, "Live position contradicts the durable operation snapshot"

    return verified, None


def _break_even_outcome(target, settings):
    position = target.live_position
    ticket = _position_ticket(position)

    try:
        result = apply_profitable_break_even(
            position,
            dry_run=False,
            expected_account_login=settings.mt5_login,
            expected_identifier=_position_identifier(position),
        )
    except Exception as exc:
        result = {
            "status": STATUS_FAILED,
            "ticket": ticket,
            "reason": str(exc),
        }

    status_map = {
        STATUS_MOVED: OUTCOME_UPDATED,
        STATUS_ALREADY_PROTECTED: OUTCOME_ALREADY_PROTECTED,
        STATUS_PENDING: OUTCOME_PENDING,
        STATUS_IGNORED: OUTCOME_SKIPPED,
        STATUS_SIMULATED: OUTCOME_FAILED,
        STATUS_FAILED: OUTCOME_FAILED,
    }
    outcome = dict(result)
    outcome["status"] = status_map.get(result.get("status"), OUTCOME_FAILED)
    return outcome


def _close_outcome(target, settings):
    position = target.live_position
    ticket = _position_ticket(position)

    try:
        result = close_trade(
            ticket,
            expected_symbol=getattr(position, "symbol", None),
            expected_magic=MAGIC_NUMBER,
            expected_comment=COMMENT,
            expected_account_login=settings.mt5_login,
            expected_identifier=_position_identifier(position),
        )
    except Exception as exc:
        result = {
            "success": False,
            "ticket": ticket,
            "comment": str(exc),
        }

    outcome = dict(result)

    if result.get("success"):
        outcome["status"] = OUTCOME_CLOSED
    elif result.get("already_absent") or result.get("comment") == "Position not found":
        outcome["status"] = OUTCOME_ALREADY_ABSENT
    elif result.get("ownership_mismatch"):
        outcome["status"] = OUTCOME_SKIPPED
    else:
        outcome["status"] = OUTCOME_FAILED

    return outcome


def _operation_summary(record):
    command = record.get("command")
    discovery = record.get("discovery", {})
    summary = {
        "command": command,
        "mode": "live",
        "positions_discovered": discovery.get("positions_discovered", 0),
        "eligible": len(record.get("positions", {})),
        "skipped": len(discovery.get("skipped", [])),
        "failed": 0,
        "already_absent": 0,
    }

    if command == COMMAND_BREAK_EVEN:
        summary.update({"updated": 0, "already_protected": 0})
    else:
        summary.update({"closed": 0})

    for position in record.get("positions", {}).values():
        status = (position.get("outcome") or {}).get("status")

        if status == OUTCOME_UPDATED:
            summary["updated"] += 1
        elif status == OUTCOME_ALREADY_PROTECTED:
            summary["already_protected"] += 1
        elif status == OUTCOME_CLOSED:
            summary["closed"] += 1
        elif status == OUTCOME_ALREADY_ABSENT:
            summary["already_absent"] += 1
        elif status == OUTCOME_SKIPPED:
            summary["skipped"] += 1
        elif status in (OUTCOME_PENDING, OUTCOME_FAILED) or status is None:
            summary["failed"] += 1

    return summary


def _finish_operation(key):
    record = load_operation(key)

    if record is None:
        return None

    summary = _operation_summary(record)
    has_retryable_failure = any(
        (position.get("outcome") or {}).get("status")
        in (OUTCOME_PENDING, OUTCOME_FAILED, None)
        for position in record.get("positions", {}).values()
    )
    final_status = (
        STATUS_PARTIAL_FAILURE
        if has_retryable_failure
        else STATUS_COMPLETED
    )

    if not transition_operation(key, final_status, result=summary):
        _emit(
            "complete_failure",
            notify=True,
            level="error",
            operation=key,
            reason="final_state_write_failed",
        )
        return None

    if final_status == STATUS_PARTIAL_FAILURE:
        _emit(
            "partial_failure",
            notify=True,
            level="warning",
            operation=key,
            command=record.get("command"),
            failed=summary.get("failed", 0),
        )

    _emit(
        f"{record.get('command')}_completed",
        notify=True,
        operation=key,
        operation_status=final_status,
        **summary,
    )
    return summary


def _resume_operation(key, settings, force=True):
    record = load_operation(key)

    if record is None or record.get("status") not in RECOVERABLE_STATUSES:
        return None

    command = record.get("command")

    if command not in (COMMAND_BREAK_EVEN, COMMAND_CLOSE_ALL):
        transition_operation(
            key,
            OPERATION_FAILED,
            result={"reason": "Recoverable operation has an invalid command"},
        )
        return None

    if not record.get("targets_prepared"):
        discovery = discover_verified_positions(settings.mt5_login, command)

        if discovery.get("error"):
            result = {
                "command": command,
                "mode": "live",
                "positions_discovered": 0,
                "eligible": 0,
                "skipped": 0,
                "failed": 1,
                "reason": discovery["error"],
            }
            terminal_identity_failure = (
                "does not match configured MT5_LOGIN" in discovery["error"]
                or "MT5_LOGIN is not configured" in discovery["error"]
            )
            transition_operation(
                key,
                OPERATION_FAILED if terminal_identity_failure else STATUS_PARTIAL_FAILURE,
                result=result,
            )
            _emit(
                "ownership_validation_failure",
                notify=True,
                level="error",
                operation=key,
                reason=discovery["error"],
            )
            return result

        ownership_failures = [
            item for item in discovery["skipped"]
            if item.get("ownership_failure")
        ]

        if ownership_failures:
            _emit(
                "ownership_validation_failure",
                notify=True,
                level="warning",
                operation=key,
                count=len(ownership_failures),
            )

        prepared = {}

        for target in discovery["targets"]:
            position_key = _position_key(
                discovery["account_login"],
                target.live_position,
                command,
            )
            prepared[position_key] = _position_snapshot(
                target,
                discovery["account_login"],
                command,
            )

        if not prepare_operation_positions(
            key,
            prepared,
            discovery=_discovery_record(discovery),
        ):
            _emit(
                "complete_failure",
                notify=True,
                level="error",
                operation=key,
                reason="position_snapshot_write_failed",
            )
            return None

        record = load_operation(key)
    elif record.get("status") == STATUS_PARTIAL_FAILURE:
        if not transition_operation(key, STATUS_IN_PROGRESS, resumed=True):
            return None
        record = load_operation(key)

    _emit(
        f"{command}_started",
        notify=True,
        operation=key,
        mode="live",
        resumed=bool(record.get("transitions", [])[:-2]),
    )

    account_login, live_positions, error = _live_positions_for_resume(
        settings.mt5_login
    )

    if error:
        summary = _operation_summary(record)
        summary["failed"] = max(1, summary.get("failed", 0))
        summary["reason"] = error
        transition_operation(key, STATUS_PARTIAL_FAILURE, result=summary)
        _emit(
            "ownership_validation_failure",
            notify=True,
            level="error",
            operation=key,
            reason=error,
        )
        return summary

    durable_records = _durable_records(load_trades())

    for position_key, snapshot in record.get("positions", {}).items():
        prior_status = (snapshot.get("outcome") or {}).get("status")

        if prior_status in _FINAL_POSITION_OUTCOMES:
            continue

        if (
            not force
            and prior_status in (OUTCOME_PENDING, OUTCOME_FAILED)
            and time.time() < snapshot.get("next_retry_at", 0)
        ):
            continue

        target, reason = _target_from_live(
            snapshot,
            live_positions,
            command,
            durable_records,
        )

        if target is None and reason is None:
            outcome = {
                "status": OUTCOME_ALREADY_ABSENT,
                "ticket": snapshot.get("ticket"),
                "identifier": snapshot.get("identifier"),
                "reason": "Position is already absent during reconciliation",
            }
        elif target is None:
            outcome = {
                "status": OUTCOME_SKIPPED,
                "ticket": snapshot.get("ticket"),
                "identifier": snapshot.get("identifier"),
                "reason": reason,
                "ownership_mismatch": True,
            }
            _emit(
                "ownership_validation_failure",
                notify=True,
                level="warning",
                operation=key,
                ticket=snapshot.get("ticket"),
                reason=reason,
            )
        elif command == COMMAND_BREAK_EVEN:
            outcome = _break_even_outcome(target, settings)
        else:
            outcome = _close_outcome(target, settings)

        if not record_position_outcome(key, position_key, outcome):
            _emit(
                "complete_failure",
                notify=True,
                level="error",
                operation=key,
                ticket=snapshot.get("ticket"),
                reason="position_outcome_write_failed",
            )
            return None

        _record_trade_metadata(
            target,
            command,
            record.get("message_id"),
            outcome,
        )

    if not record.get("positions"):
        _emit(
            "no_eligible_positions",
            notify=True,
            operation=key,
            command=command,
        )

    return _finish_operation(key)


def _operation_retry_due(record):
    if record.get("status") != STATUS_PARTIAL_FAILURE:
        return True

    for position in record.get("positions", {}).values():
        status = (position.get("outcome") or {}).get("status")

        if status not in _FINAL_POSITION_OUTCOMES and (
            status not in (OUTCOME_PENDING, OUTCOME_FAILED)
            or time.time() >= position.get("next_retry_at", 0)
        ):
            return True

    return False


def resume_pending_sticker_operations(force=False):
    with _OPERATION_LOCK:
        operations = load_operations()

        if operations is None:
            _emit(
                "complete_failure",
                notify=True,
                level="error",
                reason="durable_sticker_management_state_unreadable",
            )
            return {"resumed": 0, "skipped": 0, "failed": 1}

        pending = [
            (key, record) for key, record in operations.items()
            if record.get("status") in RECOVERABLE_STATUSES
        ]
        summary = {"resumed": 0, "skipped": 0, "failed": 0}

        if not pending:
            return summary

        remaining = []

        for key, record in pending:
            received_mode = record.get("metadata", {}).get("received_mode")

            if record.get("status") != STATUS_RECEIVED or received_mode == "live":
                remaining.append((key, record))
                continue

            if transition_operation(
                key,
                STATUS_DRY_RUN_CONSUMED,
                result={
                    "command": record.get("command"),
                    "mode": received_mode or "unknown",
                    "live_action_occurred": False,
                    "reason": (
                        "Pre-validation receipt reconciled without live action"
                    ),
                },
            ):
                summary["skipped"] += 1
            else:
                summary["failed"] += 1

        pending = remaining

        if not pending:
            return summary

        if is_paused() or not is_auto_execute():
            summary["skipped"] += len(pending)
            return summary

        try:
            settings = load_settings(validate=False)
        except ConfigurationError as exc:
            _emit(
                "complete_failure",
                notify=True,
                level="error",
                reason=str(exc),
            )
            summary["failed"] = len(pending)
            return summary

        for key, record in pending:
            if (
                record.get("chat_id") != PRIMEBOT2_TELEGRAM_CHANNEL_ID
                or record.get("command") not in (
                    COMMAND_BREAK_EVEN,
                    COMMAND_CLOSE_ALL,
                )
            ):
                transition_operation(
                    key,
                    OPERATION_FAILED,
                    result={"reason": "Stored operation identity is invalid"},
                )
                summary["failed"] += 1
                continue

            if record.get("status") == STATUS_RECEIVED:
                if not transition_operation(
                    key,
                    STATUS_VALIDATED,
                    validation={
                        "source_chat_id": record.get("chat_id"),
                        "document_id": record.get("document_id"),
                        "command": record.get("command"),
                        "account_login": settings.mt5_login,
                        "mode": "live",
                    },
                ):
                    summary["failed"] += 1
                    continue

                record = load_operation(key)

            if not force and not _operation_retry_due(record):
                summary["skipped"] += 1
                continue

            result = _resume_operation(key, settings, force=force)

            if result is None:
                summary["failed"] += 1
            else:
                summary["resumed"] += 1

        return summary


def _handle_sticker_management_locked(event, edited=False):
    document = _event_document(event)
    attribute = _sticker_attribute(document) if document is not None else None

    if document is None or attribute is None:
        return False

    metadata = sticker_metadata(event, document=document)
    chat_id = metadata["source_chat_id"]
    message_id = metadata["message_id"]
    document_id = metadata["document_id"]

    _emit("sticker_discovered", edited=bool(edited), **metadata)

    if chat_id != PRIMEBOT2_TELEGRAM_CHANNEL_ID:
        _emit(
            "sticker_ignored",
            level="warning",
            reason="unauthorized_source_chat",
            source_chat_id=chat_id,
            message_id=message_id,
            document_id=document_id,
        )
        return True

    if message_id is None or document_id is None:
        _emit(
            "sticker_ignored",
            notify=True,
            level="warning",
            reason="missing_message_or_document_id",
            source_chat_id=chat_id,
        )
        return True

    try:
        settings = load_settings(validate=False)
    except ConfigurationError as exc:
        _emit(
            "complete_failure",
            notify=True,
            level="error",
            reason=str(exc),
            message_id=message_id,
        )
        return True

    if settings.channel_id != PRIMEBOT2_TELEGRAM_CHANNEL_ID:
        _emit(
            "sticker_ignored",
            notify=True,
            level="error",
            reason="configured_source_chat_mismatch",
            source_chat_id=chat_id,
            message_id=message_id,
        )
        return True

    if settings.sticker_discovery_notify:
        notify_sticker_management_event("sticker_discovered", metadata)

    command = _command_for_document(document_id, settings)
    metadata["allowlist_match"] = command
    metadata["received_mode"] = (
        "paused" if is_paused() else ("live" if is_auto_execute() else "dry_run")
    )
    receipt = receive_operation(
        chat_id,
        message_id,
        command or COMMAND_IGNORED,
        document_id,
        metadata=metadata,
    )

    if receipt.get("error"):
        _emit(
            "complete_failure",
            notify=True,
            level="error",
            reason=receipt["error"],
            message_id=message_id,
        )
        return True

    operation_key = receipt["key"]
    record = receipt["record"]

    if (
        record.get("document_id") != document_id
        or record.get("command") != (command or COMMAND_IGNORED)
    ):
        _emit(
            "duplicate_command_suppressed",
            notify=True,
            level="warning",
            operation=operation_key,
            reason="message_identity_changed",
            edited=bool(edited),
        )
        return True

    if not receipt.get("created") and record.get("status") in TERMINAL_STATUSES:
        _emit(
            "duplicate_command_suppressed",
            notify=True,
            operation=operation_key,
            original_command=record.get("command"),
            original_status=record.get("status"),
            edited=bool(edited),
        )
        return True

    if command is None:
        reason = (
            "sticker_allowlists_empty"
            if not settings.sticker_break_even_document_ids
            and not settings.sticker_close_all_document_ids
            else "document_id_not_allowlisted"
        )
        result = {"reason": reason, "allowlist_match": None}

        if not transition_operation(operation_key, OPERATION_IGNORED, result=result):
            _emit(
                "complete_failure",
                notify=True,
                level="error",
                operation=operation_key,
                reason="ignored_state_write_failed",
            )
            return True

        _emit(
            "sticker_ignored",
            notify=settings.sticker_discovery_notify,
            operation=operation_key,
            reason=reason,
            document_id=document_id,
        )
        return True

    received_mode = record.get("metadata", {}).get("received_mode")

    if received_mode != "live" and record.get("status") == STATUS_RECEIVED:
        mode = received_mode or "unknown"
        result = {
            "command": command,
            "mode": mode,
            "live_action_occurred": False,
            "reason": f"Sticker consumed while {mode}; no live action occurred",
        }
        transition_operation(
            operation_key,
            STATUS_DRY_RUN_CONSUMED,
            result=result,
        )
        _emit(
            "dry_run_consumed",
            notify=True,
            operation=operation_key,
            command=command,
            mode=mode,
            live_action_occurred=False,
        )
        return True

    if is_paused() or not is_auto_execute():
        mode = "paused" if is_paused() else "dry_run"

        if record.get("status") in RECOVERABLE_STATUSES:
            _emit(
                "recovery_deferred",
                notify=True,
                operation=operation_key,
                command=command,
                mode=mode,
                original_status=record.get("status"),
                live_action_occurred=False,
            )
            return True

    if settings.mt5_login is None:
        result = {
            "command": command,
            "mode": "live",
            "reason": "MT5_LOGIN is not configured; ownership cannot be proven",
        }
        transition_operation(operation_key, OPERATION_FAILED, result=result)
        _emit(
            "ownership_validation_failure",
            notify=True,
            level="error",
            operation=operation_key,
            reason=result["reason"],
        )
        return True

    if record.get("status") == STATUS_RECEIVED:
        if not transition_operation(
            operation_key,
            STATUS_VALIDATED,
            validation={
                "source_chat_id": chat_id,
                "document_id": document_id,
                "command": command,
                "account_login": settings.mt5_login,
                "mode": "live",
            },
        ):
            _emit(
                "complete_failure",
                notify=True,
                level="error",
                operation=operation_key,
                reason="validated_state_write_failed",
            )
            return True

    _emit(
        "sticker_command_accepted",
        notify=True,
        operation=operation_key,
        command=command,
        document_id=document_id,
    )
    _resume_operation(operation_key, settings)
    return True


def handle_sticker_management(event, edited=False):
    with _OPERATION_LOCK:
        return _handle_sticker_management_locked(event, edited=edited)
