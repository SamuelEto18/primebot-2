from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from types import SimpleNamespace
import time

try:
    import MetaTrader5 as mt5
except ModuleNotFoundError:
    mt5 = None

from config import COMMENT, DEVIATION, LOT_SIZE, MAGIC_NUMBER
from core.logger import logger


def set_mt5_api(api):

    global mt5
    mt5 = api


def _require_mt5():

    if mt5 is None:
        raise RuntimeError("MetaTrader5 package is not available")


MT5_LOCK = RLock()
DEFAULT_HISTORY_LOOKBACK_DAYS = 14
OPEN_POSITION_IDENTITY_POLL_DELAYS = (0, 0.05, 0.1, 0.2, 0.4, 0.75, 1.0)
RECOVERY_IDENTITY_POLL_DELAYS = (0,)
POSITION_OPEN = "open"
POSITION_ABSENT = "absent"
MT5_UNAVAILABLE = "unavailable"
POSITION_QUERY_ERROR = "error"
FILLING_REJECTION = "filling_rejection"

_initialized = False
_reconnecting = False
_last_error = None
_POSITION_HISTORY_UNSUPPORTED = object()


@dataclass
class PositionQueryResult:
    status: str
    ticket: int
    position: object = None
    error: str = None

    @property
    def success(self):
        return self.status in (POSITION_OPEN, POSITION_ABSENT)

    @property
    def is_open(self):
        return self.status == POSITION_OPEN

    @property
    def is_absent(self):
        return self.status == POSITION_ABSENT

    @property
    def unavailable(self):
        return self.status == MT5_UNAVAILABLE

    @property
    def failed(self):
        return self.status == POSITION_QUERY_ERROR


def last_error():

    with MT5_LOCK:
        return _last_error


def _set_error(message):

    global _last_error
    _last_error = message


def initialize():

    global _initialized

    with MT5_LOCK:
        _require_mt5()

        if _initialized and mt5.terminal_info() is not None:
            return True

        if not mt5.initialize():
            error = str(mt5.last_error())
            _initialized = False
            _set_error(error)
            raise RuntimeError(error)

        _initialized = True
        _set_error(None)
        logger.info("MT5 initialized")
        return True


def shutdown():

    global _initialized

    with MT5_LOCK:
        _require_mt5()
        mt5.shutdown()
        _initialized = False


def is_connected():

    with MT5_LOCK:
        try:
            _require_mt5()
            return mt5.terminal_info() is not None
        except Exception as exc:
            _set_error(str(exc))
            return False


def reconnect():

    global _initialized
    global _reconnecting

    with MT5_LOCK:
        _require_mt5()

        if _reconnecting:
            return is_connected()

        _reconnecting = True

        try:
            try:
                mt5.shutdown()
            except Exception:
                pass

            _initialized = False

            if not mt5.initialize():
                error = str(mt5.last_error())
                _set_error(error)
                return False

            _initialized = mt5.terminal_info() is not None

            if _initialized:
                _set_error(None)

            return _initialized

        except Exception as exc:
            _initialized = False
            _set_error(str(exc))
            return False

        finally:
            _reconnecting = False


def terminal_info():

    with MT5_LOCK:
        try:
            _require_mt5()
            return mt5.terminal_info()
        except Exception as exc:
            _set_error(str(exc))
            return None


def account_info():

    with MT5_LOCK:
        _require_mt5()
        try:
            return mt5.account_info()
        except Exception as exc:
            _set_error(str(exc))
            return None


def positions_get(**kwargs):

    with MT5_LOCK:
        _require_mt5()
        try:
            return mt5.positions_get(**kwargs)
        except Exception as exc:
            _set_error(str(exc))
            return None


def query_position(ticket):

    with MT5_LOCK:
        _require_mt5()
        try:
            if mt5.terminal_info() is None:
                return PositionQueryResult(
                    status=MT5_UNAVAILABLE,
                    ticket=ticket,
                    error="MT5 terminal unavailable"
                )

            positions = mt5.positions_get(ticket=ticket)

            if positions is None:
                error = str(mt5.last_error())
                _set_error(error)
                return PositionQueryResult(
                    status=POSITION_QUERY_ERROR,
                    ticket=ticket,
                    error=error
                )

            if len(positions) == 0:
                return PositionQueryResult(
                    status=POSITION_ABSENT,
                    ticket=ticket
                )

            return PositionQueryResult(
                status=POSITION_OPEN,
                ticket=ticket,
                position=positions[0]
            )

        except Exception as exc:
            _set_error(str(exc))
            return PositionQueryResult(
                status=POSITION_QUERY_ERROR,
                ticket=ticket,
                error=str(exc)
            )


def symbol_info(symbol):

    with MT5_LOCK:
        _require_mt5()
        return mt5.symbol_info(symbol)


def symbol_select(symbol, enabled=True):

    with MT5_LOCK:
        _require_mt5()
        return mt5.symbol_select(symbol, enabled)


def symbol_info_tick(symbol):

    with MT5_LOCK:
        _require_mt5()
        return mt5.symbol_info_tick(symbol)


def order_send(request):

    with MT5_LOCK:
        _require_mt5()
        return mt5.order_send(request)


def history_deals_get(date_from=None, date_to=None, **kwargs):

    with MT5_LOCK:
        _require_mt5()
        try:
            if (
                date_from is None
                and date_to is None
                and any(key in kwargs for key in ("position", "ticket"))
            ):
                return mt5.history_deals_get(**kwargs)

            if date_to is None:
                date_to = datetime.now()

            if date_from is None:
                date_from = date_to - timedelta(days=DEFAULT_HISTORY_LOOKBACK_DAYS)

            return mt5.history_deals_get(date_from, date_to, **kwargs)
        except Exception as exc:
            _set_error(str(exc))
            return None


def history_orders_get(date_from=None, date_to=None, **kwargs):

    with MT5_LOCK:
        _require_mt5()
        try:
            if (
                date_from is None
                and date_to is None
                and "ticket" in kwargs
            ):
                return mt5.history_orders_get(**kwargs)

            if date_to is None:
                date_to = datetime.now()

            if date_from is None:
                date_from = date_to - timedelta(days=DEFAULT_HISTORY_LOOKBACK_DAYS)

            return mt5.history_orders_get(date_from, date_to, **kwargs)
        except Exception as exc:
            _set_error(str(exc))
            return None


def position_type_buy():

    _require_mt5()
    return mt5.POSITION_TYPE_BUY


def order_type_buy():

    _require_mt5()
    return mt5.ORDER_TYPE_BUY


def order_type_sell():

    _require_mt5()
    return mt5.ORDER_TYPE_SELL


def trade_action_deal():

    _require_mt5()
    return mt5.TRADE_ACTION_DEAL


def trade_action_sltp():

    _require_mt5()
    return mt5.TRADE_ACTION_SLTP


