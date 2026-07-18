import json
import os
import tempfile
import time
from datetime import datetime
from json import JSONDecodeError
from threading import RLock

from core.logger import logger


DATA_FILE = "data/processed_sticker_management.json"
SCHEMA_VERSION = 2

STATUS_RECEIVED = "RECEIVED"
STATUS_VALIDATED = "VALIDATED"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_PARTIAL_FAILURE = "PARTIAL_FAILURE"
STATUS_FAILED = "FAILED"
STATUS_IGNORED = "IGNORED"
STATUS_DRY_RUN_CONSUMED = "DRY_RUN_CONSUMED"

RECOVERABLE_STATUSES = frozenset({
    STATUS_RECEIVED,
    STATUS_VALIDATED,
    STATUS_IN_PROGRESS,
    STATUS_PARTIAL_FAILURE,
})
TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_DRY_RUN_CONSUMED,
})

_ALLOWED_TRANSITIONS = {
    None: {
        STATUS_RECEIVED,
        STATUS_IGNORED,
        STATUS_DRY_RUN_CONSUMED,
    },
    STATUS_RECEIVED: {
        STATUS_VALIDATED,
        STATUS_FAILED,
        STATUS_IGNORED,
        STATUS_DRY_RUN_CONSUMED,
    },
    STATUS_VALIDATED: {
        STATUS_IN_PROGRESS,
        STATUS_PARTIAL_FAILURE,
        STATUS_FAILED,
        STATUS_DRY_RUN_CONSUMED,
    },
    STATUS_IN_PROGRESS: {
        STATUS_IN_PROGRESS,
        STATUS_COMPLETED,
        STATUS_PARTIAL_FAILURE,
        STATUS_FAILED,
    },
    STATUS_PARTIAL_FAILURE: {
        STATUS_IN_PROGRESS,
        STATUS_COMPLETED,
        STATUS_PARTIAL_FAILURE,
        STATUS_FAILED,
    },
}

_STORAGE_LOCK = RLock()


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _default_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "operations": {},
    }


def _valid_operation(record):
    return (
        isinstance(record, dict)
        and record.get("status") in (
            RECOVERABLE_STATUSES | TERMINAL_STATUSES
        )
        and isinstance(record.get("transitions"), list)
        and isinstance(record.get("positions", {}), dict)
    )


def _read_state():
    if not os.path.exists(DATA_FILE):
        return _default_state(), True

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (JSONDecodeError, OSError) as exc:
        logger.error(
            "Failed reading sticker-management state | "
            f"Path={DATA_FILE} Error={exc}"
        )
        return None, False

    if (
        not isinstance(state, dict)
        or state.get("schema_version") != SCHEMA_VERSION
        or not isinstance(state.get("operations"), dict)
        or not all(_valid_operation(item) for item in state["operations"].values())
    ):
        logger.error(
            "Invalid sticker-management state schema | "
            f"Path={DATA_FILE}"
        )
        return None, False

    return state, True


def _atomic_write(state):
    directory = os.path.dirname(DATA_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=directory,
            prefix="sticker-management-",
            suffix=".tmp",
            encoding="utf-8",
        ) as temp_file:
            json.dump(state, temp_file, indent=4, sort_keys=True)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = temp_file.name

        os.replace(temp_path, DATA_FILE)
        temp_path = None
        return True
    except OSError as exc:
        logger.error(
            "Failed writing sticker-management state | "
            f"Path={DATA_FILE} Error={exc}"
        )
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def operation_key(chat_id, message_id):
    return f"{int(chat_id)}:{int(message_id)}"


def _transition_record(record, status, **fields):
    previous = record.get("status")

    if status not in _ALLOWED_TRANSITIONS.get(previous, set()):
        logger.error(
            "Invalid sticker-management state transition | "
            f"From={previous} To={status}"
        )
        return False

    changed_at = _now()
    record["status"] = status
    record["updated_at"] = changed_at
    record.setdefault("transitions", []).append({
        "from": previous,
        "to": status,
        "at": changed_at,
    })

    for name, value in fields.items():
        if value is not None:
            record[name] = value

    if status in TERMINAL_STATUSES:
        record["completed_at"] = changed_at

    return True


