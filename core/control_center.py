import time
from datetime import datetime

from core.mt5_status import (
    format_account_label,
    get_account_summary,
    get_open_positions,
    is_connected,
    position_side,
)
from core.runtime import get_runtime
from core.statistics import get_report_delivery_summary
from core.trade_storage import load_pending_identities, load_trades

HEALTHY = "\U0001F7E2 Healthy"
ERROR = "\U0001F534 Error"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONTROL_BOT_HEARTBEAT_MAX_AGE_SECONDS = 30
STATISTICS_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS = 120


def _now():

    return datetime.now()


def _parse_time(value):

    if not value:
        return None

    try:
        return datetime.strptime(value, TIME_FORMAT)
    except Exception:
        return None


def _format_time(value):

    return value or "Never"


def _format_money(value, currency=""):

    if value is None:
        return "Unavailable"

    suffix = f" {currency}" if currency else ""

    return f"{value:.2f}{suffix}"


def _format_percent(value):

    if value is None:
        return "Unavailable"

    return f"{value:.2f}%"


def _format_uptime(started):

    started_at = _parse_time(started)

    if started_at is None:
        return "Unknown"

    seconds = int((_now() - started_at).total_seconds())

    if seconds < 0:
        seconds = 0

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if days:
        parts.append(f"{days}d")

    if hours or days:
        parts.append(f"{hours}h")

    if minutes or hours or days:
        parts.append(f"{minutes}m")

    parts.append(f"{seconds}s")

    return " ".join(parts)


def _open_positions_from_storage(trades):

    positions = []

    for trade in trades:

        for position in trade.get("positions", []):

            if not position.get("closed", False):
                positions.append(position)

    return positions


def _active_signal_count(trades, pending_records=None):

    active = set()
    pending_records = pending_records or []

    for trade in trades:

        open_positions = [
            position for position in trade.get("positions", [])
            if not position.get("closed", False)
        ]

        if open_positions:
            active.add((trade.get("chat_id"), trade.get("message_id")))

    for record in pending_records:
        active.add((record.get("chat_id"), record.get("message_id")))

    return len(active)


def _health_status(ok):

    return HEALTHY if ok else ERROR


def _recent(value, max_age_seconds):

    seen_at = _parse_time(value)

    if seen_at is None:
        return False

    return (_now() - seen_at).total_seconds() <= max_age_seconds


def get_health():

    runtime_ok = True
    storage_ok = True

    try:
        runtime = get_runtime()
    except Exception:
        runtime = {}
        runtime_ok = False

    try:
        load_trades()
    except Exception:
        storage_ok = False

    return {
        "Telegram": bool(runtime.get("telegram_control_bot")) and _recent(
            runtime.get("telegram_control_bot_last_seen"),
            CONTROL_BOT_HEARTBEAT_MAX_AGE_SECONDS,
        ),
        "MT5": is_connected(),
        "Storage": storage_ok,
        "Runtime": runtime_ok,
        "Position Manager": bool(runtime.get("position_manager")) and _recent(
            runtime.get("position_manager_last_seen"),
            15
        ),
        "Statistics Scheduler": bool(runtime.get("statistics_scheduler")) and _recent(
            runtime.get("statistics_scheduler_last_seen"),
            STATISTICS_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS,
        ),
        "Listener": bool(runtime.get("telegram_listener")),
    }