def order_time_gtc():

    _require_mt5()
    return mt5.ORDER_TIME_GTC


def order_filling_ioc():

    _require_mt5()
    return mt5.ORDER_FILLING_IOC


def trade_retcode_done():

    _require_mt5()
    return mt5.TRADE_RETCODE_DONE


def deal_entry_out():

    _require_mt5()
    return getattr(mt5, "DEAL_ENTRY_OUT", 1)


def deal_reason_tp():

    _require_mt5()
    return getattr(mt5, "DEAL_REASON_TP", None)


def deal_reason_sl():

    _require_mt5()
    return getattr(mt5, "DEAL_REASON_SL", None)


def _getattr_any(item, names, default=None):

    for name in names:
        value = getattr(item, name, None)

        if value is not None:
            return value

    return default


def _same_number(left, right, tolerance=0.0000001):

    if left is None or right is None:
        return False

    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _to_float(value):

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol_point(symbol):

    try:
        info = mt5.symbol_info(symbol)
    except Exception:
        info = None

    point = getattr(info, "point", None) if info is not None else None

    if point is not None:
        point = _to_float(point)

        if point is not None and point > 0:
            return point

    digits = getattr(info, "digits", None) if info is not None else None

    try:
        digits = int(digits)
    except (TypeError, ValueError):
        digits = None

    if digits is not None and digits >= 0:
        return 10 ** -digits

    return 0.01


def _price_tolerance(symbol, points=2):

    point = _symbol_point(symbol)
    return max(point * points, 0.0000001)


def _execution_price_tolerance(symbol):

    return _price_tolerance(symbol, points=max(DEVIATION, 2))


def _identity_value(value):

    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _identity_matches(left, right):

    left = _identity_value(left)
    right = _identity_value(right)

    return left is not None and right is not None and left == right


def _position_identifier(position):

    return _getattr_any(
        position,
        ("identifier", "position_identifier", "position_id", "ticket"),
    )


def _position_ticket(position):

    return getattr(position, "ticket", None)


def _position_side_matches(position, side):

    position_type = getattr(position, "type", None)

    if side == "BUY":
        return position_type == mt5.POSITION_TYPE_BUY

    if side == "SELL":
        return position_type == getattr(mt5, "POSITION_TYPE_SELL", None)

    return False


def _position_time(position):

    value = _getattr_any(position, ("time_msc", "time_update_msc", "time"))

    if value is None:
        return None

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if value > 100000000000:
        return value / 1000

    return value


def _position_matches_primebot_request(
    position,
    symbol,
    side,
    volume,
    sl=None,
    tp=None,
    requested_price=None,
    fill_price=None,
    opened_after=None,
    excluded_tickets=None,
    excluded_identifiers=None,
    check_execution_price=True,
):

    if getattr(position, "symbol", None) != symbol:
        return False

    ticket = _position_ticket(position)
    identifier = _position_identifier(position)
    excluded_tickets = {_identity_value(value) for value in (excluded_tickets or [])}
    excluded_identifiers = {
        _identity_value(value) for value in (excluded_identifiers or [])
    }

    if _identity_value(ticket) in excluded_tickets:
        return False

    if _identity_value(identifier) in excluded_identifiers:
        return False

    if not _position_side_matches(position, side):
        return False

    if not _same_number(getattr(position, "volume", None), volume, tolerance=0.00001):
        return False

    if getattr(position, "magic", None) != MAGIC_NUMBER:
        return False

    if getattr(position, "comment", None) != COMMENT:
        return False

    level_tolerance = _price_tolerance(symbol)

    if sl is not None and not _same_number(
        getattr(position, "sl", None),
        sl,
        tolerance=level_tolerance,
    ):
        return False

    if tp is not None and not _same_number(
        getattr(position, "tp", None),
        tp,
        tolerance=level_tolerance,
    ):
        return False

    expected_prices = [
        value for value in (fill_price, requested_price)
        if value is not None
    ]

    if check_execution_price and expected_prices:
        price_open = getattr(position, "price_open", None)
        execution_tolerance = _execution_price_tolerance(symbol)

        if price_open is not None and not any(
            _same_number(price_open, expected, tolerance=execution_tolerance)
            for expected in expected_prices
        ):
            return False

    position_time = _position_time(position)

    if opened_after is not None:
        if position_time is None:
            return False

        if position_time < opened_after:
            return False

    return True


def _result_value(result, name):

    return getattr(result, name, None) if result is not None else None


def _result_success(result):

    if result is None:
        return False

    return getattr(result, "retcode", None) == mt5.TRADE_RETCODE_DONE


def _accepted_retcodes():

    names = (
        "TRADE_RETCODE_DONE",
        "TRADE_RETCODE_DONE_PARTIAL",
        "TRADE_RETCODE_PLACED",
    )
    codes = set()

    for name in names:
        code = getattr(mt5, name, None)

        if code is not None:
            codes.add(code)

    return codes


def _result_accepted(result):

    if result is None:
        return False

    return getattr(result, "retcode", None) in _accepted_retcodes()


def _retcode_invalid_filling():

    return getattr(mt5, "TRADE_RETCODE_INVALID_FILL", None)


def _result_is_filling_rejection(result):

    if result is None:
        return False

    retcode = getattr(result, "retcode", None)
    invalid_filling = _retcode_invalid_filling()

    if invalid_filling is not None and retcode == invalid_filling:
        return True

    comment = str(getattr(result, "comment", "")).lower()

    return "fill" in comment or "filling" in comment or "unsupported filling" in comment


def _order_filling_fok():

    return getattr(mt5, "ORDER_FILLING_FOK", 0)


def _order_filling_ioc():

    return getattr(mt5, "ORDER_FILLING_IOC", 1)


def _order_filling_return():

    return getattr(mt5, "ORDER_FILLING_RETURN", 2)


def _symbol_filling_flag(name, default):

    return getattr(mt5, name, default)


def _return_allowed_for_execution(info):

    execution = getattr(info, "trade_exemode", getattr(info, "execution_mode", None))
    market_execution = getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", None)

    if execution is None or market_execution is None:
        return True

    return execution != market_execution


def _supported_filling_policies(info):

    explicit = getattr(info, "supported_fillings", None)
    capability_known = False

    if explicit is not None:
        capability_known = True
        policies = list(explicit)
    else:
        filling_mode = getattr(info, "filling_mode", None)
        policies = []

        if filling_mode is not None:
            capability_known = True

            if filling_mode & _symbol_filling_flag("SYMBOL_FILLING_FOK", 1):
                policies.append(_order_filling_fok())

            if filling_mode & _symbol_filling_flag("SYMBOL_FILLING_IOC", 2):
                policies.append(_order_filling_ioc())

            if filling_mode & _symbol_filling_flag("SYMBOL_FILLING_RETURN", 4):
                policies.append(_order_filling_return())

        if not capability_known:
            policies = [
                _order_filling_fok(),
                _order_filling_ioc(),
                _order_filling_return(),
            ]

    had_explicit_policy = capability_known

    if not _return_allowed_for_execution(info):
        policies = [p for p in policies if p != _order_filling_return()]

    if had_explicit_policy and not policies:
        return []

    seen = []

    for policy in policies:
        if policy not in seen:
            seen.append(policy)

    return seen or [_order_filling_ioc()]


