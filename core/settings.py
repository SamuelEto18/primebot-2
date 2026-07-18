import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

from config import PRIMEBOT2_TELEGRAM_CHANNEL_ID


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    api_id: int
    api_hash: str
    channel_id: int
    session_name: str
    mt5_login: int = None
    sticker_close_all_document_ids: frozenset = frozenset()
    sticker_break_even_document_ids: frozenset = frozenset()
    sticker_discovery_notify: bool = False


REQUIRED_VALUES = (
    "BOT_TOKEN",
    "ADMIN_ID",
    "API_ID",
    "API_HASH",
    "CHANNEL_ID",
    "SESSION_NAME",
)

INTEGER_VALUES = (
    "ADMIN_ID",
    "API_ID",
    "CHANNEL_ID",
)

OPTIONAL_VALUES = (
    "MT5_LOGIN",
    "TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS",
    "TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS",
    "TELEGRAM_STICKER_DISCOVERY_NOTIFY",
)


def _read_env(name):
    value = os.getenv(name)

    if value is None:
        return None

    value = value.strip()

    return value or None


def _parse_int(name, value):
    if value is None:
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a valid integer") from exc


def _parse_document_ids(name, value):
    if value is None:
        return frozenset()

    document_ids = set()

    for item in re.split(r"[\s,;]+", value):
        if not item:
            continue

        document_id = _parse_int(name, item)

        if document_id is None or document_id <= 0:
            raise ConfigurationError(
                f"{name} must contain positive Telegram document IDs"
            )

        document_ids.add(document_id)

    return frozenset(document_ids)


def _parse_bool(name, value):
    if value is None:
        return False

    normalized = value.strip().lower()

    if normalized in ("1", "true", "yes", "on"):
        return True

    if normalized in ("0", "false", "no", "off"):
        return False

    raise ConfigurationError(
        f"{name} must be one of: true, false, 1, 0, yes, no, on, off"
    )


def load_settings(validate=True):
    load_dotenv()

    values = {name: _read_env(name) for name in REQUIRED_VALUES}
    values.update({name: _read_env(name) for name in OPTIONAL_VALUES})

    if validate:
        missing = [name for name in REQUIRED_VALUES if values[name] is None]

        if missing:
            raise ConfigurationError(
                "Missing required configuration value(s): "
                + ", ".join(missing)
            )

    parsed = {}

    for name in INTEGER_VALUES:
        parsed[name] = _parse_int(name, values[name])

    parsed["MT5_LOGIN"] = _parse_int("MT5_LOGIN", values["MT5_LOGIN"])

    if (
        parsed["CHANNEL_ID"] is not None
        and parsed["CHANNEL_ID"] != PRIMEBOT2_TELEGRAM_CHANNEL_ID
    ):
        raise ConfigurationError(
            "CHANNEL_ID does not match the fixed PrimeBot 2 Telegram source "
            f"({PRIMEBOT2_TELEGRAM_CHANNEL_ID})"
        )

    close_all_ids = _parse_document_ids(
        "TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS",
        values["TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS"],
    )
    break_even_ids = _parse_document_ids(
        "TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS",
        values["TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS"],
    )

    if close_all_ids & break_even_ids:
        raise ConfigurationError(
            "A Telegram sticker document ID cannot enable both close-all "
            "and break-even"
        )

    return Settings(
        bot_token=values["BOT_TOKEN"],
        admin_id=parsed["ADMIN_ID"],
        api_id=parsed["API_ID"],
        api_hash=values["API_HASH"],
        channel_id=parsed["CHANNEL_ID"],
        session_name=values["SESSION_NAME"],
        mt5_login=parsed["MT5_LOGIN"],
        sticker_close_all_document_ids=close_all_ids,
        sticker_break_even_document_ids=break_even_ids,
        sticker_discovery_notify=_parse_bool(
            "TELEGRAM_STICKER_DISCOVERY_NOTIFY",
            values["TELEGRAM_STICKER_DISCOVERY_NOTIFY"],
        ),
    )


def validate_settings():
    return load_settings(validate=True)
