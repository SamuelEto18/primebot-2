import json
import os
import tempfile
from json import JSONDecodeError
from threading import RLock

from core.logger import logger

DATA_FILE = "data/processed_management_messages.json"
_STORAGE_LOCK = RLock()


def _ensure_data_dir():
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)


def _read_json_file():
    if not os.path.exists(DATA_FILE):
        return set()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except JSONDecodeError as exc:
        logger.error(f"Corrupted processed-management JSON: {DATA_FILE} | {exc}")
        return set()
    except OSError as exc:
        logger.error(f"Failed reading processed management messages: {DATA_FILE} | {exc}")
        return set()

    if not isinstance(data, list):
        logger.error(f"Invalid processed-management schema: {DATA_FILE}")
        return set()

    return set(str(item) for item in data)


def _storage_file_is_corrupted():
    if not os.path.exists(DATA_FILE):
        return False

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return not isinstance(data, list)
    except (JSONDecodeError, OSError) as exc:
        logger.error(
            f"Refusing to overwrite unreadable processed management file: "
            f"{DATA_FILE} | {exc}"
        )
        return True


def _atomic_write_json(keys):
    _ensure_data_dir()

    if _storage_file_is_corrupted():
        logger.error(
            f"Processed-management save skipped to avoid overwriting corrupted "
            f"file: {DATA_FILE}"
        )
        return

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=os.path.dirname(DATA_FILE),
            encoding="utf-8",
        ) as temp_file:
            json.dump(sorted(keys), temp_file, indent=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = temp_file.name

        os.replace(temp_path, DATA_FILE)
    except OSError as exc:
        logger.error(f"Failed writing processed management messages: {DATA_FILE} | {exc}")

        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def load_processed_management_messages():
    with _STORAGE_LOCK:
        return _read_json_file()


def is_management_processed(key):
    return str(key) in load_processed_management_messages()


def mark_management_processed(key):
    with _STORAGE_LOCK:
        messages = _read_json_file()
        messages.add(str(key))
        _atomic_write_json(messages)