def format_status():

    runtime = get_runtime()
    trades = load_trades()
    pending_records = load_pending_identities()
    account = get_account_summary()
    health = get_health()
    report_delivery = get_report_delivery_summary()

    open_positions = _open_positions_from_storage(trades)
    mode = "Live" if runtime.get("auto_execute") else "Dry Run"
    runtime_mode = "Paused" if runtime.get("paused") else "Running"

    return (
        "PrimeBot Control Center\n\n"
        f"Bot Version: {runtime.get('version', 'Unknown')}\n"
        f"Runtime Mode: {runtime_mode}\n"
        f"Dry Run / Live: {mode}\n"
        f"Paused: {runtime.get('paused')}\n"
        f"MT5 Connected: {account['connected']}\n"
        f"Telegram Listener: {_health_status(health['Listener'])}\n"
        f"Telegram Control Bot: {_health_status(health['Telegram'])}\n"
        f"Statistics Scheduler: {_health_status(health.get('Statistics Scheduler', False))}\n"
        f"Open Trades: {len(open_positions)}\n"
        f"Active Signals: {_active_signal_count(trades, pending_records)}\n"
        f"Runtime Uptime: {_format_uptime(runtime.get('started'))}\n"
        f"Last Signal Time: {_format_time(runtime.get('last_signal_time'))}\n"
        f"Last Trade Time: {_format_time(runtime.get('last_trade_time'))}\n"
        f"Last Weekly Stats: {_format_time(report_delivery.get('weekly'))}\n"
        f"Last Monthly Stats: {_format_time(report_delivery.get('monthly'))}\n"
        f"Current Account: {format_account_label(account)}\n"
        f"Current Broker: {account['broker']}"
    )


def format_health():

    health = get_health()

    lines = ["PrimeBot Health", ""]

    for name in [
        "Telegram",
        "MT5",
        "Storage",
        "Runtime",
        "Position Manager",
        "Statistics Scheduler",
        "Listener",
    ]:
        lines.append(f"{name}: {_health_status(health.get(name, False))}")

    return "\n".join(lines)


def format_ping(started_at):

    elapsed_ms = (time.perf_counter() - started_at) * 1000

    return (
        "Pong\n\n"
        f"Latency: {elapsed_ms:.2f} ms\n"
        f"Response Time: {elapsed_ms:.2f} ms"
    )


def format_balance():

    account = get_account_summary()
    currency = account["currency"]

    if not account["logged_in"]:
        return "Balance\n\nMT5 account unavailable."

    return (
        "Balance\n\n"
        f"Connection: {account['connected']}\n"
        f"Login: {account['logged_in']}\n"
        f"Account: {format_account_label(account)}\n"
        f"Broker: {account['broker']}\n"
        f"Server: {account['server'] or 'Unavailable'}\n"
        f"Balance: {_format_money(account['balance'], currency)}\n"
        f"Equity: {_format_money(account['equity'], currency)}\n"
        f"Free Margin: {_format_money(account['free_margin'], currency)}\n"
        f"Margin Level: {_format_percent(account['margin_level'])}\n"
        f"Floating P/L: {_format_money(account['floating_pl'], currency)}\n"
        f"Open Positions: {account['open_positions']}"
    )


def _format_position(position):

    return (
        f"Ticket: {getattr(position, 'ticket', 'Unknown')}\n"
        f"{position_side(position)} {getattr(position, 'symbol', 'Unknown')} "
        f"Vol {getattr(position, 'volume', 0)}\n"
        f"Open: {getattr(position, 'price_open', 0)}\n"
        f"SL: {getattr(position, 'sl', 0)}\n"
        f"TP: {getattr(position, 'tp', 0)}\n"
        f"Profit: {getattr(position, 'profit', 0)}"
    )


def format_positions():

    positions = get_open_positions()

    if positions is None:
        trades = load_trades()
        stored_positions = _open_positions_from_storage(trades)

        if not stored_positions:
            return "Positions\n\nMT5 positions unavailable."

        lines = ["Positions", "", "MT5 unavailable. Showing stored open trades."]

        for position in stored_positions[:20]:
            lines.append("")
            lines.append(
                f"Ticket: {position.get('ticket', 'Unknown')}\n"
                f"Entry: {position.get('entry', 'Unknown')}\n"
                f"TP: {position.get('tp', 'Unknown')}\n"
                f"Break Even: {position.get('break_even', False)}"
            )

        return "\n".join(lines)

    if len(positions) == 0:
        return "Positions\n\nNo open MT5 positions."

    lines = ["Positions", "", f"Open MT5 Positions: {len(positions)}"]

    for position in positions[:20]:
        lines.append("")
        lines.append(_format_position(position))

    if len(positions) > 20:
        lines.append("")
        lines.append(f"Showing 20 of {len(positions)} positions.")

    return "\n".join(lines)
