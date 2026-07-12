import json
import os
import tempfile
from json import JSONDecodeError
from threading import RLock

from core.logger import logger

DATA_FILE = "data/processed_messages.json"
_STORAGE_LOCK = RLock()


def _ensure_data_dir():

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)


def _read_json_file():

    if not os.path.exists(DATA_FILE):
        return set()

    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    except JSONDecodeError as exc:
        logger.error(f"Corrupted processed-message JSON: {DATA_FILE} | {exc}")
        return set()
    except OSError as exc:
        logger.error(f"Failed reading processed messages: {DATA_FILE} | {exc}")
        return set()

    if not isinstance(data, list):
        logger.error(f"Invalid processed-message schema: {DATA_FILE}")
        return set()

    return set(data)


def _storage_file_is_corrupted():

    if not os.path.exists(DATA_FILE):
        return False

    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        return not isinstance(data, list)
    except (JSONDecodeError, OSError) as exc:
        logger.error(f"Refusing to overwrite unreadable processed messages: {DATA_FILE} | {exc}")
        return True


def _atomic_write_json(messages):

    _ensure_data_dir()

    if _storage_file_is_corrupted():
        logger.error(
            f"Processed-message save skipped to avoid overwriting corrupted "
            f"file: {DATA_FILE}"
        )
        return

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=os.path.dirname(DATA_FILE),
            encoding="utf-8"
        ) as temp_file:
            json.dump(list(messages), temp_file, indent=4)
            temp_path = temp_file.name

        os.replace(temp_path, DATA_FILE)

    except OSError as exc:
        logger.error(f"Failed writing processed messages: {DATA_FILE} | {exc}")

        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def load_processed_messages():

    with _STORAGE_LOCK:
        return _read_json_file()


def save_processed_messages(messages):

    with _STORAGE_LOCK:
        _atomic_write_json(messages)


def is_processed(message_id):

    messages = load_processed_messages()

    return message_id in messages


def mark_processed(message_id):

    with _STORAGE_LOCK:
        messages = _read_json_file()

        messages.add(message_id)

        _atomic_write_json(messages)
