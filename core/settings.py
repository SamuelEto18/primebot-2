import os
from dataclasses import dataclass

from dotenv import load_dotenv


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


def load_settings(validate=True):
    load_dotenv()

    values = {name: _read_env(name) for name in REQUIRED_VALUES}

    if validate:
        missing = [name for name, value in values.items() if value is None]

        if missing:
            raise ConfigurationError(
                "Missing required configuration value(s): "
                + ", ".join(missing)
            )

    parsed = {}

    for name in INTEGER_VALUES:
        parsed[name] = _parse_int(name, values[name])

    return Settings(
        bot_token=values["BOT_TOKEN"],
        admin_id=parsed["ADMIN_ID"],
        api_id=parsed["API_ID"],
        api_hash=values["API_HASH"],
        channel_id=parsed["CHANNEL_ID"],
        session_name=values["SESSION_NAME"],
    )


def validate_settings():
    return load_settings(validate=True)