def _send_order_with_filling_retry(request, info):

    policies = _supported_filling_policies(info)
    attempts = []

    if not policies:
        return SimpleNamespace(
            retcode=None,
            order=None,
            deal=None,
            comment="No broker-supported filling policy"
        ), None, attempts

    for index, policy in enumerate(policies[:2]):
        request_with_policy = dict(request)
        request_with_policy["type_filling"] = policy
        logger.debug(f"Selected MT5 filling policy {policy} for {request['symbol']}")
        result = mt5.order_send(request_with_policy)
        attempts.append({"policy": policy, "result": result})

        if _result_success(result):
            return result, policy, attempts

        if not _result_is_filling_rejection(result):
            return result, policy, attempts

        if index == 0 and len(policies) > 1:
            logger.warning(
                f"MT5 filling policy rejected for {request['symbol']} | "
                f"Policy={policy} Retcode={getattr(result, 'retcode', None)}"
            )
            continue

        return result, policy, attempts

    return None, None, attempts


def _get_positions_for_symbol(symbol):

    positions = mt5.positions_get(symbol=symbol)

    if positions is None:
        positions = mt5.positions_get()

    if positions is None:
        return []

    return list(positions)


def _get_positions_for_identity(symbol, position_identity):

    if position_identity is None:
        return _get_positions_for_symbol(symbol)

    positions = []
    direct_positions = mt5.positions_get(ticket=position_identity)

    if direct_positions is not None:
        positions.extend(list(direct_positions))

    seen_tickets = {
        _identity_value(_position_ticket(position))
        for position in positions
    }

    for position in _get_positions_for_symbol(symbol):
        ticket = _identity_value(_position_ticket(position))

        if ticket in seen_tickets:
            continue

        positions.append(position)
        seen_tickets.add(ticket)

    return positions


def _history_deals_for_window(start_time, lookback_seconds=30):

    date_to = datetime.now() + timedelta(seconds=5)
    date_from = start_time - timedelta(seconds=lookback_seconds)
    deals = mt5.history_deals_get(date_from, date_to)

    if deals is None:
        return []

    return list(deals)


def _deal_position_identifier(deal):

    if deal is None:
        return None

    return _getattr_any(
        deal,
        (
            "position_id",
            "position",
            "position_identifier",
            "position_ticket",
        )
    )


def _find_deal(deals, result):

    deal_ticket = _result_value(result, "deal")
    order_ticket = _result_value(result, "order")

    for deal in deals:
        if deal_ticket is not None and getattr(deal, "ticket", None) == deal_ticket:
            return deal

    for deal in deals:
        if order_ticket is not None and getattr(deal, "order", None) == order_ticket:
            return deal

    return None


def _result_position_identifier(result):

    return _getattr_any(
        result,
        (
            "position_id",
            "position",
            "position_identifier",
            "position_ticket",
        )
    )


def _position_matches_identity(position, position_identity):

    if position_identity is None:
        return True

    return (
        _identity_matches(_position_identifier(position), position_identity)
        or _identity_matches(_position_ticket(position), position_identity)
    )


def _candidate_positions(
    symbol,
    side,
    opened_after,
    sl,
    tp,
    requested_price,
    fill_price,
    position_identity=None,
    order_ticket=None,
    excluded_tickets=None,
    excluded_identifiers=None,
    check_execution_price=True,
):

    positions = _get_positions_for_identity(symbol, position_identity)
    candidates = []

    for position in positions:
        if not _position_matches_primebot_request(
            position,
            symbol,
            side,
            LOT_SIZE,
            sl=sl,
            tp=tp,
            requested_price=requested_price,
            fill_price=fill_price,
            opened_after=opened_after,
            excluded_tickets=excluded_tickets,
            excluded_identifiers=excluded_identifiers,
            check_execution_price=check_execution_price,
        ):
            continue

        if not _position_matches_identity(position, position_identity):
            continue

        position_order = getattr(position, "order", None)

        if order_ticket is not None and position_order is not None:
            if position_order != order_ticket:
                continue

        candidates.append(position)

    return candidates


def _select_unique_position(candidates):

    if len(candidates) != 1:
        return None

    return candidates[0]


def _resolved_price(position, result, requested_price, authoritative_fill_price=None):

    if authoritative_fill_price is not None:
        return authoritative_fill_price, "opening_deal_price"

    position_price = getattr(position, "price_open", None) if position is not None else None

    if position_price is not None:
        return position_price, "position_price_open"

    result_price = _result_value(result, "price")

    if result_price is not None:
        return result_price, "execution_result"

    return requested_price, "requested_price"


def _position_identity_result(
    position,
    result,
    requested_price,
    selected_filling,
    symbol,
    side,
    sl,
    tp,
    attempts,
    request_time=None,
    resolution_method=None,
    authoritative_fill_price=None,
):

    price, source = _resolved_price(
        position,
        result,
        requested_price,
        authoritative_fill_price=authoritative_fill_price,
    )
    position_ticket = _position_ticket(position)
    position_identifier = _position_identifier(position)
    order_ticket = _result_value(result, "order")
    deal_ticket = _result_value(result, "deal")

    return {
        "success": True,
        "accepted": True,
        "accepted_identity_pending": False,
        "identity_status": "resolved",
        "identity_resolution": resolution_method,
        "ticket": position_ticket,
        "position_ticket": position_ticket,
        "position_identifier": position_identifier,
        "position_id": position_identifier,
        "order_ticket": order_ticket,
        "deal_ticket": deal_ticket,
        "symbol": symbol,
        "side": side,
        "volume": getattr(position, "volume", LOT_SIZE),
        "requested_price": requested_price,
        "fill_price": price,
        "price_open": price,
        "price": price,
        "entry_source": source,
        "sl": sl,
        "tp": tp,
        "magic": getattr(position, "magic", MAGIC_NUMBER),
        "comment": getattr(position, "comment", COMMENT),
        "retcode": getattr(result, "retcode", None),
        "result_comment": getattr(result, "comment", None),
        "selected_filling": selected_filling,
        "filling_attempts": [attempt["policy"] for attempt in attempts],
        "order_attempt_started_at": (
            request_time.isoformat() if request_time is not None else None
        ),
    }


