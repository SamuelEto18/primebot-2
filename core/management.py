import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from config import ALLOWED_SYMBOLS, COMMENT, MAGIC_NUMBER
from core.logger import logger
from core.management_storage import (
    is_management_processed,
    mark_management_processed,
)
from core.mt5_service import (
    POSITION_OPEN,
    modify_trade,
    query_position,
    symbol_info,
    symbol_info_tick,
)
from core.notifier import (
    notify_close_instruction,
    notify_management_action,
    notify_management_blocked,
    notify_optional_management,
)
from core.parser import NUMBER_PATTERN, parse_number
from core.settings import load_settings
from core.trade_storage import load_trades, update_trade

BE_TOLERANCE = 0.05


@dataclass
class ManagementInstruction:
    action: str
    symbol: str = None
    sl: float = None
    tp_updates: dict = field(default_factory=dict)
    be_level: float = None
    global_scope: bool = False
    alert_only: bool = False
    reason: str = None
    normalized_action: str = None


@dataclass
class PositionTarget:
    trade: dict
    position: dict
    live_position: object


def _strip_diacritics(value):
    replacements = str.maketrans({
        "ă": "a",
        "â": "a",
        "î": "i",
        "ș": "s",
        "ş": "s",
        "ț": "t",
        "ţ": "t",
        "Ă": "A",
        "Â": "A",
        "Î": "I",
        "Ș": "S",
        "Ş": "S",
        "Ț": "T",
        "Ţ": "T",
    })
    value = value.translate(replacements)
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def normalize_text(text):
    text = _strip_diacritics(text or "")
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_symbol(text):
    upper = (text or "").upper()

    if re.search(r"\bBTCUSD(?:\.[A-Z0-9]+)?\b", upper):
        return "BTCUSD"

    if re.search(r"\bXAUU?USD(?:\.[A-Z0-9]+)?\b", upper):
        return "XAUUSD.s"

    for symbol in ALLOWED_SYMBOLS:
        if re.search(rf"\b{re.escape(symbol.upper())}\b", upper):
            return symbol

    return None


def _numbers_in_text(text):
    values = []

    for match in re.finditer(NUMBER_PATTERN, text or ""):
        number = parse_number(match.group(0))

        if number is not None:
            values.append(number)

    return values


def _format_number(value):
    if value is None:
        return "entry"

    return f"{float(value):.10g}"


def _normalized_action(instruction):
    symbol = instruction.symbol or "*"

    if instruction.action == "sl_update":
        return f"sl:{_format_number(instruction.sl)}:symbol:{symbol}"

    if instruction.action == "tp_update":
        if not instruction.tp_updates:
            return f"tp:alert-only:symbol:{symbol}"

        parts = [
            f"{index}={_format_number(value)}"
            for index, value in sorted(instruction.tp_updates.items())
        ]
        return f"tp:{';'.join(parts)}:symbol:{symbol}"

    if instruction.action == "break_even":
        scope = "global" if instruction.global_scope else "signal"
        return f"be:{_format_number(instruction.be_level)}:{scope}:symbol:{symbol}"

    return f"{instruction.action}:symbol:{symbol}"