def receive_operation(chat_id, message_id, command, document_id, metadata=None):
    key = operation_key(chat_id, message_id)

    with _STORAGE_LOCK:
        state, readable = _read_state()

        if not readable:
            return {
                "created": False,
                "key": key,
                "error": "Durable sticker-management state is unreadable",
            }

        existing = state["operations"].get(key)

        if existing is not None:
            return {
                "created": False,
                "key": key,
                "record": json.loads(json.dumps(existing)),
            }

        received_at = _now()
        record = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "document_id": int(document_id),
            "command": str(command),
            "status": STATUS_RECEIVED,
            "received_at": received_at,
            "updated_at": received_at,
            "metadata": dict(metadata or {}),
            "positions": {},
            "transitions": [{
                "from": None,
                "to": STATUS_RECEIVED,
                "at": received_at,
            }],
        }
        state["operations"][key] = record

        if not _atomic_write(state):
            return {
                "created": False,
                "key": key,
                "error": "Durable sticker-management receipt could not be stored",
            }

        return {
            "created": True,
            "key": key,
            "record": json.loads(json.dumps(record)),
        }


def transition_operation(key, status, **fields):
    with _STORAGE_LOCK:
        state, readable = _read_state()

        if not readable:
            return False

        record = state["operations"].get(str(key))

        if record is None:
            logger.error(
                "Sticker-management transition has no durable operation | "
                f"Operation={key}"
            )
            return False

        if not _transition_record(record, status, **fields):
            return False

        return _atomic_write(state)


def prepare_operation_positions(key, positions, discovery=None):
    prepared = {str(name): dict(value) for name, value in positions.items()}

    with _STORAGE_LOCK:
        state, readable = _read_state()

        if not readable:
            return False

        record = state["operations"].get(str(key))

        if record is None or record.get("status") not in {
            STATUS_VALIDATED,
            STATUS_IN_PROGRESS,
            STATUS_PARTIAL_FAILURE,
        }:
            logger.error(
                "Sticker-management target preparation is invalid | "
                f"Operation={key}"
            )
            return False

        if record.get("targets_prepared"):
            return True

        record["positions"] = prepared
        record["targets_prepared"] = True
        record["discovery"] = dict(discovery or {})

        if not _transition_record(record, STATUS_IN_PROGRESS):
            return False

        return _atomic_write(state)


def record_position_outcome(key, position_key, outcome):
    with _STORAGE_LOCK:
        state, readable = _read_state()

        if not readable:
            return False

        record = state["operations"].get(str(key))
        position = (
            record.get("positions", {}).get(str(position_key))
            if record is not None
            else None
        )

        if position is None:
            logger.error(
                "Sticker-management outcome has no durable position target | "
                f"Operation={key} Position={position_key}"
            )
            return False

        attempt = dict(outcome)
        attempt["attempted_at"] = _now()
        position.setdefault("attempts", []).append(attempt)
        position["outcome"] = dict(outcome)
        position["updated_at"] = attempt["attempted_at"]

        if outcome.get("status") in ("PENDING", "FAILED"):
            attempts = len(position["attempts"])
            delay = min(15 * (2 ** max(0, attempts - 1)), 300)
            position["next_retry_at"] = time.time() + delay
        else:
            position.pop("next_retry_at", None)

        record["updated_at"] = attempt["attempted_at"]
        return _atomic_write(state)


def load_operation(key):
    with _STORAGE_LOCK:
        state, readable = _read_state()

        if not readable:
            return None

        record = state["operations"].get(str(key))
        return json.loads(json.dumps(record)) if record is not None else None


def load_operations():
    with _STORAGE_LOCK:
        state, readable = _read_state()
        return (
            json.loads(json.dumps(state["operations"]))
            if readable
            else None
        )
