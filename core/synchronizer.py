from core.trade_storage import (
    get_trade,
    update_trade
)

from core.mt5_executor import modify_trade
from core.logger import logger
from core.notifier import notify_manual_intervention


def _signal_side(signal):
    return (
        getattr(signal, "final_side", None)
        or getattr(signal, "inferred_direction", None)
    )


def _trade_side(trade):
    return trade.get("final_side") or trade.get("inferred_direction") or trade.get("side")


def _mark_manual_intervention(trade, signal, reason):
    trade["requires_manual_intervention"] = True
    trade["manual_intervention_reason"] = reason
    trade["edited_textual_direction"] = getattr(signal, "textual_direction", None)
    trade["edited_inferred_direction"] = getattr(signal, "inferred_direction", None)
    trade["edited_final_side"] = _signal_side(signal)
    trade["edited_sl"] = signal.sl
    trade["edited_tps"] = list(signal.tps)

    update_trade(trade)

    notify_manual_intervention(
        f"Message {signal.message_id} changed inferred direction after execution. "
        f"Existing side: {_trade_side(trade)}. "
        f"Edited levels imply: {_signal_side(signal)}. "
        "No positions were closed, reversed, opened, or modified."
    )


def synchronize_trade(signal):

    trade = get_trade(
        signal.chat_id,
        signal.message_id
    )

    if trade is None:
        logger.info(
            f"No active trade found for message {signal.message_id}"
        )
        return

    # Safety checks
    if trade["symbol"] != signal.symbol:
        logger.error(
            "Edited signal changed symbol. Ignoring edit."
        )
        return

    if _trade_side(trade) != _signal_side(signal):
        reason = (
            "Edited signal changed inferred direction after execution. "
            "Manual intervention required."
        )
        logger.error(reason)
        _mark_manual_intervention(trade, signal, reason)
        return

    # Update stored stop loss
    trade["sl"] = signal.sl
    trade["textual_direction"] = getattr(signal, "textual_direction", None)
    trade["inferred_direction"] = getattr(signal, "inferred_direction", _signal_side(signal))
    trade["final_side"] = _signal_side(signal)
    trade["direction_source"] = getattr(signal, "direction_source", None)
    trade["direction_conflict"] = getattr(signal, "direction_conflict", False)

    logger.info(
        f"Synchronizing {len(trade['positions'])} position(s)"
    )

    for index, position in enumerate(trade["positions"]):

        # Ignore positions already closed
        if position.get("closed", False):
            continue

        # Ignore if edited signal has fewer TP levels
        if index >= len(signal.tps):
            continue

        new_tp = signal.tps[index]

        result = modify_trade(
            ticket=position["ticket"],
            sl=signal.sl,
            tp=new_tp
        )

        if result["success"]:

            logger.info(
                f"Updated Ticket={position['ticket']} "
                f"SL={signal.sl} "
                f"TP={new_tp}"
            )

            position["tp"] = new_tp

        else:

            logger.error(
                f"Failed updating Ticket={position['ticket']} "
                f"Reason: {result['comment']}"
            )

    update_trade(trade)

    logger.info(
        f"Synchronization complete for message {signal.message_id}"
    )