def parse_management_instruction(text):
    normalized = normalize_text(text)
    symbol = _extract_symbol(text)

    if not normalized:
        return None

    if re.search(r"\b(optional|cine\s+(vrea|doreste)|whoever\s+wants|who\s+wants)\b", normalized):
        instruction = ManagementInstruction(
            action="optional",
            symbol=symbol,
            alert_only=True,
        )
        instruction.normalized_action = _normalized_action(instruction)
        return instruction

    close_detected = re.search(r"\b(inchidem|inchide|inchizi|close|closing)\b", normalized)
    close_context = re.search(
        r"\b(tot|totul|toate|all|everything|manual|tp\s*\d+)\b",
        normalized,
    )

    if close_detected and close_context:
        instruction = ManagementInstruction(
            action="close",
            symbol=symbol,
            alert_only=True,
        )
        instruction.normalized_action = _normalized_action(instruction)
        return instruction

    be_detected = (
        re.search(r"\bbreak\s*even\b", normalized)
        or re.search(r"\bbreakeven\b", normalized)
        or re.search(r"\bbe\b", normalized)
    )

    if be_detected:
        numbers = _numbers_in_text(normalized)
        global_scope = bool(
            re.search(r"\b(tot|totul|toate)\s+(?:(?:in|la)\s+)?be\b", normalized)
            or re.search(r"\bset\s+everything\s+to\s+be\b", normalized)
            or re.search(r"\bmove\s+everything\s+to\s+be\b", normalized)
        )
        instruction = ManagementInstruction(
            action="break_even",
            symbol=symbol,
            be_level=numbers[0] if numbers else None,
            global_scope=global_scope,
        )
        instruction.normalized_action = _normalized_action(instruction)
        return instruction

    tp_updates = {}
    tp_pattern = re.compile(
        rf"\btp\s*([1-9]\d*)\b[^\d]{{0,24}}({NUMBER_PATTERN})"
    )

    for match in tp_pattern.finditer(normalized):
        index = int(match.group(1))
        value = parse_number(match.group(2))

        if value is not None:
            tp_updates[index] = value

    if tp_updates:
        instruction = ManagementInstruction(
            action="tp_update",
            symbol=symbol,
            tp_updates=tp_updates,
        )
        instruction.normalized_action = _normalized_action(instruction)
        return instruction

    if re.search(r"\btp\b|\btake\s+profit\b", normalized) and re.search(
        r"\b(schimbat|schimbam|modificat|modificam|changed|change|update|updated)\b",
        normalized,
    ):
        instruction = ManagementInstruction(
            action="tp_update",
            symbol=symbol,
            alert_only=True,
            reason="TP levels changed but no numeric levels were provided",
        )
        instruction.normalized_action = _normalized_action(instruction)
        return instruction

    sl_match = re.search(rf"\bsl\b[^\d]{{0,30}}({NUMBER_PATTERN})", normalized)

    if sl_match:
        instruction = ManagementInstruction(
            action="sl_update",
            symbol=symbol,
            sl=parse_number(sl_match.group(1)),
        )
        instruction.normalized_action = _normalized_action(instruction)
        return instruction

    return None


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


