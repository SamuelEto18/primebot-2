from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import COMMENT, MAGIC_NUMBER
from core import mt5_service
from core.logger import logger
from core.trade_storage import (
    _STORAGE_LOCK,
    _atomic_write_json,
    _read_json_file,
    load_pending_identities,
    load_trade_history,
    load_trades,
)

REPORT_TIMEZONE_NAME = "Europe/Vienna"
REPORT_STATE_FILE = "data/statistics_report_state.json"
REPORT_TYPES = ("weekly", "monthly")
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_SAFE_LIMIT = 3900
MONEY_EPSILON = 0.005
BREAK_EVEN_PRICE_TOLERANCE = 0.05


class EuropeViennaFallback(tzinfo):
    def _last_sunday(self, year, month):
        value = datetime(year, month + 1, 1) - timedelta(days=1)

        while value.weekday() != 6:
            value -= timedelta(days=1)

        return value.day

    def _dst_start_local(self, year):
        return datetime(year, 3, self._last_sunday(year, 3), 2)

    def _dst_end_local(self, year):
        return datetime(year, 10, self._last_sunday(year, 10), 3)

    def _dst_start_utc(self, year):
        return datetime(
            year,
            3,
            self._last_sunday(year, 3),
            1,
            tzinfo=timezone.utc,
        )

    def _dst_end_utc(self, year):
        return datetime(
            year,
            10,
            self._last_sunday(year, 10),
            1,
            tzinfo=timezone.utc,
        )

    def dst(self, value):
        if value is None:
            return timedelta(0)

        naive = value.replace(tzinfo=None)

        if self._dst_start_local(value.year) <= naive < self._dst_end_local(value.year):
            return timedelta(hours=1)

        return timedelta(0)

    def utcoffset(self, value):
        return timedelta(hours=1) + self.dst(value)

    def tzname(self, value):
        return "CEST" if self.dst(value) else "CET"

    def fromutc(self, value):
        utc_value = value.replace(tzinfo=timezone.utc)

        if self._dst_start_utc(value.year) <= utc_value < self._dst_end_utc(value.year):
            return (value + timedelta(hours=2)).replace(tzinfo=self)

        return (value + timedelta(hours=1)).replace(tzinfo=self)