def _pending_identity_result(
    symbol,
    side,
    requested_price,
    sl,
    tp,
    result,
    selected_filling,
    attempts,
    request_time,
    resolution_attempts,
    comment=None,
):

    accepted = _result_accepted(result)
    fill_price = _result_value(result, "price")

    return {
        "success": False,
        "accepted": accepted,
        "accepted_identity_pending": accepted,
        "identity_status": "pending" if accepted else "unresolved",
        "unresolved": True,
        "ticket": None,
        "position_ticket": None,
        "position_identifier": None,
        "position_id": None,
        "order_ticket": _result_value(result, "order"),
        "deal_ticket": _result_value(result, "deal"),
        "symbol": symbol,
        "side": side,
        "volume": LOT_SIZE,
        "requested_price": requested_price,
        "fill_price": fill_price,
        "price": fill_price or requested_price,
        "tp": tp,
        "sl": sl,
        "magic": MAGIC_NUMBER,
        "comment": comment or (
            "MT5 accepted the order, but position identity is pending"
            if accepted
            else "MT5 position identity unresolved"
        ),
        "retcode": getattr(result, "retcode", None),
        "result_comment": getattr(result, "comment", None),
        "selected_filling": selected_filling,
        "filling_attempts": [attempt["policy"] for attempt in attempts],
        "order_attempt_started_at": (
            request_time.isoformat() if request_time is not None else None
        ),
        "identity_resolution_attempts": resolution_attempts,
    }


def _resolve_opened_position_once(
    symbol,
    side,
    requested_price,
    sl,
    tp,
    result,
    request_time,
    excluded_position_tickets=None,
    excluded_position_identifiers=None,
):

    deals = _history_deals_for_window(request_time)
    deal = _find_deal(deals, result)
    opened_after = request_time.timestamp() - 0.5
    authoritative_fill_price = (
        getattr(deal, "price", None) if deal is not None else None
    )
    fill_price = authoritative_fill_price

    if fill_price is None:
        fill_price = _result_value(result, "price")
    order_ticket = _result_value(result, "order")
    deal_position_id = _deal_position_identifier(deal)

    if deal_position_id is not None:
        candidates = _candidate_positions(
            symbol,
            side,
            opened_after,
            sl,
            tp,
            requested_price,
            fill_price,
            position_identity=deal_position_id,
            order_ticket=order_ticket,
            excluded_tickets=excluded_position_tickets,
            excluded_identifiers=excluded_position_identifiers,
            check_execution_price=False,
        )
        position = _select_unique_position(candidates)

        if position is not None:
            return (
                position,
                "opening_deal_position_id",
                len(candidates),
                authoritative_fill_price,
            )

        return (
            None,
            "opening_deal_position_id",
            len(candidates),
            authoritative_fill_price,
        )

    result_position_id = _result_position_identifier(result)

    if result_position_id is not None:
        candidates = _candidate_positions(
            symbol,
            side,
            opened_after,
            sl,
            tp,
            requested_price,
            fill_price,
            position_identity=result_position_id,
            order_ticket=order_ticket,
            excluded_tickets=excluded_position_tickets,
            excluded_identifiers=excluded_position_identifiers,
        )
        position = _select_unique_position(candidates)

        if position is not None:
            return position, "returned_position_id", len(candidates), None

        return None, "returned_position_id", len(candidates), None

    candidates = _candidate_positions(
        symbol,
        side,
        opened_after,
        sl,
        tp,
        requested_price,
        fill_price,
        order_ticket=order_ticket,
        excluded_tickets=excluded_position_tickets,
        excluded_identifiers=excluded_position_identifiers,
    )
    position = _select_unique_position(candidates)

    if position is not None:
        return position, "strict_fallback", len(candidates), None

    return None, "strict_fallback", len(candidates), None


def _resolve_opened_position(
    symbol,
    side,
    requested_price,
    sl,
    tp,
    result,
    selected_filling,
    request_time,
    attempts,
    excluded_position_tickets=None,
    excluded_position_identifiers=None,
    poll_delays=None,
    log_failure=True,
):

    if poll_delays is None:
        poll_delays = OPEN_POSITION_IDENTITY_POLL_DELAYS

    resolution_attempts = []
    position = None
    method = None
    candidate_count = 0
    authoritative_fill_price = None

    for delay in poll_delays:
        if delay:
            time.sleep(delay)

        position, method, candidate_count, authoritative_fill_price = _resolve_opened_position_once(
            symbol,
            side,
            requested_price,
            sl,
            tp,
            result,
            request_time,
            excluded_position_tickets=excluded_position_tickets,
            excluded_position_identifiers=excluded_position_identifiers,
        )
        resolution_attempts.append({
            "method": method,
            "candidates": candidate_count,
        })

        if position is not None:
            break

    if position is None:
        if log_failure:
            logger.error(
                "Unable to resolve MT5 position identity | "
                f"Symbol={symbol} Side={side} "
                f"Order={_result_value(result, 'order')} "
                f"Deal={_result_value(result, 'deal')} "
                f"Method={method} "
                f"Candidates={candidate_count}"
            )
        return _pending_identity_result(
            symbol,
            side,
            requested_price,
            sl,
            tp,
            result,
            selected_filling,
            attempts,
            request_time,
            resolution_attempts,
        )

    return _position_identity_result(
        position,
        result,
        requested_price,
        selected_filling,
        symbol,
        side,
        sl,
        tp,
        attempts,
        request_time=request_time,
        resolution_method=method,
        authoritative_fill_price=authoritative_fill_price,
    )


def open_trade(
    symbol,
    side,
    sl,
    tp,
    excluded_position_tickets=None,
    excluded_position_identifiers=None,
    identity_poll_delays=None,
):

    with MT5_LOCK:
        _require_mt5()
        info = mt5.symbol_info(symbol)

        if info is None:
            return {
                "success": False,
                "ticket": None,
                "price": None,
                "comment": f"{symbol} not found"
            }

        if not info.visible:
            mt5.symbol_select(symbol, True)

        tick = mt5.symbol_info_tick(symbol)

        if tick is None:
            return {
                "success": False,
                "ticket": None,
                "price": None,
                "comment": "No market price"
            }

        if side == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        request_time = datetime.now()
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": LOT_SIZE,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": DEVIATION,
            "magic": MAGIC_NUMBER,
            "comment": COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result, selected_filling, attempts = _send_order_with_filling_retry(request, info)

        if _result_success(result):
            return _resolve_opened_position(
                symbol,
                side,
                price,
                sl,
                tp,
                result,
                selected_filling,
                request_time,
                attempts,
                excluded_position_tickets=excluded_position_tickets,
                excluded_position_identifiers=excluded_position_identifiers,
                poll_delays=identity_poll_delays,
            )

        if result is None:
            resolved = _resolve_opened_position(
                symbol,
                side,
                price,
                sl,
                tp,
                result,
                selected_filling,
                request_time,
                attempts,
                excluded_position_tickets=excluded_position_tickets,
                excluded_position_identifiers=excluded_position_identifiers,
                poll_delays=identity_poll_delays,
            )

            if resolved.get("success"):
                return resolved

            resolved["comment"] = "No response from MT5; no matching position resolved"
            return resolved

        return {
            "success": False,
            "ticket": None,
            "position_ticket": None,
            "requested_price": price,
            "price": getattr(result, "price", None) or price,
            "tp": tp,
            "sl": sl,
            "retcode": result.retcode,
            "comment": result.comment,
            "selected_filling": selected_filling,
            "filling_attempts": [attempt["policy"] for attempt in attempts],
        }