def _identity_value(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _position_ticket(position):
    return position.get("position_ticket", position.get("ticket"))


def _position_identifier(position):
    return position.get(
        "position_identifier",
        position.get("position_id"),
    )


def _trade_side(trade):
    return (
        trade.get("side")
        or trade.get("final_side")
        or trade.get("inferred_direction")
        or trade.get("textual_direction")
    )


def _position_side(position, trade):
    return position.get("side") or _trade_side(trade)


def _mt5_api():
    try:
        from core import mt5_service

        return getattr(mt5_service, "mt5", None)
    except Exception:
        return None


def _live_position_side_matches(live_position, side):
    position_type = getattr(live_position, "type", None)

    if position_type is None:
        return True

    api = _mt5_api()
    buy_type = getattr(api, "POSITION_TYPE_BUY", 0)
    sell_type = getattr(api, "POSITION_TYPE_SELL", 1)

    if side == "BUY":
        return position_type == buy_type

    if side == "SELL":
        return position_type == sell_type

    return False


def _live_matches_stored(trade, position, live_position):
    expected_symbol = position.get("symbol") or trade.get("symbol")
    live_symbol = getattr(live_position, "symbol", None)

    if live_symbol is not None and expected_symbol is not None and live_symbol != expected_symbol:
        return False

    expected_identifier = _identity_value(_position_identifier(position))
    live_identifier = _identity_value(
        getattr(
            live_position,
            "identifier",
            getattr(live_position, "position_identifier", None),
        )
    )

    if expected_identifier is not None and live_identifier is not None:
        if expected_identifier != live_identifier:
            return False

    side = _position_side(position, trade)

    if not _live_position_side_matches(live_position, side):
        return False

    expected_magic = position.get("magic", MAGIC_NUMBER)
    live_magic = getattr(live_position, "magic", None)

    if live_magic is not None and expected_magic is not None and live_magic != expected_magic:
        return False

    expected_comment = position.get("comment", COMMENT)
    live_comment = getattr(live_position, "comment", None)

    if live_comment is not None and expected_comment is not None and live_comment != expected_comment:
        return False

    return True


def _position_entry(live_position, stored_position):
    for value in (
        getattr(live_position, "price_open", None),
        stored_position.get("price_open"),
        stored_position.get("fill_price"),
        stored_position.get("entry"),
    ):
        numeric = _to_float(value)

        if numeric is not None:
            return numeric

    return None


def _position_sl(live_position, stored_position, trade):
    for value in (
        getattr(live_position, "sl", None),
        stored_position.get("sl"),
        trade.get("sl"),
    ):
        numeric = _to_float(value)

        if numeric is not None:
            return numeric

    return None


def _position_tp(live_position, stored_position):
    for value in (
        getattr(live_position, "tp", None),
        stored_position.get("tp"),
    ):
        numeric = _to_float(value)

        if numeric is not None:
            return numeric

    return None


def _symbol_point(symbol):
    try:
        info = symbol_info(symbol)
    except Exception:
        info = None

    point = _to_float(getattr(info, "point", None))

    if point is not None and point > 0:
        return point

    digits = getattr(info, "digits", None)

    try:
        digits = int(digits)
    except (TypeError, ValueError):
        digits = None

    if digits is not None and digits >= 0:
        return 10 ** -digits

    return 0.01


def _minimum_stop_distance(symbol):
    try:
        info = symbol_info(symbol)
    except Exception:
        info = None

    stops_level = _to_float(getattr(info, "trade_stops_level", None))

    if stops_level is None:
        stops_level = 0

    return max(stops_level * _symbol_point(symbol), 0)


def _tick(symbol):
    try:
        return symbol_info_tick(symbol)
    except Exception:
        return None


def _stop_level_safe(symbol, side, level, level_type):
    tick = _tick(symbol)

    if tick is None:
        return True

    minimum = _minimum_stop_distance(symbol)
    bid = _to_float(getattr(tick, "bid", None))
    ask = _to_float(getattr(tick, "ask", None))

    if level_type == "sl":
        if side == "BUY" and bid is not None:
            return level < bid - minimum or _same_number(level, bid - minimum)

        if side == "SELL" and ask is not None:
            return level > ask + minimum or _same_number(level, ask + minimum)

    if level_type == "tp":
        if side == "BUY" and ask is not None:
            return level > ask + minimum or _same_number(level, ask + minimum)

        if side == "SELL" and bid is not None:
            return level < bid - minimum or _same_number(level, bid - minimum)

    return True


def _active_trade_targets():
    active = []

    for trade in load_trades():
        positions = []

        for position in trade.get("positions", []):
            if position.get("closed"):
                continue

            if position.get("identity_status") == "pending":
                continue

            ticket = _position_ticket(position)

            if ticket is None:
                continue

            result = query_position(ticket)

            if result.status != POSITION_OPEN:
                continue

            if not _live_matches_stored(trade, position, result.position):
                continue

            positions.append(PositionTarget(trade, position, result.position))

        if positions:
            active.append({
                "trade": trade,
                "positions": positions,
            })

    return active


def _resolve_trade_targets(instruction, active):
    if instruction.action == "break_even" and instruction.global_scope:
        positions = [
            target
            for item in active
            for target in item["positions"]
        ]
        return positions, None

    candidates = active

    if instruction.symbol:
        candidates = [
            item for item in active
            if item["trade"].get("symbol") == instruction.symbol
        ]

    if not candidates:
        return [], "No active PrimeBot positions"

    if len(candidates) > 1:
        symbols = sorted({item["trade"].get("symbol") or "Unknown" for item in candidates})
        symbol_text = ", ".join(
            f"{symbol} signals: {sum(1 for item in candidates if item['trade'].get('symbol') == symbol)}"
            for symbol in symbols
        )
        return [], f"Management instruction ambiguous; Ambiguous target ({symbol_text})"

    return list(candidates[0]["positions"]), None


def _target_label(targets):
    signal_keys = []

    for target in targets:
        trade = target.trade
        key = (
            trade.get("symbol") or target.position.get("symbol") or "Unknown",
            trade.get("message_id"),
        )

        if key not in signal_keys:
            signal_keys.append(key)

    if len(signal_keys) == 1:
        symbol, message_id = signal_keys[0]
        return f"{symbol} signal {message_id}"

    return f"{len(signal_keys)} signals"


def _validate_sl_geometry(targets, new_sl):
    for target in targets:
        trade = target.trade
        position = target.position
        live_position = target.live_position
        side = _position_side(position, trade)
        symbol = position.get("symbol") or trade.get("symbol")
        tp = _position_tp(live_position, position)

        if side == "BUY":
            if tp is not None and new_sl >= tp:
                return False, f"BUY SL {new_sl} is not below TP {tp}"
        elif side == "SELL":
            if tp is not None and new_sl <= tp:
                return False, f"SELL SL {new_sl} is not above TP {tp}"
        else:
            return False, "Unknown trade side"

        if not _stop_level_safe(symbol, side, new_sl, "sl"):
            return False, f"SL {new_sl} violates current broker stop distance"

    return True, None


def _validate_tp_geometry(targets, updates):
    for target in targets:
        trade = target.trade
        position = target.position
        tp_index = position.get("tp_index")

        if tp_index not in updates:
            continue

        live_position = target.live_position
        side = _position_side(position, trade)
        symbol = position.get("symbol") or trade.get("symbol")
        new_tp = updates[tp_index]
        entry = _position_entry(live_position, position)
        sl = _position_sl(live_position, position, trade)

        if side == "BUY":
            if sl is not None and new_tp <= sl:
                return False, f"BUY TP{tp_index} {new_tp} is not above SL {sl}"

            if entry is not None and new_tp <= entry:
                return False, f"BUY TP{tp_index} {new_tp} is not above entry {entry}"
        elif side == "SELL":
            if sl is not None and new_tp >= sl:
                return False, f"SELL TP{tp_index} {new_tp} is not below SL {sl}"

            if entry is not None and new_tp >= entry:
                return False, f"SELL TP{tp_index} {new_tp} is not below entry {entry}"
        else:
            return False, "Unknown trade side"

        if not _stop_level_safe(symbol, side, new_tp, "tp"):
            return False, f"TP{tp_index} {new_tp} violates current broker stop distance"

    return True, None


def _validate_be_level(target, new_sl):
    trade = target.trade
    position = target.position
    live_position = target.live_position
    side = _position_side(position, trade)
    symbol = position.get("symbol") or trade.get("symbol")
    entry = _position_entry(live_position, position)

    if entry is None:
        return False, "Entry price unavailable for BE"

    if side == "BUY":
        if new_sl + BE_TOLERANCE < entry:
            return False, f"BUY BE level {new_sl} is below entry {entry}"
    elif side == "SELL":
        if new_sl - BE_TOLERANCE > entry:
            return False, f"SELL BE level {new_sl} is above entry {entry}"
    else:
        return False, "Unknown trade side"

    if not _stop_level_safe(symbol, side, new_sl, "sl"):
        return False, f"BE level {new_sl} violates current broker stop distance"

    return True, None


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _result_summary(result):
    return {
        key: result.get(key)
        for key in ("success", "ticket", "retcode", "comment", "noop")
        if key in result
    }


def _record_position_metadata(
    target,
    action,
    source_message_id,
    previous_sl=None,
    new_sl=None,
    previous_tp=None,
    new_tp=None,
    result=None,
):
    position = target.position
    position["management_updated_at"] = _now()
    position["management_source_message_id"] = source_message_id
    position["management_action"] = action

    if previous_sl is not None or new_sl is not None:
        position["previous_sl"] = previous_sl
        position["new_sl"] = new_sl

    if previous_tp is not None or new_tp is not None:
        position["previous_tp"] = previous_tp
        position["new_tp"] = new_tp

    if result is not None:
        position["mt5_result"] = _result_summary(result)


def _record_trade_metadata(trades, action, source_message_id, succeeded, failed, noop):
    for trade in trades:
        trade["management_updated_at"] = _now()
        trade["management_source_message_id"] = source_message_id
        trade["management_action"] = action
        trade["management_result"] = {
            "succeeded_tickets": succeeded,
            "failed_tickets": failed,
            "noop_tickets": noop,
        }


def _save_target_trades(targets, action, source_message_id, succeeded, failed, noop):
    seen = []
    trades = []

    for target in targets:
        key = (target.trade.get("chat_id"), target.trade.get("message_id"))

        if key in seen:
            continue

        seen.append(key)
        trades.append(target.trade)

    _record_trade_metadata(trades, action, source_message_id, succeeded, failed, noop)

    for trade in trades:
        update_trade(trade)


def _apply_sl_update(instruction, targets, source_message_id):
    valid, reason = _validate_sl_geometry(targets, instruction.sl)

    if not valid:
        return _blocked_result(reason)

    succeeded = []
    failed = []
    noop = []

    for target in targets:
        ticket = _position_ticket(target.position)
        previous_sl = _position_sl(target.live_position, target.position, target.trade)
        result = modify_trade(ticket, sl=instruction.sl)

        if result.get("success"):
            target.position["sl"] = instruction.sl
            target.trade["sl"] = instruction.sl
            _record_position_metadata(
                target,
                "SL update",
                source_message_id,
                previous_sl=previous_sl,
                new_sl=instruction.sl,
                result=result,
            )

            if result.get("noop"):
                noop.append(ticket)
            else:
                succeeded.append(ticket)
        else:
            failed.append(ticket)
            _record_position_metadata(
                target,
                "SL update",
                source_message_id,
                previous_sl=previous_sl,
                new_sl=instruction.sl,
                result=result,
            )

    _save_target_trades(
        targets,
        "SL update",
        source_message_id,
        succeeded,
        failed,
        noop,
    )

    return _action_result(succeeded, failed, noop, len(targets), f"SL -> {instruction.sl}")


def _apply_tp_update(instruction, targets, source_message_id):
    selected = [
        target for target in targets
        if target.position.get("tp_index") in instruction.tp_updates
    ]

    if not selected:
        return _blocked_result("No open PrimeBot position matched the requested TP index")

    valid, reason = _validate_tp_geometry(selected, instruction.tp_updates)

    if not valid:
        return _blocked_result(reason)

    succeeded = []
    failed = []
    noop = []

    for target in selected:
        ticket = _position_ticket(target.position)
        tp_index = target.position.get("tp_index")
        new_tp = instruction.tp_updates[tp_index]
        previous_tp = _position_tp(target.live_position, target.position)
        result = modify_trade(ticket, tp=new_tp)

        if result.get("success"):
            target.position["tp"] = new_tp
            _record_position_metadata(
                target,
                "TP update",
                source_message_id,
                previous_tp=previous_tp,
                new_tp=new_tp,
                result=result,
            )

            if result.get("noop"):
                noop.append(ticket)
            else:
                succeeded.append(ticket)
        else:
            failed.append(ticket)
            _record_position_metadata(
                target,
                "TP update",
                source_message_id,
                previous_tp=previous_tp,
                new_tp=new_tp,
                result=result,
            )

    action_text = ", ".join(
        f"TP{index} -> {value}"
        for index, value in sorted(instruction.tp_updates.items())
    )
    _save_target_trades(
        selected,
        "TP update",
        source_message_id,
        succeeded,
        failed,
        noop,
    )

    return _action_result(succeeded, failed, noop, len(selected), action_text)


def _apply_break_even(instruction, targets, source_message_id):
    levels = {}

    for target in targets:
        level = (
            instruction.be_level
            if instruction.be_level is not None
            else _position_entry(target.live_position, target.position)
        )
        valid, reason = _validate_be_level(target, level)

        if not valid:
            return _blocked_result(reason)

        levels[_position_ticket(target.position)] = level

    succeeded = []
    failed = []
    noop = []

    for target in targets:
        ticket = _position_ticket(target.position)
        new_sl = levels[ticket]
        previous_sl = _position_sl(target.live_position, target.position, target.trade)
        result = modify_trade(ticket, sl=new_sl)

        if result.get("success"):
            target.position["sl"] = new_sl
            _record_position_metadata(
                target,
                "Break even",
                source_message_id,
                previous_sl=previous_sl,
                new_sl=new_sl,
                result=result,
            )

            if result.get("noop"):
                noop.append(ticket)
            else:
                target.position["break_even"] = True
                succeeded.append(ticket)
        else:
            failed.append(ticket)
            _record_position_metadata(
                target,
                "Break even",
                source_message_id,
                previous_sl=previous_sl,
                new_sl=new_sl,
                result=result,
            )

    _save_target_trades(
        targets,
        "Break even",
        source_message_id,
        succeeded,
        failed,
        noop,
    )

    action_text = (
        f"SL -> {instruction.be_level}"
        if instruction.be_level is not None
        else "SL -> entry"
    )
    return _action_result(succeeded, failed, noop, len(targets), action_text)


def _blocked_result(reason):
    return {
        "blocked": True,
        "reason": reason,
        "succeeded": [],
        "failed": [],
        "noop": [],
        "total": 0,
        "action_text": "No positions modified",
    }


def _action_result(succeeded, failed, noop, total, action_text):
    return {
        "blocked": False,
        "reason": None,
        "succeeded": succeeded,
        "failed": failed,
        "noop": noop,
        "total": total,
        "action_text": action_text,
    }


def _management_key(chat_id, message_id, instruction, timestamp):
    timestamp_text = _timestamp_text(timestamp)
    return (
        f"{chat_id}:{message_id}:"
        f"{instruction.normalized_action or _normalized_action(instruction)}:"
        f"{timestamp_text}"
    )


def _timestamp_text(value):
    if value is None:
        return "no-timestamp"

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return str(value)


def _event_timestamp(event):
    message = getattr(event, "message", None)

    for source in (event, message):
        if source is None:
            continue

        for attr in ("edit_date", "date"):
            value = getattr(source, attr, None)

            if value is not None:
                return value

    return None


def _event_text(event):
    return getattr(event, "raw_text", None) or getattr(event, "text", "")


def _event_chat_id(event):
    return getattr(event, "chat_id", None)


def _event_message_id(event):
    return getattr(event, "id", getattr(event, "message_id", None))


def _is_authorized_source(chat_id):
    try:
        settings = load_settings(validate=False)
    except Exception as exc:
        logger.warning(f"Management source check failed: {exc}")
        return False

    if settings.channel_id is None:
        return True

    try:
        return int(chat_id) == int(settings.channel_id)
    except (TypeError, ValueError):
        return False


def process_management_message(event):
    text = _event_text(event)
    instruction = parse_management_instruction(text)

    if instruction is None:
        return False

    chat_id = _event_chat_id(event)
    message_id = _event_message_id(event)

    if not _is_authorized_source(chat_id):
        logger.warning(
            "Unauthorized management message ignored | "
            f"ChatID={chat_id} Message={message_id}"
        )
        return True

    key = _management_key(
        chat_id,
        message_id,
        instruction,
        _event_timestamp(event),
    )

    if is_management_processed(key):
        logger.info(
            "Duplicate management instruction ignored | "
            f"Message={message_id} Action={instruction.normalized_action}"
        )
        return True

    mark_management_processed(key)

    if instruction.action == "optional":
        notify_optional_management(text)
        return True

    if instruction.action == "close":
        notify_close_instruction(text)
        return True

    if instruction.action == "tp_update" and not instruction.tp_updates:
        notify_management_blocked(
            reason=instruction.reason or "TP update did not include numeric levels",
            message=text,
            details="No positions modified",
        )
        return True

    active = _active_trade_targets()

    if not active:
        notify_management_blocked(
            reason="No active PrimeBot positions",
            message=text,
            details="No positions modified",
        )
        return True

    targets, reason = _resolve_trade_targets(instruction, active)

    if reason:
        notify_management_blocked(
            reason=reason,
            message=text,
            details="No positions modified",
        )
        return True

    if instruction.action == "sl_update":
        result = _apply_sl_update(instruction, targets, message_id)
        action_type = "SL update"
    elif instruction.action == "tp_update":
        result = _apply_tp_update(instruction, targets, message_id)
        action_type = "TP update"
    elif instruction.action == "break_even":
        result = _apply_break_even(instruction, targets, message_id)
        action_type = "Break even"
    else:
        return True

    if result["blocked"]:
        notify_management_blocked(
            reason=result["reason"],
            message=text,
            details="No positions modified",
        )
        return True

    modified = len(result["succeeded"])
    failed = len(result["failed"])
    noop = len(result["noop"])
    result_text = f"{modified}/{result['total']} positions modified"

    if noop:
        result_text += f" ({noop} already matched)"

    if failed:
        result_text += f"; {failed} failed"

    notify_management_action(
        action_type=action_type,
        source_message_id=message_id,
        target=_target_label(targets),
        action=result["action_text"],
        result=result_text,
    )
    return True
