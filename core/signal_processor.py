from config import (
    ALLOWED_SYMBOLS,
    MAX_POSITIONS
)

from core.parser import (
    parse_signal,
    validate_signal
)

from core.duplicate_detector import (
    is_processed,
    mark_processed
)

from core.executor import execute_signal
from core.management import process_management_message
from core.sticker_management import handle_sticker_management
from core.synchronizer import synchronize_trade
from core.notifier import (
    notify_signal,
    notify_dry_run,
    notify_edit,
    notify_paused_signal,
    notify_direction_conflict,
    notify_error
)
from core.logger import logger
from core.runtime import (
    is_auto_execute,
    is_paused,
    mark_signal_received,
    mark_trade_executed
)


def _signal_side(signal):
    return (
        getattr(signal, "final_side", None)
        or getattr(signal, "inferred_direction", None)
    )


def _has_signal_structure(signal):
    return (
        signal.symbol is not None
        or signal.sl is not None
        or len(signal.tps) > 0
        or signal.entry_low is not None
        or signal.entry_high is not None
    )


def _report_invalid_signal(signal):
    reason = (
        getattr(signal, "validation_error", None)
        or getattr(signal, "direction_error", None)
        or "Invalid signal"
    )

    logger.info(f"Invalid signal ignored | Reason={reason}")

    if _has_signal_structure(signal):
        notify_error(f"Signal rejected: {reason}")


def _report_direction_conflict(signal):
    if not getattr(signal, "direction_conflict", False):
        return

    logger.warning(
        "Direction conflict: "
        f"message said {signal.textual_direction}, "
        f"levels imply {signal.inferred_direction}. "
        f"Using {signal.final_side}."
    )
    notify_direction_conflict(signal)


def _has_numeric_entry(signal):
    return signal.entry_low is not None or signal.entry_high is not None


def _validate_live_now_price(signal):
    logger.info(
        "Live market execution uses broker stop validation; "
        "current-price geometry is not pre-rejected."
    )
    return True


def _log_parsed_signal(signal, mode):
    logger.info(
        "PARSED SIGNAL | "
        f"ChatID={signal.chat_id} "
        f"Message={signal.message_id} "
        f"Symbol={signal.symbol} "
        f"SymbolSource={signal.symbol_source} "
        f"TextDirection={signal.textual_direction} "
        f"InferredDirection={signal.inferred_direction} "
        f"FinalSide={signal.final_side} "
        f"Conflict={signal.direction_conflict} "
        f"EntryLow={signal.entry_low} "
        f"EntryHigh={signal.entry_high} "
        f"NOW={signal.now_signal} "
        f"SL={signal.sl} "
        f"TPs={signal.tps!r} "
        f"Mode={mode}"
    )


async def process_new_message(event):

    if handle_sticker_management(event):
        return

    if not event.raw_text or not event.raw_text.strip():
        return

    if event.media:
        return

    if is_paused():
        logger.info(
            f"Signal ignored because bot is paused | "
            f"Message={event.id}"
        )

        notify_paused_signal(event)

        return

    logger.info("=" * 70)
    logger.info("RAW MESSAGE")
    logger.info(event.raw_text)
    logger.info("=" * 70)

    signal = parse_signal(event.raw_text)

    signal.message_id = event.id
    signal.chat_id = event.chat_id

    signal.tps = list(dict.fromkeys(signal.tps))

    if not validate_signal(signal):
        if process_management_message(event):
            return

        _report_invalid_signal(signal)
        return

    if signal.symbol not in ALLOWED_SYMBOLS:
        return

    if len(signal.tps) > MAX_POSITIONS:
        logger.error("Too many TP levels")
        return

    if is_processed(signal.message_id):
        return

    mark_signal_received()

    _report_direction_conflict(signal)

    auto_execute = is_auto_execute()
    mode = "live" if auto_execute else "dry_run"
    _log_parsed_signal(signal, mode)

    notify_signal(signal)

    if not auto_execute:

        logger.info("DRY RUN")

        notify_dry_run(signal)

        for tp in signal.tps:

            logger.info(
                f"{_signal_side(signal)} "
                f"{signal.symbol} "
                f"SL={signal.sl} "
                f"TP={tp}"
            )

        mark_processed(signal.message_id)

        return

    if not _validate_live_now_price(signal):
        mark_processed(signal.message_id)
        return

    summary = execute_signal(signal)

    if summary.get("opened", 0):
        mark_trade_executed(summary["opened"])

    logger.info(summary)

    mark_processed(signal.message_id)


async def process_edited_message(event):

    if handle_sticker_management(event, edited=True):
        return

    if not event.raw_text or not event.raw_text.strip():
        return

    if event.media:
        return

    signal = parse_signal(event.raw_text)

    signal.message_id = event.id
    signal.chat_id = event.chat_id
    signal.edited = True

    signal.tps = list(dict.fromkeys(signal.tps))

    if not validate_signal(signal):
        if process_management_message(event):
            return

        _report_invalid_signal(signal)
        return

    _report_direction_conflict(signal)

    notify_edit(signal)

    synchronize_trade(signal)