def _parse_order_attempt_time(value):

    if value is None:
        return datetime.now()

    if isinstance(value, datetime):
        return value

    numeric = _to_float(value)

    if numeric is not None:
        return datetime.fromtimestamp(numeric)

    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now()


def _order_position_identifier(order):

    return _getattr_any(
        order,
        ("position_id", "position", "position_identifier", "position_ticket"),
    )


def _order_time(order):

    value = _getattr_any(order, ("time_done_msc", "time_setup_msc", "time_done", "time_setup"))
    numeric = _to_float(value)

    if numeric is None:
        return None

    if numeric > 100000000000:
        return numeric / 1000

    return numeric


def _order_side_matches(order, side):

    order_type = getattr(order, "type", None)

    if side == "BUY":
        return order_type == mt5.ORDER_TYPE_BUY

    if side == "SELL":
        return order_type == mt5.ORDER_TYPE_SELL

    return False


def _history_order_matches_pending_record(order, record, request_time):

    if getattr(order, "ticket", None) != record.get("order_ticket"):
        return False

    if getattr(order, "symbol", None) != record.get("symbol"):
        return False

    if not _order_side_matches(order, record.get("side")):
        return False

    volume = _getattr_any(order, ("volume_initial", "volume"))
    if not _same_number(volume, record.get("volume", LOT_SIZE), tolerance=0.00001):
        return False

    if getattr(order, "magic", None) != MAGIC_NUMBER:
        return False

    if getattr(order, "comment", None) != COMMENT:
        return False

    level_tolerance = _price_tolerance(record.get("symbol"))

    for key in ("sl", "tp"):
        expected = record.get(key)
        if expected is None:
            continue
        if not _same_number(
            getattr(order, key, None),
            expected,
            tolerance=level_tolerance,
        ):
            return False

    order_time = _order_time(order)
    if order_time is None or order_time < request_time.timestamp() - 0.5:
        return False

    return True


def _opening_deal_matches_pending_record(deal, record, position_id, request_time):

    if deal is None or not _deal_is_opening(deal):
        return False

    if not _identity_matches(_deal_position_identifier(deal), position_id):
        return False

    if getattr(deal, "order", None) != record.get("order_ticket"):
        return False

    expected_deal_ticket = record.get("deal_ticket")
    if (
        expected_deal_ticket is not None
        and getattr(deal, "ticket", None) != expected_deal_ticket
    ):
        return False

    if getattr(deal, "symbol", None) != record.get("symbol"):
        return False

    if getattr(deal, "magic", None) != MAGIC_NUMBER:
        return False

    if getattr(deal, "comment", None) != COMMENT:
        return False

    deal_type = getattr(deal, "type", None)
    if record.get("side") == "BUY" and deal_type != deal_type_buy():
        return False
    if record.get("side") == "SELL" and deal_type != deal_type_sell():
        return False

    if not _same_number(
        getattr(deal, "volume", None),
        record.get("volume", LOT_SIZE),
        tolerance=0.00001,
    ):
        return False

    deal_time = _deal_time(deal)
    if deal_time is None or deal_time < request_time.timestamp() - 0.5:
        return False

    return True


def _closed_position_identity_result(record, order, opening_deal, closing_deals):

    position_id = _order_position_identifier(order)
    close_volume = round(sum(_deal_volume(deal) for deal in closing_deals), 10)
    final_deal = closing_deals[-1]
    metadata = _deal_close_metadata(final_deal, close_volume=close_volume)
    profits = [
        _to_float(getattr(deal, "profit", None))
        for deal in closing_deals
    ]
    known_profits = [profit for profit in profits if profit is not None]
    profit = round(sum(known_profits), 10) if known_profits else None
    metadata["close_profit"] = profit
    fill_price = getattr(opening_deal, "price", None)

    return {
        "success": True,
        "accepted": True,
        "accepted_identity_pending": False,
        "identity_status": "resolved",
        "identity_resolution": "historical_order_position_id",
        "ticket": position_id,
        "position_ticket": position_id,
        "position_identifier": position_id,
        "position_id": position_id,
        "order_ticket": getattr(order, "ticket", None),
        "deal_ticket": getattr(opening_deal, "ticket", None),
        "symbol": record.get("symbol"),
        "side": record.get("side"),
        "volume": getattr(opening_deal, "volume", record.get("volume", LOT_SIZE)),
        "requested_price": record.get("requested_price"),
        "fill_price": fill_price,
        "price_open": fill_price,
        "price": fill_price,
        "entry_source": "opening_deal_price",
        "opened_at": _deal_time_iso(opening_deal),
        "sl": record.get("sl"),
        "tp": record.get("tp"),
        "magic": MAGIC_NUMBER,
        "comment": COMMENT,
        "closed": True,
        "break_even": False,
        "profit": profit,
        **metadata,
    }


