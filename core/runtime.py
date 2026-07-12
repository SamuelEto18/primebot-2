import errno
import json
import os
import tempfile
import time
from datetime import datetime
from json import JSONDecodeError
from threading import RLock

from config import AUTO_EXECUTE
from core.logger import logger

RUNTIME_FILE = "data/runtime.json"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_RUNTIME_LOCK = RLock()
_DAILY_COUNTERS = (
    "signals_today",
    "executed_today",
    "errors_today",
)
_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2)
_WINDOWS_SHARING_WINERRORS = {5, 32, 33}


def _now():
    return datetime.now().strftime(TIME_FORMAT)


def _today():
    return datetime.now().date().isoformat()


def _default_state():
    return {
        "paused": False,
        "auto_execute": AUTO_EXECUTE,
        "started": _now(),
        "stats_date": _today(),
        "signals_today": 0,
        "executed_today": 0,
        "errors_today": 0,
        "version": "1.0.0",
        "last_signal_time": None,
        "last_trade_time": None,
        "telegram_listener": False,
        "telegram_listener_last_seen": None,
        "telegram_control_bot": False,
        "telegram_control_bot_last_seen": None,
        "position_manager": False,
        "position_manager_last_seen": None,
        "position_manager_error": None,
        "statistics_scheduler": False,
        "statistics_scheduler_last_seen": None,
        "statistics_scheduler_error": None,
        "statistics_last_successful_report": None,
        "statistics_last_successful_weekly": None,
        "statistics_last_successful_monthly": None,
        "telegram_listener_error": None,
        "telegram_control_bot_error": None,
        "watchdog": False,
        "watchdog_last_seen": None,
        "mt5_connected": False,
        "mt5_last_error": None,
        "last_error": None,
    }


def _ensure_data_dir():
    directory = os.path.dirname(RUNTIME_FILE)

    if directory:
        os.makedirs(directory, exist_ok=True)


def _read_runtime_file():
    if not os.path.exists(RUNTIME_FILE):
        return _default_state(), True

    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except JSONDecodeError as exc:
        logger.error(f"Corrupted runtime JSON: {RUNTIME_FILE} | {exc}")
        return _default_state(), False
    except OSError as exc:
        logger.error(f"Failed reading runtime state: {RUNTIME_FILE} | {exc}")
        return _default_state(), False

    if not isinstance(state, dict):
        logger.error(f"Invalid runtime schema: {RUNTIME_FILE}")
        return _default_state(), False

    return state, True


def _runtime_file_is_corrupted():
    if not os.path.exists(RUNTIME_FILE):
        return False

    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return not isinstance(state, dict)
    except (JSONDecodeError, OSError) as exc:
        logger.error(
            f"Refusing to overwrite unreadable runtime state: "
            f"{RUNTIME_FILE} | {exc}"
        )
        return True


def _merge_defaults_and_rollover(state):
    default = _default_state()
    changed = False

    for key, value in default.items():
        if key not in state:
            state[key] = value
            changed = True

    current_date = _today()

    if state.get("stats_date") != current_date:
        for counter in _DAILY_COUNTERS:
            state[counter] = 0

        state["stats_date"] = current_date
        changed = True

    return changed


def _is_transient_replace_error(exc):
    winerror = getattr(exc, "winerror", None)

    return (
        isinstance(exc, PermissionError)
        or winerror in _WINDOWS_SHARING_WINERRORS
        or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM)
    )


def _replace_runtime_file(temp_path):
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(temp_path, RUNTIME_FILE)
            return
        except OSError as exc:
            last_attempt = attempt == len(_REPLACE_RETRY_DELAYS)

            if last_attempt or not _is_transient_replace_error(exc):
                raise

            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


