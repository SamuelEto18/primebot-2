from core.telegram_control import send


def _signal_side(signal):
    return (
        getattr(signal, "final_side", None)
        or getattr(signal, "inferred_direction", None)
        or "Unknown"
    )


def notify_start():

    send(
        "🟢 PrimeBot Started"
    )


def notify_signal(signal):
    side = _signal_side(signal)

    text = (
        "📥 NEW SIGNAL\n\n"
        f"Symbol : {signal.symbol}\n"
        f"Side   : {side}\n"
        f"SL     : {signal.sl}\n"
        f"TPs    : {len(signal.tps)}"
    )

    send(text)


def notify_dry_run(signal):
    side = _signal_side(signal)

    message = (
        "🧪 DRY RUN\n\n"
        f"{side} {signal.symbol}\n\n"
    )

    for i, tp in enumerate(signal.tps, start=1):

        message += (
            f"Trade {i}\n"
            f"SL {signal.sl}\n"
            f"TP {tp}\n\n"
        )

    send(message)


def notify_paused_signal(item):

    symbol = getattr(item, "symbol", "Unknown")
    side = getattr(item, "side", "Unknown")
    message_id = getattr(
        item,
        "message_id",
        getattr(item, "id", "Unknown")
    )

    send(
        "PAUSED: SIGNAL IGNORED\n\n"
        "Bot is paused.\n\n"
        f"{side} {symbol}\n"
        f"Message ID {message_id}"
    )


def notify_success(trade):

    message = (
        "✅ EXECUTION COMPLETE\n\n"
        f"Positions : {len(trade['positions'])}"
    )

    send(message)


def notify_identity_pending(record):

    send(
        "MT5 accepted the order, but position identity is pending. "
        "Further TP orders were stopped to prevent untracked positions.\n\n"
        f"{record.get('symbol')} {record.get('side')} "
        f"TP{record.get('tp_index')} {record.get('tp')}\n"
        f"Order {record.get('order_ticket')} Deal {record.get('deal_ticket')}"
    )


def notify_identity_recovered(position):

    send(
        "PENDING POSITION RECOVERED\n\n"
        f"Ticket {position.get('ticket')}\n"
        f"{position.get('symbol')} TP{position.get('tp_index')} "
        f"{position.get('tp')}"
    )


def notify_position_closed(position):

    close_price = position.get("close_price")
    close_price_text = (
        f"\nClose Price {close_price}"
        if close_price is not None
        else ""
    )

    send(
        "POSITION CLOSED\n\n"
        f"Ticket {position.get('ticket')}\n"
        f"{position.get('symbol')} TP{position.get('tp_index')}\n"
        f"Reason {position.get('close_reason')}"
        f"{close_price_text}"
    )


def notify_signal_archived(trade):

    send(
        "SIGNAL ARCHIVED\n\n"
        f"Message ID {trade.get('message_id')}\n"
        f"{trade.get('symbol')} {trade.get('side')}"
    )


def notify_break_even(ticket):

    send(
        f"🎯 Break Even moved\n\nTicket {ticket}"
    )


def notify_break_even_summary(summary):
    no_positions = (
        "Unknown (paused)"
        if summary.get("paused")
        else ("Yes" if summary.get("no_primebot2_positions") else "No")
    )
    send(
        "SET BREAKEVEN - APPROVED STICKER\n\n"
        f"Mode: {str(summary.get('mode', 'unknown')).upper()}\n"
        f"Positions discovered: {summary.get('positions_discovered', 0)}\n"
        f"PrimeBot 2 positions: {summary.get('primebot2_positions', 0)}\n"
        f"Moved successfully: {summary.get('moved', 0)}\n"
        f"Already protected: {summary.get('already_protected', 0)}\n"
        f"Pending broker-validity retry: {summary.get('pending', 0)}\n"
        f"Simulated: {summary.get('simulated', 0)}\n"
        f"Failed: {summary.get('failed', 0)}\n"
        f"Foreign/manual positions ignored: {summary.get('ignored', 0)}\n"
        f"No PrimeBot 2 positions open: {no_positions}"
    )


def notify_break_even_retry_summary(summary):
    send(
        "SET BREAKEVEN - PENDING RETRY\n\n"
        f"Retried: {summary.get('retried', 0)}\n"
        f"Moved successfully: {summary.get('moved', 0)}\n"
        f"Already protected: {summary.get('already_protected', 0)}\n"
        f"Still pending: {summary.get('pending', 0)}\n"
        f"Closed: {summary.get('closed', 0)}\n"
        f"Failed: {summary.get('failed', 0)}"
    )


def notify_sticker_management_event(event_name, details=None):
    details = details or {}
    lines = [
        "PRIMEBOT 2 STICKER MANAGEMENT",
        f"Event: {str(event_name).replace('_', ' ').upper()}",
    ]

    preferred_fields = (
        "command",
        "mode",
        "operation_status",
        "original_status",
        "live_action_occurred",
        "operation",
        "ticket",
        "stage",
        "message_id",
        "source_chat_id",
        "sender_id",
        "document_id",
        "access_hash",
        "sticker_set_id",
        "sticker_set_short_name",
        "sticker_emoji",
        "mime_type",
        "is_animated",
        "is_video",
        "is_static",
        "message_date",
        "edit_date",
        "allowlist_match",
        "positions_discovered",
        "eligible",
        "updated",
        "already_protected",
        "closed",
        "already_absent",
        "skipped",
        "failed",
        "count",
        "reason",
    )

    for field in preferred_fields:
        value = details.get(field)

        if value is not None:
            label = field.replace("_", " ").title()
            lines.append(f"{label}: {value}")

    send("\n".join(lines)[:3900])


def notify_edit(signal):
    side = _signal_side(signal)

    send(
        "✏️ SIGNAL UPDATED\n\n"
        f"{signal.symbol}\n"
        f"Side {side}\n"
        f"SL {signal.sl}"
    )


def notify_error(message):

    send(
        f"❌ ERROR\n\n{message}"
    )


def notify_direction_conflict(signal):
    send(
        "Direction conflict: "
        f"message said {signal.textual_direction}, "
        f"levels imply {signal.inferred_direction}. "
        f"Using {signal.final_side}."
    )


def notify_manual_intervention(message):
    send(
        "MANUAL INTERVENTION REQUIRED\n\n"
        f"{message}"
    )


def notify_management_action(action_type, source_message_id, target, action, result):
    send(
        "MANAGEMENT ACTION DETECTED\n"
        f"Type: {action_type}\n"
        f"Source message: {source_message_id}\n"
        f"Target: {target}\n"
        f"Action: {action}\n"
        f"Result: {result}"
    )


def notify_management_blocked(reason, message, details=None):
    text = (
        "MANAGEMENT ACTION BLOCKED\n"
        f"Reason: {reason}\n"
        f"Message: {message}"
    )

    if details:
        text += f"\n{details}"

    send(text)


def notify_optional_management(message):
    send(
        "OPTIONAL MANAGEMENT SUGGESTION\n"
        f"Message: {message}\n"
        "No automatic action taken"
    )


def notify_close_instruction(message):
    send(
        "CLOSE INSTRUCTION DETECTED\n"
        f"Message: {message}\n"
        "No automatic close performed in this phase"
    )