def _recover_pending_position_from_history(
    record,
    request_time,
    excluded_position_tickets=None,
    excluded_position_identifiers=None,
):

    order_ticket = record.get("order_ticket")
    if order_ticket is None or not hasattr(mt5, "history_orders_get"):
        return None

    try:
        orders = mt5.history_orders_get(ticket=order_ticket)
    except (AttributeError, TypeError):
        return None
    except Exception as exc:
        _set_error(str(exc))
        return None

    if not orders:
        return None

    matching_orders = [
        order for order in orders
        if _history_order_matches_pending_record(order, record, request_time)
    ]
    if len(matching_orders) != 1:
        return None

    order = matching_orders[0]
    position_id = _order_position_identifier(order)
    if position_id is None:
        return None

    excluded_tickets = {
        _identity_value(value) for value in (excluded_position_tickets or [])
    }
    excluded_identifiers = {
        _identity_value(value) for value in (excluded_position_identifiers or [])
    }
    identity = _identity_value(position_id)
    if identity in excluded_tickets or identity in excluded_identifiers:
        return None

    try:
        deals = mt5.history_deals_get(position=position_id)
    except Exception as exc:
        _set_error(str(exc))
        return None

    if not deals:
        return None

    opening_deals = [
        deal for deal in deals
        if _opening_deal_matches_pending_record(
            deal,
            record,
            position_id,
            request_time,
        )
    ]
    if len(opening_deals) != 1:
        return None

    opening_deal = opening_deals[0]
    fill_price = getattr(opening_deal, "price", None)
    opened_after = request_time.timestamp() - 0.5
    open_positions = mt5.positions_get(ticket=position_id)

    if open_positions is None:
        return None

    candidates = [
        position for position in open_positions
        if _position_matches_primebot_request(
            position,
            record.get("symbol"),
            record.get("side"),
            record.get("volume", LOT_SIZE),
            sl=record.get("sl"),
            tp=record.get("tp"),
            requested_price=record.get("requested_price"),
            fill_price=fill_price,
            opened_after=opened_after,
            excluded_tickets=excluded_position_tickets,
            excluded_identifiers=excluded_position_identifiers,
            check_execution_price=False,
        )
        and _position_matches_identity(position, position_id)
    ]

    if candidates:
        position = _select_unique_position(candidates)
        if position is None:
            return None
        result = SimpleNamespace(
            retcode=record.get("retcode"),
            order=getattr(order, "ticket", None),
            deal=getattr(opening_deal, "ticket", None),
            price=fill_price,
            comment=record.get("result_comment"),
        )
        return _position_identity_result(
            position,
            result,
            record.get("requested_price"),
            record.get("selected_filling"),
            record.get("symbol"),
            record.get("side"),
            record.get("sl"),
            record.get("tp"),
            [],
            request_time=request_time,
            resolution_method="historical_order_position_id",
            authoritative_fill_price=fill_price,
        )

    identities = _position_history_identities(position_id, position_id)
    closing_deals = _valid_closing_deals(
        list(deals),
        identities,
        symbol=record.get("symbol"),
        side=record.get("side"),
        opening_deal=opening_deal,
    )
    close_volume = sum(_deal_volume(deal) for deal in closing_deals)
    if not closing_deals or not _close_volume_covers_expected(
        close_volume,
        record.get("volume", LOT_SIZE),
    ):
        return None

    return _closed_position_identity_result(
        record,
        order,
        opening_deal,
        closing_deals,
    )


def recover_pending_position_identity(
    record,
    excluded_position_tickets=None,
    excluded_position_identifiers=None,
    identity_poll_delays=None,
):

    with MT5_LOCK:
        _require_mt5()
        request_time = _parse_order_attempt_time(
            record.get("order_attempt_started_at")
            or record.get("created_at")
            or record.get("timestamp")
        )
        result = SimpleNamespace(
            retcode=record.get("retcode"),
            order=record.get("order_ticket"),
            deal=record.get("deal_ticket"),
            price=record.get("fill_price") or record.get("price"),
            comment=record.get("result_comment"),
            position=record.get("returned_position_id"),
            position_id=record.get("returned_position_id"),
        )

        historical = _recover_pending_position_from_history(
            record,
            request_time,
            excluded_position_tickets=excluded_position_tickets,
            excluded_position_identifiers=excluded_position_identifiers,
        )

        if historical is not None:
            return historical

        return _resolve_opened_position(
            record.get("symbol"),
            record.get("side"),
            record.get("requested_price"),
            record.get("sl"),
            record.get("tp"),
            result,
            record.get("selected_filling"),
            request_time,
            [],
            excluded_position_tickets=excluded_position_tickets,
            excluded_position_identifiers=excluded_position_identifiers,
            poll_delays=identity_poll_delays or RECOVERY_IDENTITY_POLL_DELAYS,
            log_failure=False,
        )


def modify_trade(ticket, sl=None, tp=None):

    with MT5_LOCK:
        _require_mt5()
        positions = mt5.positions_get(ticket=ticket)

        if not positions:
            return {
                "success": False,
                "ticket": ticket,
                "comment": "Position not found"
            }

        position = positions[0]
        symbol = position.symbol
        requested_sl = sl if sl is not None else position.sl
        requested_tp = tp if tp is not None else position.tp
        tolerance = _price_tolerance(symbol)

        if (
            _same_number(requested_sl, position.sl, tolerance=tolerance)
            and _same_number(requested_tp, position.tp, tolerance=tolerance)
        ):
            logger.info(
                f"MT5 modify no changes | "
                f"Ticket={ticket} SL={requested_sl} TP={requested_tp}"
            )
            return {
                "success": True,
                "ticket": ticket,
                "comment": "No changes",
                "noop": True,
            }

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol,
            "sl": requested_sl,
            "tp": requested_tp,
        }

        result = mt5.order_send(request)

        if result is None:
            return {
                "success": False,
                "ticket": ticket,
                "comment": "No response from MT5"
            }

        success = result.retcode == mt5.TRADE_RETCODE_DONE
        comment = str(getattr(result, "comment", ""))
        no_changes = "no changes" in comment.lower()

        if not success and no_changes:
            positions = mt5.positions_get(ticket=ticket)

            if positions:
                current = positions[0]

                if (
                    _same_number(requested_sl, current.sl, tolerance=tolerance)
                    and _same_number(requested_tp, current.tp, tolerance=tolerance)
                ):
                    logger.info(
                        f"MT5 modify no changes | "
                        f"Ticket={ticket} SL={requested_sl} TP={requested_tp}"
                    )
                    return {
                        "success": True,
                        "ticket": ticket,
                        "retcode": result.retcode,
                        "comment": result.comment,
                        "noop": True,
                    }

        return {
            "success": success,
            "ticket": ticket,
            "retcode": result.retcode,
            "comment": result.comment,
            "noop": False,
        }


def close_trade(ticket):

    with MT5_LOCK:
        _require_mt5()
        positions = mt5.positions_get(ticket=ticket)

        if not positions:
            return {
                "success": False,
                "ticket": ticket,
                "comment": "Position not found"
            }

        position = positions[0]
        info = mt5.symbol_info(position.symbol)
        tick = mt5.symbol_info_tick(position.symbol)

        if tick is None:
            return {
                "success": False,
                "ticket": ticket,
                "comment": "No market price"
            }

        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": order_type,
            "price": price,
            "deviation": DEVIATION,
            "magic": MAGIC_NUMBER,
            "comment": COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result, selected_filling, attempts = _send_order_with_filling_retry(request, info)

        if result is None:
            return {
                "success": False,
                "ticket": ticket,
                "comment": "No response from MT5",
                "selected_filling": selected_filling,
                "filling_attempts": [attempt["policy"] for attempt in attempts],
            }

        return {
            "success": result.retcode == mt5.TRADE_RETCODE_DONE,
            "ticket": ticket,
            "retcode": result.retcode,
            "comment": result.comment,
            "selected_filling": selected_filling,
            "filling_attempts": [attempt["policy"] for attempt in attempts],
        }


