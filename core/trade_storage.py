import json
import os
import tempfile
from datetime import datetime
from json import JSONDecodeError
from threading import RLock

from core.logger import logger

DATA_FILE = "data/active_trades.json"
HISTORY_FILE = "data/trade_history.json"
PENDING_FILE = "data/pending_position_identities.json"
_STORAGE_LOCK = RLock()


def _ensure_data_dir(path=None):

    if path is None:
        path = DATA_FILE

    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)


def _read_json_file(path=None, expected_type=list):

    if path is None:
        path = DATA_FILE

    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except JSONDecodeError as exc:
        logger.error(f"Corrupted trade storage JSON: {path} | {exc}")
        return []
    except OSError as exc:
        logger.error(f"Failed reading trade storage: {path} | {exc}")
        return []

    if not isinstance(data, expected_type):
        logger.error(f"Invalid trade storage schema: {path}")
        return []

    return data


def _storage_file_is_corrupted(path=None, expected_type=list):

    if path is None:
        path = DATA_FILE

    if not os.path.exists(path):
        return False

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return not isinstance(data, expected_type)
    except (JSONDecodeError, OSError) as exc:
        logger.error(f"Refusing to overwrite unreadable trade storage: {path} | {exc}")
        return True


def _atomic_write_json(data, path=None, expected_type=list):

    if path is None:
        path = DATA_FILE

    _ensure_data_dir(path)

    if _storage_file_is_corrupted(path, expected_type=expected_type):
        logger.error(
            f"Trade storage save skipped to avoid overwriting corrupted file: "
            f"{path}"
        )
        return False

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=os.path.dirname(path) or ".",
            encoding="utf-8"
        ) as temp_file:
            json.dump(data, temp_file, indent=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = temp_file.name

        os.replace(temp_path, path)
        return True

    except OSError as exc:
        logger.error(f"Failed writing trade storage: {path} | {exc}")

        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        return False


def load_trades():

    with _STORAGE_LOCK:
        return _read_json_file()


def save_trades(trades):

    with _STORAGE_LOCK:
        _atomic_write_json(trades)


def add_trade(trade):

    with _STORAGE_LOCK:
        trades = _read_json_file()

        # Replace existing trade with the same Telegram message
        trades = [
            t for t in trades
            if not (
                t["chat_id"] == trade["chat_id"]
                and
                t["message_id"] == trade["message_id"]
            )
        ]

        trades.append(trade)

        _atomic_write_json(trades)


def get_trade(chat_id, message_id):

    trades = load_trades()

    for trade in trades:

        if (
            trade["chat_id"] == chat_id
            and
            trade["message_id"] == message_id
        ):
            return trade

    return None


def update_trade(updated_trade):

    with _STORAGE_LOCK:
        trades = _read_json_file()

        for index, trade in enumerate(trades):

            if (
                trade["chat_id"] == updated_trade["chat_id"]
                and
                trade["message_id"] == updated_trade["message_id"]
            ):
                trades[index] = updated_trade
                _atomic_write_json(trades)
                return

        trades.append(updated_trade)
        _atomic_write_json(trades)


def remove_trade(chat_id, message_id):

    with _STORAGE_LOCK:
        trades = _read_json_file()

        trades = [
            trade for trade in trades
            if not (
                trade["chat_id"] == chat_id
                and
                trade["message_id"] == message_id
            )
        ]

        _atomic_write_json(trades)


def find_trade_by_ticket(ticket):

    trades = load_trades()

    for trade in trades:

        for position in trade.get("positions", []):

            if position["ticket"] == ticket:
                return trade

    return None


def mark_position_closed(ticket):

    with _STORAGE_LOCK:
        trades = _read_json_file()

        updated = False

        for trade in trades:

            for position in trade.get("positions", []):

                if position["ticket"] == ticket:

                    position["closed"] = True
                    updated = True

                    break

        if updated:
            _atomic_write_json(trades)


def _now():

    return datetime.now().isoformat(timespec="seconds")


def _trade_matches(trade, chat_id, message_id):

    return (
        trade.get("chat_id") == chat_id
        and trade.get("message_id") == message_id
    )


def _trade_history_key(trade):

    return (
        trade.get("chat_id"),
        trade.get("message_id"),
    )


def _identity_value(value):

    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _pending_id(record):

    existing = record.get("pending_id")

    if existing:
        return existing

    parts = [
        record.get("chat_id"),
        record.get("message_id"),
        record.get("tp_index"),
        record.get("order_ticket"),
        record.get("deal_ticket"),
        record.get("tp"),
    ]

    return ":".join(str(part) for part in parts)


def load_trade_history():

    with _STORAGE_LOCK:
        return _read_json_file(HISTORY_FILE)


def save_trade_history(history):

    with _STORAGE_LOCK:
        _atomic_write_json(history, HISTORY_FILE)


def load_pending_identities():

    with _STORAGE_LOCK:
        return _read_json_file(PENDING_FILE)


def save_pending_identities(records):

    with _STORAGE_LOCK:
        prepared = []

        for record in records:
            item = dict(record)
            item["pending_id"] = _pending_id(item)
            prepared.append(item)

        _atomic_write_json(prepared, PENDING_FILE)


def add_pending_identity(record):

    with _STORAGE_LOCK:
        records = _read_json_file(PENDING_FILE)
        prepared = dict(record)
        prepared["pending_id"] = _pending_id(prepared)
        prepared.setdefault("identity_status", "pending")
        prepared.setdefault("accepted", True)
        prepared.setdefault("accepted_identity_pending", True)
        prepared.setdefault("created_at", _now())

        records = [
            item for item in records
            if _pending_id(item) != prepared["pending_id"]
        ]
        records.append(prepared)

        _atomic_write_json(records, PENDING_FILE)
        return prepared


def remove_pending_identity(pending_id):

    with _STORAGE_LOCK:
        records = _read_json_file(PENDING_FILE)
        filtered = [
            item for item in records
            if _pending_id(item) != pending_id
        ]

        if len(filtered) != len(records):
            _atomic_write_json(filtered, PENDING_FILE)
            return True

    return False


def pending_for_trade(chat_id, message_id):

    records = load_pending_identities()

    return [
        record for record in records
        if (
            record.get("chat_id") == chat_id
            and record.get("message_id") == message_id
        )
    ]


def has_pending_for_trade(chat_id, message_id):

    return bool(pending_for_trade(chat_id, message_id))


def _position_already_stored(trade, position):

    ticket = _identity_value(
        position.get("position_ticket", position.get("ticket"))
    )
    identifier = _identity_value(
        position.get("position_identifier", position.get("position_id"))
    )

    for stored in trade.get("positions", []):
        stored_ticket = _identity_value(
            stored.get("position_ticket", stored.get("ticket"))
        )
        stored_identifier = _identity_value(
            stored.get("position_identifier", stored.get("position_id"))
        )

        if ticket is not None and stored_ticket == ticket:
            return True

        if identifier is not None and stored_identifier == identifier:
            return True

    return False


def stored_position_for_pending(record):

    pending_id = _pending_id(record)

    with _STORAGE_LOCK:
        for trade in _read_json_file(DATA_FILE):
            for position in trade.get("positions", []):
                if position.get("recovered_pending_id") == pending_id:
                    return position

    return None


def _trade_from_pending(record):

    return {
        "chat_id": record.get("chat_id"),
        "message_id": record.get("message_id"),
        "symbol": record.get("symbol"),
        "side": record.get("side"),
        "textual_direction": record.get("textual_direction"),
        "inferred_direction": record.get("inferred_direction"),
        "final_side": record.get("side"),
        "direction_source": record.get("direction_source"),
        "direction_conflict": record.get("direction_conflict", False),
        "sl": record.get("sl"),
        "raw_message": record.get("raw_message"),
        "positions": [],
    }


def finalize_pending_identity(record, position):

    with _STORAGE_LOCK:
        trades = _read_json_file(DATA_FILE)
        records = _read_json_file(PENDING_FILE)
        chat_id = record.get("chat_id")
        message_id = record.get("message_id")
        pending_id = _pending_id(record)
        trade = None

        for item in trades:
            if _trade_matches(item, chat_id, message_id):
                trade = item
                break

        if trade is None:
            trade = _trade_from_pending(record)
            trades.append(trade)

        added = False

        if not _position_already_stored(trade, position):
            trade.setdefault("positions", []).append(position)
            trade["positions"].sort(
                key=lambda item: item.get("tp_index") or 0
            )
            added = True

        remaining = [
            item for item in records
            if _pending_id(item) != pending_id
        ]

        if not _atomic_write_json(trades, DATA_FILE):
            return None, False, False

        if not _atomic_write_json(remaining, PENDING_FILE):
            return trade, added, False

        return trade, added, True


def archive_trade(trade):

    with _STORAGE_LOCK:
        trades = _read_json_file(DATA_FILE)
        history = _read_json_file(HISTORY_FILE)
        key = _trade_history_key(trade)
        already_archived = any(
            _trade_history_key(item) == key for item in history
        )

        if not already_archived:
            archived = json.loads(json.dumps(trade))
            archived["final_status"] = archived.get("final_status", "closed")
            archived["archived_at"] = _now()
            history.append(archived)
            if not _atomic_write_json(history, HISTORY_FILE):
                return False

        active = [
            item for item in trades
            if _trade_history_key(item) != key
        ]
        if not _atomic_write_json(active, DATA_FILE):
            return False

        return not already_archived
