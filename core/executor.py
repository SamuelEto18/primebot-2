from datetime import datetime

from core.direction import infer_direction
from core.logger import logger
from core.mt5_executor import open_trade
from core.notifier import (
    notify_error,
    notify_identity_pending,
    notify_success,
)
from core.position_manager import recover_pending_identities_once
from core.trade_storage import (
    add_pending_identity,
    add_trade,
    load_trades,
)


def _fresh_direction(signal):
    return infer_direction(
        getattr(signal, "sl", None),
        getattr(signal, "tps", None),
        getattr(signal, "entry_low", None),
        getattr(signal, "entry_high", None),
        getattr(signal, "textual_direction", None),
    )


def _log_metadata_conflicts(signal, side):
    fields = (
        "side",
        "final_side",
        "inferred_direction",
    )

    for field in fields:
        value = getattr(signal, field, None)

        if value in ("BUY", "SELL") and value != side:
            logger.warning(
                "Ignoring stale direction metadata before execution | "
                f"Field={field} Value={value} Fresh={side}"
            )


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _position_key_values(position):
    return (
        position.get("position_ticket", position.get("ticket")),
        position.get("position_identifier", position.get("position_id")),
    )


def _assigned_position_identities(current_trade):
    tickets = set()
    identifiers = set()

    for trade in load_trades() + [current_trade]:
        for position in trade.get("positions", []):
            if position.get("closed"):
                continue

            ticket, identifier = _position_key_values(position)

            if ticket is not None:
                tickets.add(ticket)

            if identifier is not None:
                identifiers.add(identifier)

    return tickets, identifiers


def _position_record(signal, side, tp, index, result):
    return {
        "ticket": result["ticket"],
        "position_ticket": result.get("position_ticket", result["ticket"]),
        "position_identifier": result.get("position_identifier"),
        "position_id": result.get("position_id"),
        "order_ticket": result.get("order_ticket"),
        "deal_ticket": result.get("deal_ticket"),
        "symbol": result.get("symbol", signal.symbol),
        "side": result.get("side", side),
        "volume": result.get("volume"),
        "requested_price": result.get("requested_price"),
        "fill_price": result.get("fill_price"),
        "price_open": result.get("price_open"),
        "entry": result["price"],
        "entry_source": result.get("entry_source"),
        "sl": result.get("sl", signal.sl),
        "tp": tp,
        "magic": result.get("magic"),
        "comment": result.get("comment"),
        "chat_id": signal.chat_id,
        "message_id": signal.message_id,
        "tp_index": index,
        "order_attempt_started_at": result.get("order_attempt_started_at"),
        "identity_status": result.get("identity_status", "resolved"),
        "identity_resolution": result.get("identity_resolution"),
        "closed": False,
        "break_even": False,
    }


def _pending_record(signal, side, tp, index, result):
    return {
        "chat_id": signal.chat_id,
        "message_id": signal.message_id,
        "symbol": signal.symbol,
        "side": side,
        "textual_direction": getattr(signal, "textual_direction", None),
        "inferred_direction": getattr(signal, "inferred_direction", None),
        "direction_source": getattr(signal, "direction_source", None),
        "direction_conflict": getattr(signal, "direction_conflict", False),
        "sl": result.get("sl", signal.sl),
        "tp": tp,
        "tp_index": index,
        "volume": result.get("volume"),
        "order_ticket": result.get("order_ticket"),
        "deal_ticket": result.get("deal_ticket"),
        "requested_price": result.get("requested_price"),
        "fill_price": result.get("fill_price"),
        "price": result.get("price"),
        "magic": result.get("magic"),
        "comment": result.get("comment"),
        "retcode": result.get("retcode"),
        "result_comment": result.get("result_comment"),
        "selected_filling": result.get("selected_filling"),
        "filling_attempts": result.get("filling_attempts", []),
        "accepted": True,
        "accepted_identity_pending": True,
        "identity_status": "pending",
        "raw_message": signal.raw,
        "created_at": _now(),
        "order_attempt_started_at": result.get("order_attempt_started_at"),
    }


def _open_trade(signal, side, tp, trade):
    assigned_tickets, assigned_identifiers = _assigned_position_identities(trade)
    kwargs = {}

    if assigned_tickets:
        kwargs["excluded_position_tickets"] = assigned_tickets

    if assigned_identifiers:
        kwargs["excluded_position_identifiers"] = assigned_identifiers

    return open_trade(
        signal.symbol,
        side,
        signal.sl,
        tp,
        **kwargs
    )


def execute_signal(signal):
    direction = _fresh_direction(signal)

    if not direction.valid:
        reason = direction.reason or "Invalid SL/TP direction geometry"
        notify_error(f"Signal rejected: {reason}")
        return {
            "opened": 0,
            "failed": 0,
            "accepted_identity_pending": 0,
            "not_attempted": 0,
            "trade": None,
            "pending_records": [],
        }

    side = direction.final_side
    _log_metadata_conflicts(signal, side)

    trade = {
        "chat_id": signal.chat_id,
        "message_id": signal.message_id,
        "symbol": signal.symbol,
        "side": side,
        "textual_direction": getattr(signal, "textual_direction", None),
        "inferred_direction": direction.inferred_direction,
        "final_side": side,
        "direction_source": direction.direction_source,
        "direction_conflict": direction.direction_conflict,
        "sl": signal.sl,
        "raw_message": signal.raw,
        "positions": []
    }

    opened = 0
    failed = 0
    accepted_identity_pending = 0
    not_attempted = 0
    pending_records = []
    persisted = False

    logger.info("=" * 70)
    logger.info(
        f"EXECUTING {side} {signal.symbol}"
    )
    logger.info("=" * 70)

    for index, tp in enumerate(signal.tps, start=1):

        logger.info(
            f"Opening Trade {index}/{len(signal.tps)} "
            f"TP={tp}"
        )

        result = _open_trade(signal, side, tp, trade)

        logger.info(result)

        if result["success"]:

            opened += 1
            trade["positions"].append(
                _position_record(signal, side, tp, index, result)
            )

            logger.info(
                f"SUCCESS | "
                f"Ticket={result['ticket']} "
                f"Entry={result['price']} "
                f"TP={tp}"
            )

        elif result.get("accepted_identity_pending"):

            accepted_identity_pending += 1
            not_attempted = len(signal.tps) - index
            trade["identity_status"] = "pending"
            add_trade(trade)
            persisted = True

            record = add_pending_identity(
                _pending_record(signal, side, tp, index, result)
            )
            pending_records.append(record)
            notify_identity_pending(record)
            recover_pending_identities_once(
                chat_id=signal.chat_id,
                message_id=signal.message_id,
            )

            logger.error(
                "ACCEPTED IDENTITY PENDING | "
                f"TP={tp} Order={result.get('order_ticket')} "
                f"Deal={result.get('deal_ticket')}"
            )

            break

        else:

            failed += 1

            logger.error(
                f"FAILED | "
                f"TP={tp} | "
                f"{result['comment']}"
            )

    if trade["positions"] and not persisted:

        add_trade(trade)
        persisted = True

    if trade["positions"] and not accepted_identity_pending:

        notify_success(trade)

    if failed:

        notify_error(
            f"{failed} position(s) failed to open."
        )

    return {
        "opened": opened,
        "failed": failed,
        "accepted_identity_pending": accepted_identity_pending,
        "not_attempted": not_attempted,
        "trade": trade,
        "pending_records": pending_records,
    }