def _position_history_identities(position_ticket, position_identifier=None):

    identities = []

    for value in (position_identifier, position_ticket):
        value = _identity_value(value)

        if value is None:
            continue

        if value not in identities:
            identities.append(value)

    return identities


def _deal_matches_position_identity(deal, position_ticket, position_identifier=None):

    identities = _position_history_identities(position_ticket, position_identifier)
    deal_position_id = _deal_position_identifier(deal)

    return any(
        _identity_matches(deal_position_id, identity)
        for identity in identities
    )


def _deal_matches_any_position_identity(deal, identities):

    deal_position_id = _deal_position_identifier(deal)

    return any(
        _identity_matches(deal_position_id, identity)
        for identity in identities
    )


def _deal_symbol_matches(deal, symbol):

    if symbol is None:
        return True

    deal_symbol = getattr(deal, "symbol", None)

    if deal_symbol is None:
        return True

    return deal_symbol == symbol


def deal_entry_in():

    _require_mt5()
    return getattr(mt5, "DEAL_ENTRY_IN", 0)


def deal_entry_out_by():

    _require_mt5()
    return getattr(mt5, "DEAL_ENTRY_OUT_BY", None)


def _deal_is_exit(deal):

    entry = getattr(deal, "entry", None)
    exit_entries = {deal_entry_out()}
    entry_out_by = deal_entry_out_by()

    if entry_out_by is not None:
        exit_entries.add(entry_out_by)

    return entry in exit_entries


def _deal_is_opening(deal):

    return getattr(deal, "entry", None) == deal_entry_in()


def deal_type_buy():

    _require_mt5()
    return getattr(mt5, "DEAL_TYPE_BUY", 0)


def deal_type_sell():

    _require_mt5()
    return getattr(mt5, "DEAL_TYPE_SELL", 1)


def _deal_close_type_matches_side(deal, side):

    if side is None:
        return True

    deal_type = getattr(deal, "type", None)

    if deal_type is None:
        return True

    side = str(side).upper()

    if side == "BUY":
        return deal_type == deal_type_sell()

    if side == "SELL":
        return deal_type == deal_type_buy()

    return True


def _deal_has_positive_volume(deal):

    volume = _to_float(getattr(deal, "volume", None))

    return volume is not None and volume > 0


def _deal_volume(deal):

    return _to_float(getattr(deal, "volume", None)) or 0.0


def _deal_reason_is_tp(deal):

    reason_tp = deal_reason_tp()
    return reason_tp is not None and getattr(deal, "reason", None) == reason_tp


def _deal_reason_is_sl(deal):

    reason_sl = deal_reason_sl()
    return reason_sl is not None and getattr(deal, "reason", None) == reason_sl


def _manual_close_reasons():

    names = (
        "DEAL_REASON_CLIENT",
        "DEAL_REASON_MOBILE",
        "DEAL_REASON_WEB",
    )
    reasons = {0}

    for name in names:
        reason = getattr(mt5, name, None)

        if reason is not None:
            reasons.add(reason)

    return reasons


def _normalize_close_reason(deal):

    reason = getattr(deal, "reason", None)

    if _deal_reason_is_tp(deal):
        return "take_profit"

    if _deal_reason_is_sl(deal):
        return "stop_loss"

    if reason is None:
        return "unknown_confirmed_close"

    if reason in _manual_close_reasons():
        return "manual_close"

    return "other_close"


def _deal_price_matches_tp(deal, tp, tolerance=0.05):

    price = getattr(deal, "price", None)

    if price is None or tp is None:
        return False

    return abs(float(price) - float(tp)) <= tolerance


def _deal_time(deal):

    value = _getattr_any(deal, ("time_msc", "time"))

    if value is None:
        return None

    numeric = _to_float(value)

    if numeric is None:
        return None

    if numeric > 100000000000:
        numeric = numeric / 1000

    return numeric


def _deal_time_iso(deal):

    timestamp = _deal_time(deal)

    if timestamp is None:
        return None

    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _deal_close_metadata(deal, close_volume=None):

    if deal is None:
        return {}

    close_reason = _normalize_close_reason(deal)

    return {
        "close_deal_ticket": getattr(deal, "ticket", None),
        "close_order_ticket": getattr(deal, "order", None),
        "close_price": getattr(deal, "price", None),
        "closed_at": _deal_time_iso(deal),
        "close_volume": (
            close_volume
            if close_volume is not None
            else getattr(deal, "volume", None)
        ),
        "close_profit": getattr(deal, "profit", None),
        "close_reason": close_reason,
        "take_profit_confirmed": close_reason == "take_profit",
    }


def _position_has_remaining_volume(position):

    if position is None:
        return False

    volume = getattr(position, "volume", 0)

    try:
        return float(volume) > 0
    except (TypeError, ValueError):
        return True


def _history_deals_for_position_identity(position_identity):

    try:
        deals = mt5.history_deals_get(position=position_identity)
    except TypeError:
        return _POSITION_HISTORY_UNSUPPORTED
    except Exception as exc:
        _set_error(str(exc))
        return None

    if deals is None:
        return None

    return list(deals)


def _history_deals_for_close_confirmation(ticket, position_identifier, lookback_days):

    seen = set()
    exact_deals = []
    exact_lookup_supported = True

    for identity in _position_history_identities(ticket, position_identifier):
        deals = _history_deals_for_position_identity(identity)

        if deals is _POSITION_HISTORY_UNSUPPORTED:
            exact_lookup_supported = False
            break

        if deals is None:
            return None

        for deal in deals:
            deal_ticket = getattr(deal, "ticket", None)
            key = (
                deal_ticket
                if deal_ticket is not None
                else id(deal)
            )

            if key in seen:
                continue

            seen.add(key)
            exact_deals.append(deal)

        if deals:
            break

    if exact_deals:
        return exact_deals

    date_to = datetime.now() + timedelta(days=1)
    date_from = date_to - timedelta(days=lookback_days + 1)

    try:
        deals = mt5.history_deals_get(date_from, date_to)
    except Exception as exc:
        _set_error(str(exc))
        return None

    if deals is None:
        return None

    if exact_lookup_supported:
        logger.debug(
            f"Position history lookup returned no deals; used date fallback | "
            f"Ticket={ticket} PositionId={position_identifier}"
        )

    return list(deals)


def _opening_deal_matches_metadata(
    deal,
    opening_deal_ticket=None,
    opening_order_ticket=None,
    expected_magic=None,
    expected_comment=None,
):

    if opening_deal_ticket is not None:
        return getattr(deal, "ticket", None) == opening_deal_ticket

    if opening_order_ticket is not None:
        return getattr(deal, "order", None) == opening_order_ticket

    if expected_magic is not None and getattr(deal, "magic", None) != expected_magic:
        return False

    if expected_comment is not None and getattr(deal, "comment", None) != expected_comment:
        return False

    return True


