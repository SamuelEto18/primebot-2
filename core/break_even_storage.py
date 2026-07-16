import json
import os
import tempfile
from json import JSONDecodeError
from threading import RLock

from core.logger import logger


STATE_FILE = "data/break_even_actions.json"
_STORAGE_LOCK = RLock()


def _default_state():
    return {
        "actions": {},
        "pending": {},
    }


def _ensure_data_dir():
    directory = os.path.dirname(STATE_FILE)

    if directory:
        os.makedirs(directory, exist_ok=True)


def _read_state():
    if not os.path.exists(STATE_FILE):
        return _default_state(), True

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (JSONDecodeError, OSError) as exc:
        logger.error(f"Failed reading break-even action state: {STATE_FILE} | {exc}")
        return None, False

    if not isinstance(state, dict):
        logger.error(f"Invalid break-even action state schema: {STATE_FILE}")
        return None, False

    actions = state.get("actions")
    pending = state.get("pending")

    if not isinstance(actions, dict) or not isinstance(pending, dict):
        logger.error(f"Invalid break-even action state schema: {STATE_FILE}")
        return None, False

    return state, True


def _state_file_is_corrupted():
    if not os.path.exists(STATE_FILE):
        return False

    _state, readable = _read_state()
    return not readable


def load_break_even_state():
    with _STORAGE_LOCK:
        state, readable = _read_state()
        return state if readable else None


def save_break_even_state(state):
    with _STORAGE_LOCK:
        if _state_file_is_corrupted():
            logger.error(
                "Break-even action save skipped to avoid overwriting "
                f"unreadable state: {STATE_FILE}"
            )
            return False

        if not isinstance(state, dict):
            return False

        if not isinstance(state.get("actions"), dict):
            return False

        if not isinstance(state.get("pending"), dict):
            return False

        _ensure_data_dir()
        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                "w",
                delete=False,
                dir=os.path.dirname(STATE_FILE) or ".",
                prefix="break-even-",
                suffix=".tmp",
                encoding="utf-8",
            ) as temp_file:
                json.dump(state, temp_file, indent=4, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = temp_file.name

            os.replace(temp_path, STATE_FILE)
            temp_path = None
            return True
        except OSError as exc:
            logger.error(f"Failed writing break-even action state: {STATE_FILE} | {exc}")
            return False
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
