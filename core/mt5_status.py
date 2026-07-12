from core import mt5_service

DEFAULT_BROKER = "PU Prime"


def get_terminal_info():

    return mt5_service.terminal_info()


def get_account_info():

    return mt5_service.account_info()


def get_open_positions():

    positions = mt5_service.positions_get()

    if positions is None:
        return None

    return list(positions)


def is_connected():

    return mt5_service.is_connected()


def is_logged_in():

    return get_account_info() is not None


def get_account_number():

    account = get_account_info()

    return getattr(account, "login", None) if account else None


def get_broker():

    account = get_account_info()

    if account is None:
        return DEFAULT_BROKER

    return getattr(account, "company", None) or DEFAULT_BROKER


def get_server():

    account = get_account_info()

    return getattr(account, "server", None) if account else None


def get_balance():

    account = get_account_info()

    return getattr(account, "balance", None) if account else None


def get_equity():

    account = get_account_info()

    return getattr(account, "equity", None) if account else None


def get_free_margin():

    account = get_account_info()

    return getattr(account, "margin_free", None) if account else None


def get_margin_level():

    account = get_account_info()

    return getattr(account, "margin_level", None) if account else None


def get_floating_pl():

    account = get_account_info()

    return getattr(account, "profit", None) if account else None


def get_currency():

    account = get_account_info()

    return getattr(account, "currency", "") if account else ""


def get_open_position_count():

    positions = get_open_positions()

    return len(positions) if positions is not None else None


def get_account_summary():

    account = get_account_info()
    positions = get_open_positions()

    return {
        "connected": is_connected(),
        "logged_in": account is not None,
        "account_number": getattr(account, "login", None) if account else None,
        "broker": getattr(account, "company", None) if account else DEFAULT_BROKER,
        "server": getattr(account, "server", None) if account else None,
        "balance": getattr(account, "balance", None) if account else None,
        "equity": getattr(account, "equity", None) if account else None,
        "free_margin": getattr(account, "margin_free", None) if account else None,
        "margin_level": getattr(account, "margin_level", None) if account else None,
        "floating_pl": getattr(account, "profit", None) if account else None,
        "currency": getattr(account, "currency", "") if account else "",
        "open_positions": len(positions) if positions is not None else None,
    }


def format_account_label(summary=None):

    if summary is None:
        summary = get_account_summary()

    account_number = summary.get("account_number")
    server = summary.get("server")

    if account_number is None:
        return "Unavailable"

    if server:
        return f"{account_number} | {server}"

    return str(account_number)


def position_side(position):

    return "BUY" if position.type == mt5_service.position_type_buy() else "SELL"