def _select_opening_deal(
    deals,
    identities,
    symbol=None,
    opening_deal_ticket=None,
    opening_order_ticket=None,
    expected_magic=None,
    expected_comment=None,
):

    candidates = []

    for deal in deals:
        if not _deal_matches_any_position_identity(deal, identities):
            continue

        if not _deal_symbol_matches(deal, symbol):
            continue

        if not _deal_is_opening(deal):
            continue

        if not _opening_deal_matches_metadata(
            deal,
            opening_deal_ticket=opening_deal_ticket,
            opening_order_ticket=opening_order_ticket,
            expected_magic=expected_magic,
            expected_comment=expected_comment,
        ):
            continue

        candidates.append(deal)

    if not candidates:
        return None

    candidates.sort(
        key=lambda deal: (
            _deal_time(deal) or 0,
            getattr(deal, "ticket", 0) or 0,
        )
    )
    return candidates[0]


def _valid_closing_deals(
    deals,
    identities,
    symbol=None,
    side=None,
    opening_deal=None,
):

    opening_time = _deal_time(opening_deal)
    matching_exit_deals = []

    for deal in deals:
        if not _deal_matches_any_position_identity(deal, identities):
            continue

        if not _deal_symbol_matches(deal, symbol):
            continue

        if not _deal_is_exit(deal):
            continue

        if not _deal_has_positive_volume(deal):
            continue

        if not _deal_close_type_matches_side(deal, side):
            continue

        close_time = _deal_time(deal)

        if (
            opening_time is not None
            and close_time is not None
            and close_time <= opening_time
        ):
            continue

        matching_exit_deals.append(deal)

    matching_exit_deals.sort(
        key=lambda deal: (
            _deal_time(deal) or 0,
            getattr(deal, "ticket", 0) or 0,
        )
    )
    return matching_exit_deals


def _close_volume_covers_expected(close_volume, expected_volume, tolerance=0.00001):

    if expected_volume is None:
        return close_volume > 0

    expected_volume = _to_float(expected_volume)

    if expected_volume is None:
        return close_volume > 0

    return close_volume + tolerance >= expected_volume


def confirm_position_closed(
    ticket,
    tp=None,
    position_identifier=None,
    expected_volume=None,
    lookback_days=DEFAULT_HISTORY_LOOKBACK_DAYS,
    symbol=None,
    side=None,
    opening_deal_ticket=None,
    opening_order_ticket=None,
    expected_magic=None,
    expected_comment=None,
):

    with MT5_LOCK:
        _require_mt5()
        if mt5.terminal_info() is None:
            return {
                "confirmed": False,
                "pending": True,
                "reason": "MT5 unavailable",
                "close_reason": None,
                "deal": None,
                "metadata": {},
            }

        open_positions = mt5.positions_get(ticket=ticket)

        if open_positions is None:
            return {
                "confirmed": False,
                "pending": True,
                "reason": str(mt5.last_error()),
                "close_reason": None,
                "deal": None,
                "metadata": {},
            }

        if any(_position_has_remaining_volume(position) for position in open_positions):
            return {
                "confirmed": False,
                "pending": True,
                "reason": "Position still has remaining open volume",
                "close_reason": None,
                "deal": None,
                "metadata": {},
            }

        deals = _history_deals_for_close_confirmation(
            ticket,
            position_identifier,
            lookback_days,
        )

        if deals is None:
            return {
                "confirmed": False,
                "pending": True,
                "reason": str(mt5.last_error()),
                "close_reason": None,
                "deal": None,
                "metadata": {},
            }

        identities = _position_history_identities(ticket, position_identifier)
        opening_deal = _select_opening_deal(
            deals,
            identities,
            symbol=symbol,
            opening_deal_ticket=opening_deal_ticket,
            opening_order_ticket=opening_order_ticket,
            expected_magic=expected_magic,
            expected_comment=expected_comment,
        )
        matching_exit_deals = _valid_closing_deals(
            deals,
            identities,
            symbol=symbol,
            side=side,
            opening_deal=opening_deal,
        )

        if not matching_exit_deals:
            return {
                "confirmed": False,
                "pending": True,
                "reason": "No close deal found for position identity",
                "close_reason": None,
                "deal": None,
                "metadata": {},
            }

        total_close_volume = sum(_deal_volume(deal) for deal in matching_exit_deals)
        final_deal = matching_exit_deals[-1]
        metadata = _deal_close_metadata(
            final_deal,
            close_volume=round(total_close_volume, 10),
        )

        if tp is not None and _deal_price_matches_tp(final_deal, tp):
            logger.debug(
                f"Close deal price near TP | Ticket={ticket} "
                f"Deal={getattr(final_deal, 'ticket', None)} "
                f"Price={getattr(final_deal, 'price', None)} TP={tp}"
            )

        if not _close_volume_covers_expected(total_close_volume, expected_volume):
            logger.warning(
                f"Close deal volume below expected | "
                f"Ticket={ticket} ClosedVolume={total_close_volume} "
                f"Expected={expected_volume}"
            )
            return {
                "confirmed": False,
                "pending": False,
                "reason": "Close volume below expected volume",
                "close_reason": metadata.get("close_reason"),
                "deal": final_deal,
                "metadata": metadata,
            }

        return {
            "confirmed": True,
            "pending": False,
            "reason": metadata["close_reason"],
            "close_reason": metadata["close_reason"],
            "deal": final_deal,
            "metadata": metadata,
        }


def confirm_position_closed_by_tp(
    ticket,
    tp,
    position_identifier=None,
    expected_volume=None,
    lookback_days=DEFAULT_HISTORY_LOOKBACK_DAYS,
    symbol=None,
    side=None,
    opening_deal_ticket=None,
    opening_order_ticket=None,
    expected_magic=None,
    expected_comment=None,
):

    result = confirm_position_closed(
        ticket,
        tp=tp,
        position_identifier=position_identifier,
        expected_volume=expected_volume,
        lookback_days=lookback_days,
        symbol=symbol,
        side=side,
        opening_deal_ticket=opening_deal_ticket,
        opening_order_ticket=opening_order_ticket,
        expected_magic=expected_magic,
        expected_comment=expected_comment,
    )

    if result["pending"]:
        return result

    if result["confirmed"] and result.get("close_reason") == "take_profit":
        return {
            "confirmed": True,
            "pending": False,
            "reason": "take_profit",
            "close_reason": "take_profit",
            "deal": result["deal"],
            "metadata": result.get("metadata", {}),
        }

    return {
        "confirmed": False,
        "pending": False,
        "reason": result.get("close_reason") or result.get("reason"),
        "close_reason": result.get("close_reason"),
        "deal": result.get("deal"),
        "metadata": result.get("metadata", {}),
    }