def _atomic_write_runtime(state):
    _ensure_data_dir()

    if _runtime_file_is_corrupted():
        logger.error(
            f"Runtime save skipped to avoid overwriting corrupted file: "
            f"{RUNTIME_FILE}"
        )
        return False

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=os.path.dirname(RUNTIME_FILE) or ".",
            prefix="runtime-",
            suffix=".tmp",
            encoding="utf-8",
        ) as temp_file:
            json.dump(state, temp_file, indent=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = temp_file.name

        _replace_runtime_file(temp_path)
        temp_path = None
        return True
    except OSError as exc:
        logger.error(f"Failed writing runtime state: {RUNTIME_FILE} | {exc}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as exc:
                logger.error(
                    f"Failed removing runtime temp file: {temp_path} | {exc}"
                )


def _prepare_runtime_state_locked():
    _ensure_data_dir()
    state, writable = _read_runtime_file()
    changed = _merge_defaults_and_rollover(state)
    missing = not os.path.exists(RUNTIME_FILE)

    return state, writable, changed, missing


def _mutate_runtime(mutator):
    with _RUNTIME_LOCK:
        state, writable, _changed, _missing = _prepare_runtime_state_locked()

        if not writable:
            logger.error(
                f"Runtime mutation skipped because state is not writable: "
                f"{RUNTIME_FILE}"
            )
            return False

        mutator(state)
        return _atomic_write_runtime(state)


def load_runtime():
    with _RUNTIME_LOCK:
        state, writable, changed, missing = _prepare_runtime_state_locked()

        if writable and (changed or missing):
            _atomic_write_runtime(state)

        return state


def save_runtime(state):
    with _RUNTIME_LOCK:
        prepared = dict(state)
        _merge_defaults_and_rollover(prepared)
        return _atomic_write_runtime(prepared)


def update_runtime(**updates):
    return _mutate_runtime(lambda state: state.update(updates))


def get_runtime():
    return load_runtime()


def is_paused():
    return load_runtime()["paused"]


def pause_bot():
    return update_runtime(paused=True)


def resume_bot():
    return update_runtime(paused=False)


def is_auto_execute():
    return load_runtime()["auto_execute"]


def set_auto_execute(enabled):
    return update_runtime(auto_execute=enabled)


def _increment_counter(counter, amount=1):
    def mutate(state):
        state[counter] += amount

    return _mutate_runtime(mutate)


def increment_signal():
    return _increment_counter("signals_today")


def increment_execution():
    return _increment_counter("executed_today")


def increment_error():
    return _increment_counter("errors_today")


def mark_signal_received():
    def mutate(state):
        state["signals_today"] += 1
        state["last_signal_time"] = _now()

    return _mutate_runtime(mutate)


def mark_trade_executed(count=1):
    def mutate(state):
        state["executed_today"] += count
        state["last_trade_time"] = _now()

    return _mutate_runtime(mutate)


def mark_listener_started():
    return update_runtime(
        telegram_listener=True,
        telegram_listener_last_seen=_now(),
        telegram_listener_error=None,
    )


def mark_control_bot_started():
    return update_runtime(
        telegram_control_bot=True,
        telegram_control_bot_last_seen=_now(),
        telegram_control_bot_error=None,
    )


def mark_control_bot_heartbeat():
    return update_runtime(
        telegram_control_bot=True,
        telegram_control_bot_last_seen=_now(),
    )


def mark_position_manager_started():
    return update_runtime(
        position_manager=True,
        position_manager_last_seen=_now(),
        position_manager_error=None,
    )


def mark_position_manager_heartbeat():
    return update_runtime(
        position_manager=True,
        position_manager_last_seen=_now(),
    )


def mark_statistics_scheduler_started():
    return update_runtime(
        statistics_scheduler=True,
        statistics_scheduler_last_seen=_now(),
        statistics_scheduler_error=None,
    )


def mark_statistics_scheduler_heartbeat():
    return update_runtime(
        statistics_scheduler=True,
        statistics_scheduler_last_seen=_now(),
    )


def mark_statistics_scheduler_success(report_type, period_end):
    key = f"statistics_last_successful_{report_type}"
    timestamp = _now()

    return update_runtime(
        statistics_scheduler=True,
        statistics_scheduler_last_seen=timestamp,
        statistics_last_successful_report=(
            f"{report_type}:{period_end.isoformat(timespec='seconds')}"
        ),
        **{key: period_end.isoformat(timespec="seconds")},
    )


def mark_statistics_scheduler_stopped():
    return update_runtime(statistics_scheduler=False)


def mark_statistics_scheduler_error(message):
    def mutate(state):
        state["statistics_scheduler"] = False
        state["statistics_scheduler_error"] = message
        state["last_error"] = message
        state["errors_today"] += 1

    return _mutate_runtime(mutate)


def mark_position_manager_error(message):
    def mutate(state):
        state["position_manager"] = False
        state["position_manager_error"] = message
        state["last_error"] = message
        state["errors_today"] += 1

    return _mutate_runtime(mutate)


def mark_listener_error(message):
    return update_runtime(
        telegram_listener=False,
        telegram_listener_error=message,
        last_error=message,
    )


def mark_control_bot_stopped():
    return update_runtime(telegram_control_bot=False)


def mark_control_bot_error(message):
    return update_runtime(
        telegram_control_bot=False,
        telegram_control_bot_error=message,
        last_error=message,
    )


def mark_watchdog_heartbeat():
    return update_runtime(
        watchdog=True,
        watchdog_last_seen=_now(),
    )


def mark_mt5_connected():
    return update_runtime(
        mt5_connected=True,
        mt5_last_error=None,
    )


def mark_mt5_error(message):
    return update_runtime(
        mt5_connected=False,
        mt5_last_error=message,
        last_error=message,
    )


def initialize_process_runtime():
    def mutate(state):
        paused = state.get("paused", False)
        auto_execute = state.get("auto_execute", AUTO_EXECUTE)

        state.update({
            "paused": paused,
            "auto_execute": auto_execute,
            "started": _now(),
            "telegram_listener": False,
            "telegram_listener_last_seen": None,
            "telegram_listener_error": None,
            "telegram_control_bot": False,
            "telegram_control_bot_last_seen": None,
            "telegram_control_bot_error": None,
            "position_manager": False,
            "position_manager_last_seen": None,
            "position_manager_error": None,
            "statistics_scheduler": False,
            "statistics_scheduler_last_seen": None,
            "statistics_scheduler_error": None,
            "watchdog": False,
            "watchdog_last_seen": None,
            "mt5_connected": False,
            "mt5_last_error": None,
            "last_error": None,
        })

    return _mutate_runtime(mutate)


def mark_listener_heartbeat():
    return update_runtime(
        telegram_listener=True,
        telegram_listener_last_seen=_now(),
    )


def mark_listener_stopped():
    return update_runtime(telegram_listener=False)


def mark_position_manager_stopped():
    return update_runtime(position_manager=False)


def mark_watchdog_stopped():
    return update_runtime(watchdog=False)


def mark_runtime_shutdown_complete():
    return update_runtime(
        telegram_listener=False,
        telegram_control_bot=False,
        position_manager=False,
        statistics_scheduler=False,
        watchdog=False,
        mt5_connected=False,
    )