def _load_report_timezone():
    try:
        return ZoneInfo(REPORT_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        logger.warning(
            "System timezone database missing; using built-in Europe/Vienna DST rules"
        )
        return EuropeViennaFallback()


REPORT_TZ = _load_report_timezone()


class StatisticsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportPeriod:
    report_type: str
    period_start: datetime
    period_end: datetime
    incomplete: bool = False


@dataclass
class DealCashflow:
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0

    @property
    def net(self):
        return self.profit + self.commission + self.swap + self.fee


@dataclass
class PositionAnalysis:
    trade_key: tuple
    signal_label: str
    symbol: str
    side: str
    stored: dict
    deals: list = field(default_factory=list)
    opened_at: datetime = None
    closed_at: datetime = None
    is_closed: bool = False
    close_reason: str = "unknown_confirmed_close"
    total_cashflow: DealCashflow = field(default_factory=DealCashflow)
    period_cashflow: DealCashflow = field(default_factory=DealCashflow)

    @property
    def result(self):
        return self.total_cashflow.net


@dataclass
class SignalAnalysis:
    key: tuple
    label: str
    symbol: str
    trade: dict = None
    positions: list = field(default_factory=list)
    pending_records: list = field(default_factory=list)

    @property
    def result(self):
        return sum(position.result for position in self.positions)


@dataclass
class StatisticsReport:
    report_type: str
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    incomplete: bool
    account: dict
    metrics: dict
    symbol_rows: list
    open_groups: list
    warnings: list = field(default_factory=list)


def _now_local():
    return datetime.now(REPORT_TZ)


def _as_local(value):
    if value is None:
        return _now_local()

    if value.tzinfo is None:
        return value.replace(tzinfo=REPORT_TZ)

    return value.astimezone(REPORT_TZ)


def _midnight(date_value):
    return datetime(
        date_value.year,
        date_value.month,
        date_value.day,
        tzinfo=REPORT_TZ,
    )


def _add_month(value, months=1):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return datetime(year, month, 1, tzinfo=REPORT_TZ)


def most_recent_weekly_boundary(now=None):
    local_now = _as_local(now)
    days_since_saturday = (local_now.weekday() - 5) % 7
    candidate = _midnight(local_now.date() - timedelta(days=days_since_saturday))
    return candidate


def next_weekly_boundary(now=None):
    local_now = _as_local(now)
    boundary = most_recent_weekly_boundary(local_now)

    if boundary <= local_now:
        return boundary + timedelta(days=7)

    return boundary


def weekly_period_for_boundary(period_end):
    end = _as_local(period_end)
    return ReportPeriod(
        report_type="weekly",
        period_start=end - timedelta(days=5),
        period_end=end,
    )


def most_recent_monthly_boundary(now=None):
    local_now = _as_local(now)
    return datetime(local_now.year, local_now.month, 1, tzinfo=REPORT_TZ)


def next_monthly_boundary(now=None):
    return _add_month(most_recent_monthly_boundary(now), 1)


def monthly_period_for_boundary(period_end):
    end = _as_local(period_end)
    return ReportPeriod(
        report_type="monthly",
        period_start=_add_month(end, -1),
        period_end=end,
    )


def completed_period(report_type, now=None):
    if report_type == "weekly":
        return weekly_period_for_boundary(most_recent_weekly_boundary(now))

    if report_type == "monthly":
        return monthly_period_for_boundary(most_recent_monthly_boundary(now))

    raise ValueError(f"Unknown report type: {report_type}")


def current_preview_period(report_type, now=None):
    local_now = _as_local(now)

    if report_type == "weekly":
        start = _midnight(local_now.date() - timedelta(days=local_now.weekday()))
    elif report_type == "monthly":
        start = datetime(local_now.year, local_now.month, 1, tzinfo=REPORT_TZ)
    else:
        raise ValueError(f"Unknown report type: {report_type}")

    return ReportPeriod(
        report_type=report_type,
        period_start=start,
        period_end=local_now,
        incomplete=True,
    )


def _default_report_state():
    return {
        "weekly": {"last_successful_period_end": None},
        "monthly": {"last_successful_period_end": None},
    }


def load_report_state():
    with _STORAGE_LOCK:
        state = _read_json_file(REPORT_STATE_FILE, expected_type=dict)

    default = _default_report_state()

    if not isinstance(state, dict):
        state = {}

    for report_type in REPORT_TYPES:
        section = state.get(report_type)

        if not isinstance(section, dict):
            state[report_type] = dict(default[report_type])
            continue

        for key, value in default[report_type].items():
            section.setdefault(key, value)

    return state


def save_report_state(state):
    with _STORAGE_LOCK:
        _atomic_write_json(state, REPORT_STATE_FILE, expected_type=dict)


def _parse_iso_time(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None

    return _as_local(parsed)


def _iso(value):
    return _as_local(value).isoformat(timespec="seconds")


def last_successful_period_end(report_type, state=None):
    state = state or load_report_state()
    return _parse_iso_time(
        state.get(report_type, {}).get("last_successful_period_end")
    )


def report_already_delivered(report_type, period_end, state=None):
    delivered = last_successful_period_end(report_type, state=state)
    return delivered is not None and delivered >= _as_local(period_end)


def mark_report_delivered(report_type, period_end):
    state = load_report_state()
    state.setdefault(report_type, {})
    state[report_type]["last_successful_period_end"] = _iso(period_end)
    save_report_state(state)
    return state


def due_report_periods(now=None, state=None):
    state = state or load_report_state()
    periods = []

    for report_type in REPORT_TYPES:
        period = completed_period(report_type, now=now)

        if report_already_delivered(report_type, period.period_end, state=state):
            logger.debug(
                "Statistics duplicate report suppressed | "
                f"Type={report_type} PeriodEnd={_iso(period.period_end)}"
            )
            continue

        periods.append(period)

    return periods


def get_report_delivery_summary(state=None):
    state = state or load_report_state()
    return {
        report_type: state.get(report_type, {}).get("last_successful_period_end")
        for report_type in REPORT_TYPES
    }


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
    return position.get("position_identifier", position.get("position_id"))


def _position_identities(position):
    identities = []

    for value in (_position_identifier(position), _position_ticket(position)):
        normalized = _identity_value(value)

        if normalized is None:
            continue

        if normalized not in identities:
            identities.append(normalized)

    return identities


def _signal_key(trade):
    return (
        trade.get("chat_id"),
        trade.get("message_id"),
    )


def _signal_label(trade_or_record):
    chat_id = trade_or_record.get("chat_id", "?")
    message_id = trade_or_record.get("message_id", "?")
    symbol = trade_or_record.get("symbol") or "Unknown"
    return f"{symbol} {chat_id}/{message_id}"


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _deal_time(deal):
    value = getattr(deal, "time_msc", None)

    if value is None:
        value = getattr(deal, "time", None)

    if value is None:
        return None

    numeric = _to_float(value, default=None)

    if numeric is None:
        return None

    if numeric > 100000000000:
        numeric = numeric / 1000

    return datetime.fromtimestamp(numeric, tz=timezone.utc).astimezone(REPORT_TZ)


def _stored_time(*values):
    for value in values:
        parsed = _parse_iso_time(value)

        if parsed is not None:
            return parsed

    return None


def _deal_entry_in():
    try:
        return mt5_service.deal_entry_in()
    except Exception:
        return 0


def _deal_entry_out_values():
    values = set()

    try:
        values.add(mt5_service.deal_entry_out())
    except Exception:
        values.add(1)

    try:
        out_by = mt5_service.deal_entry_out_by()

        if out_by is not None:
            values.add(out_by)
    except Exception:
        values.add(3)

    return values


def _is_opening_deal(deal):
    return getattr(deal, "entry", None) == _deal_entry_in()


def _is_exit_deal(deal):
    return getattr(deal, "entry", None) in _deal_entry_out_values()


def _deal_reason_tp():
    try:
        return mt5_service.deal_reason_tp()
    except Exception:
        return None


def _deal_reason_sl():
    try:
        return mt5_service.deal_reason_sl()
    except Exception:
        return None


def _manual_close_reasons():
    reasons = {0}
    api = getattr(mt5_service, "mt5", None)

    for name in ("DEAL_REASON_CLIENT", "DEAL_REASON_MOBILE", "DEAL_REASON_WEB"):
        reason = getattr(api, name, None)

        if reason is not None:
            reasons.add(reason)

    return reasons


def _deal_field(deal, name):
    return _to_float(getattr(deal, name, 0.0), default=0.0)


def _deal_cashflow(deal):
    return DealCashflow(
        profit=_deal_field(deal, "profit"),
        commission=_deal_field(deal, "commission"),
        swap=_deal_field(deal, "swap"),
        fee=_deal_field(deal, "fee"),
    )


def _add_cashflow(left, right):
    left.profit += right.profit
    left.commission += right.commission
    left.swap += right.swap
    left.fee += right.fee


def _in_period(value, period):
    if value is None:
        return False

    local = _as_local(value)
    return period.period_start <= local < period.period_end


def _price_near_entry(deal, position):
    price = _to_float(getattr(deal, "price", None), default=None)
    entry = _to_float(
        position.stored.get("entry")
        or position.stored.get("price_open")
        or position.stored.get("fill_price"),
        default=None,
    )

    if price is None or entry is None:
        return False

    return abs(price - entry) <= BREAK_EVEN_PRICE_TOLERANCE


def _classify_close(position, final_deal):
    stored_reason = position.stored.get("close_reason")

    if final_deal is None:
        if stored_reason:
            return stored_reason

        if position.stored.get("break_even"):
            return "break_even"

        return "unknown_confirmed_close"

    reason = getattr(final_deal, "reason", None)
    tp_reason = _deal_reason_tp()
    sl_reason = _deal_reason_sl()

    if tp_reason is not None and reason == tp_reason:
        return "take_profit"

    if sl_reason is not None and reason == sl_reason:
        if position.stored.get("break_even") and _price_near_entry(final_deal, position):
            return "break_even"

        return "stop_loss"

    if reason in _manual_close_reasons():
        return "manual_close"

    if stored_reason:
        return stored_reason

    return "other_close" if reason is not None else "unknown_confirmed_close"


def _expected_volume(position):
    return _to_float(
        position.stored.get("original_volume")
        or position.stored.get("initial_volume")
        or position.stored.get("volume"),
        default=None,
    )


def _close_volume_covers_position(position, close_deals):
    if not close_deals:
        return False

    expected = _expected_volume(position)
    closed_volume = sum(_to_float(getattr(deal, "volume", 0.0)) for deal in close_deals)

    if expected is None:
        return closed_volume > 0

    return closed_volume + 0.00001 >= expected


def _deal_sort_key(deal):
    return (
        _deal_time(deal) or datetime.min.replace(tzinfo=REPORT_TZ),
        getattr(deal, "ticket", 0) or 0,
    )


def _dedupe_deals(deals):
    seen = set()
    result = []

    for deal in deals:
        ticket = getattr(deal, "ticket", None)
        key = ("ticket", ticket) if ticket is not None else ("object", id(deal))

        if key in seen:
            continue

        seen.add(key)
        result.append(deal)

    result.sort(key=_deal_sort_key)
    return result


def _fetch_deals_for_position(position):
    deals = []

    for identity in _position_identities(position.stored):
        result = mt5_service.history_deals_get(position=identity)

        if result is None:
            raise StatisticsUnavailable(
                f"MT5 history unavailable for position {identity}"
            )

        deals.extend(list(result))

    return _dedupe_deals(deals)


def _opening_deal_for_position(position):
    opening_deal_ticket = position.stored.get("deal_ticket")
    opening_order_ticket = position.stored.get("order_ticket")

    for deal in position.deals:
        if not _is_opening_deal(deal):
            continue

        if opening_deal_ticket is not None and getattr(deal, "ticket", None) == opening_deal_ticket:
            return deal

        if opening_order_ticket is not None and getattr(deal, "order", None) == opening_order_ticket:
            return deal

    for deal in position.deals:
        if not _is_opening_deal(deal):
            continue

        if getattr(deal, "magic", None) == MAGIC_NUMBER and getattr(deal, "comment", None) == COMMENT:
            return deal

    for deal in position.deals:
        if _is_opening_deal(deal):
            return deal

    return None


def _analyze_position(position, period):
    position.deals = _fetch_deals_for_position(position)
    opening_deal = _opening_deal_for_position(position)
    exit_deals = [deal for deal in position.deals if _is_exit_deal(deal)]

    position.opened_at = (
        _deal_time(opening_deal)
        or _stored_time(
            position.stored.get("order_attempt_started_at"),
            position.stored.get("created_at"),
        )
    )

    final_close_deal = exit_deals[-1] if exit_deals else None
    position.closed_at = (
        _deal_time(final_close_deal)
        or _stored_time(position.stored.get("closed_at"))
    )
    position.is_closed = bool(position.stored.get("closed")) or _close_volume_covers_position(
        position,
        exit_deals,
    )

    if position.is_closed:
        position.close_reason = _classify_close(position, final_close_deal)

    for deal in position.deals:
        cashflow = _deal_cashflow(deal)
        _add_cashflow(position.total_cashflow, cashflow)

        if _in_period(_deal_time(deal), period):
            _add_cashflow(position.period_cashflow, cashflow)

    return position


def _load_signal_analyses():
    signals = {}

    def ensure_signal(key, label, symbol, trade=None):
        if key not in signals:
            signals[key] = SignalAnalysis(
                key=key,
                label=label,
                symbol=symbol or "Unknown",
                trade=trade,
            )
        elif trade is not None and signals[key].trade is None:
            signals[key].trade = trade

        return signals[key]

    for trade in load_trade_history() + load_trades():
        key = _signal_key(trade)
        signal = ensure_signal(
            key,
            _signal_label(trade),
            trade.get("symbol"),
            trade=trade,
        )

        for stored_position in trade.get("positions", []):
            signal.positions.append(
                PositionAnalysis(
                    trade_key=key,
                    signal_label=signal.label,
                    symbol=stored_position.get("symbol") or trade.get("symbol") or "Unknown",
                    side=stored_position.get("side") or trade.get("side"),
                    stored=stored_position,
                )
            )

    for record in load_pending_identities():
        key = _signal_key(record)
        signal = ensure_signal(
            key,
            _signal_label(record),
            record.get("symbol"),
            trade=None,
        )
        signal.pending_records.append(record)

    return signals


def _account_snapshot():
    account = mt5_service.account_info()
    positions = mt5_service.positions_get()

    if account is None:
        raise StatisticsUnavailable("MT5 account information unavailable")

    if positions is None:
        raise StatisticsUnavailable("MT5 open positions unavailable")

    return {
        "login": getattr(account, "login", None),
        "server": getattr(account, "server", None),
        "company": getattr(account, "company", None),
        "balance": getattr(account, "balance", None),
        "equity": getattr(account, "equity", None),
        "currency": getattr(account, "currency", ""),
        "positions": list(positions),
    }


def _live_identity(position):
    values = []

    for name in ("identifier", "position_identifier", "position_id", "ticket"):
        value = _identity_value(getattr(position, name, None))

        if value is not None and value not in values:
            values.append(value)

    return values


def _open_exposure(signals, account):
    known_open = {}

    for signal in signals.values():
        for position in signal.positions:
            if position.is_closed:
                continue

            for identity in _position_identities(position.stored):
                known_open[identity] = position

    groups = {}
    total_floating = 0.0
    open_count = 0

    for live_position in account["positions"]:
        matched = None

        for identity in _live_identity(live_position):
            if identity in known_open:
                matched = known_open[identity]
                break

        if matched is None:
            continue

        floating = _to_float(getattr(live_position, "profit", 0.0))
        total_floating += floating
        open_count += 1
        key = (matched.trade_key, getattr(live_position, "symbol", matched.symbol))
        group = groups.setdefault(
            key,
            {
                "signal": matched.signal_label,
                "symbol": getattr(live_position, "symbol", matched.symbol),
                "positions": 0,
                "floating": 0.0,
            },
        )
        group["positions"] += 1
        group["floating"] += floating

    return open_count, total_floating, list(groups.values())


def _pending_origin_in_period(signal, period):
    for record in signal.pending_records:
        started = _stored_time(
            record.get("order_attempt_started_at"),
            record.get("created_at"),
        )

        if _in_period(started, period):
            return True

    return False


def _signal_origin_time(signal):
    times = [position.opened_at for position in signal.positions if position.opened_at]

    for record in signal.pending_records:
        value = _stored_time(record.get("order_attempt_started_at"), record.get("created_at"))

        if value is not None:
            times.append(value)

    if not times:
        return None

    return min(times)


def _signal_completion_time(signal):
    if not signal.positions:
        return None

    if any(not position.is_closed for position in signal.positions):
        return None

    times = [position.closed_at for position in signal.positions if position.closed_at]

    if not times:
        return None

    return max(times)


def _is_profitable(value):
    return value > MONEY_EPSILON


def _is_losing(value):
    return value < -MONEY_EPSILON


def _is_break_even(value):
    return not _is_profitable(value) and not _is_losing(value)


def _win_rate(wins, total):
    if total <= 0:
        return None

    return wins * 100.0 / total


def _profit_factor(gross_profit, gross_loss):
    if abs(gross_loss) <= MONEY_EPSILON:
        return None

    return gross_profit / abs(gross_loss)


def _max_consecutive(completed_signals, predicate):
    current = 0
    best = 0

    for signal, _closed_at in completed_signals:
        if predicate(signal.result):
            current += 1
            best = max(best, current)
        else:
            current = 0

    return best


def _closed_result_drawdown(completed_signals):
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for signal, _closed_at in completed_signals:
        equity += signal.result
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)

    return max_drawdown


def _deal_ticket_key(deal):
    ticket = getattr(deal, "ticket", None)
    return ("ticket", ticket) if ticket is not None else ("object", id(deal))


def _financial_metrics(positions):
    seen = set()
    gross_profit = 0.0
    gross_loss = 0.0
    cashflow = DealCashflow()

    for position in positions:
        for deal in position.deals:
            key = _deal_ticket_key(deal)

            if key in seen:
                continue

            seen.add(key)
            deal_flow = _deal_cashflow(deal)

            if deal_flow.profit > 0:
                gross_profit += deal_flow.profit
            elif deal_flow.profit < 0:
                gross_loss += deal_flow.profit

            _add_cashflow(cashflow, deal_flow)

    return gross_profit, gross_loss, cashflow


def _period_financial_metrics(positions):
    seen = set()
    gross_profit = 0.0
    gross_loss = 0.0
    cashflow = DealCashflow()

    for position in positions:
        for deal in position.deals:
            if not _in_period(_deal_time(deal), position.report_period):
                continue

            key = _deal_ticket_key(deal)

            if key in seen:
                continue

            seen.add(key)
            deal_flow = _deal_cashflow(deal)

            if deal_flow.profit > 0:
                gross_profit += deal_flow.profit
            elif deal_flow.profit < 0:
                gross_loss += deal_flow.profit

            _add_cashflow(cashflow, deal_flow)

    return gross_profit, gross_loss, cashflow


def _symbol_summary(all_signals, completed_signals, closed_positions, period):
    rows = {}

    for position in closed_positions:
        row = rows.setdefault(
            position.symbol,
            {
                "symbol": position.symbol,
                "completed_signals": set(),
                "closed_positions": 0,
                "net_realized": 0.0,
            },
        )
        row["closed_positions"] += 1

    for signal in completed_signals:
        row = rows.setdefault(
            signal.symbol,
            {
                "symbol": signal.symbol,
                "completed_signals": set(),
                "closed_positions": 0,
                "net_realized": 0.0,
            },
        )
        row["completed_signals"].add(signal.key)

    seen_deals = set()

    for signal in all_signals:
        for position in signal.positions:
            for deal in position.deals:
                if not _in_period(_deal_time(deal), period):
                    continue

                key = _deal_ticket_key(deal)

                if key in seen_deals:
                    continue

                seen_deals.add(key)
                row = rows.setdefault(
                    position.symbol,
                    {
                        "symbol": position.symbol,
                        "completed_signals": set(),
                        "closed_positions": 0,
                        "net_realized": 0.0,
                    },
                )
                row["net_realized"] += _deal_cashflow(deal).net

    result = []

    for row in rows.values():
        item = dict(row)
        item["completed_signals"] = len(item["completed_signals"])
        result.append(item)

    result.sort(key=lambda item: item["symbol"])
    return result


def _calculate_metrics(signals, period, account):
    all_positions = [
        position
        for signal in signals.values()
        for position in signal.positions
    ]

    for position in all_positions:
        position.report_period = period

    positions_opened = [
        position for position in all_positions
        if _in_period(position.opened_at, period)
    ]
    positions_closed = [
        position for position in all_positions
        if position.is_closed and _in_period(position.closed_at, period)
    ]
    positions_still_open = [
        position for position in positions_opened
        if not position.is_closed
    ]

    executed_signals = [
        signal for signal in signals.values()
        if (
            any(_in_period(position.opened_at, period) for position in signal.positions)
            or _pending_origin_in_period(signal, period)
        )
    ]
    open_signals_from_period = [
        signal for signal in executed_signals
        if (
            any(not position.is_closed for position in signal.positions)
            or signal.pending_records
        )
    ]

    completed_signals = []

    for signal in signals.values():
        completed_at = _signal_completion_time(signal)

        if _in_period(completed_at, period):
            completed_signals.append((signal, completed_at))

    completed_signals.sort(key=lambda item: item[1])
    completed_signal_only = [item[0] for item in completed_signals]

    profitable_completed_signals = [
        signal for signal in completed_signal_only if _is_profitable(signal.result)
    ]
    losing_completed_signals = [
        signal for signal in completed_signal_only if _is_losing(signal.result)
    ]
    break_even_completed_signals = [
        signal for signal in completed_signal_only if _is_break_even(signal.result)
    ]

    profitable_positions = [
        position for position in positions_closed if _is_profitable(position.result)
    ]
    losing_positions = [
        position for position in positions_closed if _is_losing(position.result)
    ]
    break_even_positions = [
        position for position in positions_closed if _is_break_even(position.result)
    ]

    period_positions_for_finance = []
    seen_positions = set()

    for position in all_positions:
        for deal in position.deals:
            if _in_period(_deal_time(deal), period):
                key = tuple(_position_identities(position.stored)) or (id(position),)

                if key not in seen_positions:
                    seen_positions.add(key)
                    period_positions_for_finance.append(position)

                break

    gross_profit, gross_loss, period_cashflow = _period_financial_metrics(
        period_positions_for_finance
    )

    open_count, floating, open_groups = _open_exposure(signals, account)

    best_signal = None
    worst_signal = None

    if completed_signal_only:
        best_signal = max(completed_signal_only, key=lambda signal: signal.result)
        worst_signal = min(completed_signal_only, key=lambda signal: signal.result)

    metrics = {
        "executed_signals": len(executed_signals),
        "completed_signals": len(completed_signal_only),
        "open_signals_from_period": len(open_signals_from_period),
        "profitable_completed_signals": len(profitable_completed_signals),
        "losing_completed_signals": len(losing_completed_signals),
        "break_even_completed_signals": len(break_even_completed_signals),
        "signal_win_rate": _win_rate(
            len(profitable_completed_signals),
            len(completed_signal_only),
        ),
        "best_signal": best_signal,
        "worst_signal": worst_signal,
        "positions_opened": len(positions_opened),
        "positions_closed": len(positions_closed),
        "positions_still_open": len(positions_still_open),
        "tp_closes": sum(1 for position in positions_closed if position.close_reason == "take_profit"),
        "sl_closes": sum(1 for position in positions_closed if position.close_reason == "stop_loss"),
        "break_even_closes": sum(1 for position in positions_closed if position.close_reason == "break_even"),
        "manual_closes": sum(1 for position in positions_closed if position.close_reason == "manual_close"),
        "other_closes": sum(
            1
            for position in positions_closed
            if position.close_reason in ("other_close", "unknown_confirmed_close")
        ),
        "profitable_positions": len(profitable_positions),
        "losing_positions": len(losing_positions),
        "break_even_positions": len(break_even_positions),
        "position_win_rate": _win_rate(
            len(profitable_positions),
            len(positions_closed),
        ),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "realized_profit": period_cashflow.profit,
        "commissions": period_cashflow.commission,
        "swaps": period_cashflow.swap,
        "fees": period_cashflow.fee,
        "net_realized": period_cashflow.net,
        "average_profitable_position": (
            sum(position.result for position in profitable_positions)
            / len(profitable_positions)
            if profitable_positions
            else None
        ),
        "average_losing_position": (
            sum(position.result for position in losing_positions)
            / len(losing_positions)
            if losing_positions
            else None
        ),
        "profit_factor": _profit_factor(gross_profit, gross_loss),
        "max_consecutive_profitable_completed_signals": _max_consecutive(
            completed_signals,
            _is_profitable,
        ),
        "max_consecutive_losing_completed_signals": _max_consecutive(
            completed_signals,
            _is_losing,
        ),
        "closed_result_drawdown": _closed_result_drawdown(completed_signals),
        "open_primebot_positions": open_count,
        "floating_pl": floating,
    }

    return (
        metrics,
        _symbol_summary(
            list(signals.values()),
            completed_signal_only,
            positions_closed,
            period,
        ),
        open_groups,
    )


def build_statistics_report(report_type, current=False, now=None):
    period = (
        current_preview_period(report_type, now=now)
        if current
        else completed_period(report_type, now=now)
    )
    generated_at = _as_local(now)

    logger.info(
        "Statistics report generation started | "
        f"Type={report_type} PeriodStart={_iso(period.period_start)} "
        f"PeriodEnd={_iso(period.period_end)}"
    )

    account = _account_snapshot()
    signals = _load_signal_analyses()
    warnings = []

    for signal in signals.values():
        for position in signal.positions:
            try:
                _analyze_position(position, period)
            except StatisticsUnavailable:
                raise
            except Exception as exc:
                logger.exception(
                    "Position statistics analysis failed | "
                    f"Signal={signal.label} Ticket={_position_ticket(position.stored)}"
                )
                warnings.append(str(exc))

    metrics, symbol_rows, open_groups = _calculate_metrics(signals, period, account)

    return StatisticsReport(
        report_type=report_type,
        period_start=period.period_start,
        period_end=period.period_end,
        generated_at=generated_at,
        incomplete=period.incomplete,
        account=account,
        metrics=metrics,
        symbol_rows=symbol_rows,
        open_groups=open_groups,
        warnings=warnings,
    )


def _format_dt(value):
    return _as_local(value).strftime("%d %b %Y %H:%M")


def _format_money(value, currency=""):
    if value is None:
        return "Unavailable"

    prefix = f"{currency} " if currency else ""
    return f"{prefix}{value:.2f}"


def _format_percent(value):
    if value is None:
        return "N/A"

    return f"{value:.2f}%"


def _format_factor(value):
    if value is None:
        return "N/A"

    return f"{value:.2f}"


def _format_signal(signal, currency):
    if signal is None:
        return "None"

    return f"{signal.label} | {_format_money(signal.result, currency)}"


def format_statistics_report(report):
    metrics = report.metrics
    currency = report.account.get("currency") or ""
    title = f"PRIMEBOT {report.report_type.upper()} REPORT"

    lines = [
        title,
    ]

    if report.incomplete:
        lines.append("INCOMPLETE PERIOD PREVIEW")

    lines.extend([
        f"{_format_dt(report.period_start)} -> {_format_dt(report.period_end)}",
        f"Timezone: {REPORT_TIMEZONE_NAME}",
        f"Generated: {_format_dt(report.generated_at)}",
        (
            "Account: "
            f"{report.account.get('login') or 'Unavailable'} | "
            f"{report.account.get('server') or 'Unavailable'}"
        ),
        (
            "Balance / Equity: "
            f"{_format_money(report.account.get('balance'), currency)} / "
            f"{_format_money(report.account.get('equity'), currency)}"
        ),
        "",
        "RESULT",
        f"Net realized: {_format_money(metrics['net_realized'], currency)}",
        f"Gross profit: {_format_money(metrics['gross_profit'], currency)}",
        f"Gross loss: {_format_money(metrics['gross_loss'], currency)}",
        f"Floating P/L: {_format_money(metrics['floating_pl'], currency)}",
        "",
        "SIGNALS",
        f"Executed: {metrics['executed_signals']}",
        f"Completed: {metrics['completed_signals']}",
        (
            "Wins / Losses / BE: "
            f"{metrics['profitable_completed_signals']} / "
            f"{metrics['losing_completed_signals']} / "
            f"{metrics['break_even_completed_signals']}"
        ),
        f"Currently open from period: {metrics['open_signals_from_period']}",
        f"Win rate: {_format_percent(metrics['signal_win_rate'])}",
        "",
        "POSITIONS",
        (
            "Opened / Closed / Open: "
            f"{metrics['positions_opened']} / "
            f"{metrics['positions_closed']} / "
            f"{metrics['positions_still_open']}"
        ),
        (
            "TP / SL / BE / Manual / Other: "
            f"{metrics['tp_closes']} / "
            f"{metrics['sl_closes']} / "
            f"{metrics['break_even_closes']} / "
            f"{metrics['manual_closes']} / "
            f"{metrics['other_closes']}"
        ),
        (
            "Profitable / Losing / BE: "
            f"{metrics['profitable_positions']} / "
            f"{metrics['losing_positions']} / "
            f"{metrics['break_even_positions']}"
        ),
        f"Position win rate: {_format_percent(metrics['position_win_rate'])}",
        "",
        "FINANCIALS",
        f"Realized profit: {_format_money(metrics['realized_profit'], currency)}",
        f"Commissions: {_format_money(metrics['commissions'], currency)}",
        f"Swaps: {_format_money(metrics['swaps'], currency)}",
        f"Fees: {_format_money(metrics['fees'], currency)}",
        (
            "Average profitable / losing position: "
            f"{_format_money(metrics['average_profitable_position'], currency)} / "
            f"{_format_money(metrics['average_losing_position'], currency)}"
        ),
        "",
        "RISK",
        f"Profit factor: {_format_factor(metrics['profit_factor'])}",
        (
            "Max consecutive profitable / losing signals: "
            f"{metrics['max_consecutive_profitable_completed_signals']} / "
            f"{metrics['max_consecutive_losing_completed_signals']}"
        ),
        (
            "Closed-result drawdown: "
            f"{_format_money(metrics['closed_result_drawdown'], currency)}"
        ),
        f"Best signal: {_format_signal(metrics['best_signal'], currency)}",
        f"Worst signal: {_format_signal(metrics['worst_signal'], currency)}",
        "",
        "OPEN EXPOSURE",
        f"Open PrimeBot positions: {metrics['open_primebot_positions']}",
        f"Current floating P/L: {_format_money(metrics['floating_pl'], currency)}",
    ])

    if report.open_groups:
        for group in report.open_groups:
            lines.append(
                f"{group['symbol']} {group['signal']}: "
                f"{group['positions']} pos | "
                f"{_format_money(group['floating'], currency)}"
            )
    else:
        lines.append("None")

    lines.extend(["", "BY SYMBOL"])

    if report.symbol_rows:
        for row in report.symbol_rows:
            lines.append(
                f"{row['symbol']}: "
                f"{row['completed_signals']} signals | "
                f"{row['closed_positions']} closed positions | "
                f"{_format_money(row['net_realized'], currency)}"
            )
    else:
        lines.append("No activity")

    lines.extend([
        "",
        "BASIS",
        "Cashflow metrics: MT5 deals occurring inside the period.",
        "Position metrics: PrimeBot positions opened or fully closed inside the period.",
        "Signal metrics: signals executed from period openings and completed by period closures.",
        "Floating P/L is a current open exposure snapshot and is not included in net realized.",
    ])

    if report.warnings:
        lines.extend(["", "WARNINGS"])
        lines.extend(report.warnings[:5])

    return "\n".join(lines)


def split_telegram_message(text, limit=TELEGRAM_SAFE_LIMIT):
    if len(text) <= limit:
        return [text]

    messages = []
    current = []
    current_length = 0

    for line in text.splitlines():
        line_length = len(line) + 1

        if line_length > limit:
            if current:
                messages.append("\n".join(current))
                current = []
                current_length = 0

            for index in range(0, len(line), limit):
                messages.append(line[index:index + limit])

            continue

        if current and current_length + line_length > limit:
            messages.append("\n".join(current))
            current = []
            current_length = 0

        current.append(line)
        current_length += line_length

    if current:
        messages.append("\n".join(current))

    return messages


def generate_report_messages(report_type, current=False, now=None):
    report = build_statistics_report(report_type, current=current, now=now)
    return split_telegram_message(format_statistics_report(report))
