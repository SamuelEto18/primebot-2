import json
import os
from datetime import datetime
from threading import Lock, Thread

from core.command_handler import (
    get_command_bot_status,
    is_command_bot_running,
    restart_command_bot,
)
from core.lifecycle import is_shutdown_requested, wait_for_shutdown
from core.logger import logger
from core.mt5_service import is_connected as mt5_is_connected
from core.mt5_service import reconnect as reconnect_mt5
from core.notifier import notify_error
from core.runtime import (
    get_runtime,
    mark_listener_error,
    mark_mt5_connected,
    mark_mt5_error,
    mark_position_manager_error,
    mark_watchdog_heartbeat,
    mark_watchdog_stopped,
    pause_bot,
)
from core.trade_storage import load_trades

CHECK_INTERVAL = 5
CONTROL_BOT_HEARTBEAT_MAX_AGE_SECONDS = 30
MT5_RECONNECT_ATTEMPTS = 3
MT5_RECONNECT_DELAY = 5
STORAGE_FILES = [
    "data/active_trades.json",
    "data/processed_messages.json",
    "data/runtime.json",
]

_services = {}
_service_lock = Lock()
_control_bot_restart_lock = Lock()
_watchdog_lock = Lock()
_watchdog_thread = None
_notified = set()


def _parse_runtime_time(value):

    if not value:
        return None

    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _control_bot_heartbeat_current(runtime):
    seen_at = _parse_runtime_time(
        runtime.get("telegram_control_bot_last_seen")
    )

    if seen_at is None:
        return False

    age = (datetime.now() - seen_at).total_seconds()

    return (
        bool(runtime.get("telegram_control_bot"))
        and 0 <= age <= CONTROL_BOT_HEARTBEAT_MAX_AGE_SECONDS
    )


def _notify_once(key, message):

    if is_shutdown_requested():
        return

    if key in _notified:
        return

    _notified.add(key)
    notify_error(message)


def _clear_notification(key):

    if key in _notified:
        _notified.remove(key)


def _thread_runner(name, target, error_marker):

    try:
        target()
    except Exception as exc:
        if is_shutdown_requested():
            logger.info(f"{name} stopped during shutdown")
            return

        logger.exception(f"{name} stopped")
        error_marker(str(exc))
        _notify_once(name, f"Watchdog: {name} stopped. Restarting.\n\n{exc}")
        raise


def _start_service(name, target, error_marker):

    if is_shutdown_requested():
        return None

    with _service_lock:

        if is_shutdown_requested():
            return None

        thread = _services.get(name)

        if thread is not None and thread.is_alive():
            return thread

        logger.warning(f"Watchdog starting {name}")

        thread = Thread(
            target=_thread_runner,
            args=(name, target, error_marker),
            daemon=True,
            name=f"PrimeBot-{name}"
        )

        _services[name] = thread
        thread.start()

        return thread


def _restart_dead_service(name, target, error_marker):

    if is_shutdown_requested():
        return

    thread = _services.get(name)

    if thread is not None and thread.is_alive():
        _clear_notification(name)
        return

    logger.warning(f"Watchdog detected stopped service: {name}")
    _notify_once(name, f"Watchdog: {name} stopped. Restarting.")
    _start_service(name, target, error_marker)


def _check_control_bot():

    if is_shutdown_requested():
        return

    runtime = get_runtime()
    heartbeat_current = _control_bot_heartbeat_current(runtime)

    if is_command_bot_running() and heartbeat_current:

        if "Telegram Control Bot" in _notified:
            notify_error("Watchdog: Telegram Control Bot recovered.")

        _clear_notification("Telegram Control Bot")
        return

    status = get_command_bot_status()
    logger.warning(
        f"Watchdog detected Telegram Control Bot stopped or stale | "
        f"Heartbeat current: {heartbeat_current} | "
        f"Last error: {status.get('last_error')}"
    )

    with _control_bot_restart_lock:

        if is_shutdown_requested():
            return

        runtime = get_runtime()
        heartbeat_current = _control_bot_heartbeat_current(runtime)

        if is_command_bot_running() and heartbeat_current:
            _clear_notification("Telegram Control Bot")
            return

        _notify_once(
            "Telegram Control Bot",
            "Watchdog: Telegram Control Bot stopped. Restarting."
        )

        try:
            restarted = restart_command_bot()
        except Exception as exc:
            if is_shutdown_requested():
                return

            logger.exception("Telegram Control Bot restart failed")
            _notify_once(
                "Telegram Control Bot Restart",
                f"Watchdog: Telegram Control Bot restart failed.\n\n{exc}"
            )
            return

        if restarted:
            logger.info("Watchdog restarted Telegram Control Bot")
            notify_error("Watchdog: Telegram Control Bot recovered.")
            _clear_notification("Telegram Control Bot")
            _clear_notification("Telegram Control Bot Restart")
        else:
            status = get_command_bot_status()
            _notify_once(
                "Telegram Control Bot Restart",
                "Watchdog: Telegram Control Bot restart failed.\n\n"
                f"{status.get('last_error')}"
            )


def _check_mt5():

    if is_shutdown_requested():
        return

    if mt5_is_connected():
        mark_mt5_connected()
        _clear_notification("mt5")
        return

    logger.error("Watchdog detected MT5 disconnected")

    for attempt in range(1, MT5_RECONNECT_ATTEMPTS + 1):

        if is_shutdown_requested():
            return

        try:
            logger.info(
                f"Watchdog attempting MT5 reconnect "
                f"{attempt}/{MT5_RECONNECT_ATTEMPTS}"
            )

            if reconnect_mt5():
                logger.info("Watchdog reconnected MT5")
                mark_mt5_connected()
                if "mt5" in _notified:
                    notify_error("Watchdog: MT5 connection recovered.")
                _clear_notification("mt5")
                return

        except Exception as exc:
            logger.exception(f"MT5 reconnect attempt failed: {exc}")

        wait_for_shutdown(MT5_RECONNECT_DELAY)

    if is_shutdown_requested():
        return

    pause_bot()
    mark_mt5_error("MT5 disconnected and reconnect failed")

    _notify_once(
        "mt5",
        "Watchdog: MT5 disconnected and reconnect failed. Trading paused."
    )


def _check_runtime():

    try:
        get_runtime()
        _clear_notification("runtime")
        return True
    except Exception as exc:
        logger.exception("Watchdog runtime check failed")
        _notify_once("runtime", f"Watchdog: Runtime error.\n\n{exc}")
        return False


def _check_storage():

    try:
        os.makedirs("data", exist_ok=True)

        for file_path in STORAGE_FILES:

            if not os.path.exists(file_path):
                continue

            with open(file_path, "r") as f:
                json.load(f)

        load_trades()
        _clear_notification("storage")
        return True
    except Exception as exc:
        logger.exception("Watchdog storage check failed")
        _notify_once("storage", f"Watchdog: Storage error.\n\n{exc}")
        return False


def _watchdog_loop(start_listener, monitor_positions):

    logger.info("Watchdog starting")

    try:
        _start_service(
            "Telegram Listener",
            start_listener,
            mark_listener_error
        )
        _start_service(
            "Position Manager",
            monitor_positions,
            mark_position_manager_error
        )

        while not is_shutdown_requested():

            try:
                mark_watchdog_heartbeat()

                _check_runtime()
                _check_storage()
                _check_mt5()
                _check_control_bot()

                _restart_dead_service(
                    "Telegram Listener",
                    start_listener,
                    mark_listener_error
                )
                _restart_dead_service(
                    "Position Manager",
                    monitor_positions,
                    mark_position_manager_error
                )

                wait_for_shutdown(CHECK_INTERVAL)

            except Exception as exc:
                if is_shutdown_requested():
                    break

                logger.exception("Watchdog loop error")
                notify_error(f"Watchdog error.\n\n{exc}")
                wait_for_shutdown(CHECK_INTERVAL)

    finally:
        mark_watchdog_stopped()


def start_watchdog(start_listener, monitor_positions):

    global _watchdog_thread

    with _watchdog_lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return _watchdog_thread

        if is_shutdown_requested():
            return None

        _watchdog_thread = Thread(
            target=_watchdog_loop,
            args=(start_listener, monitor_positions),
            daemon=True,
            name="PrimeBot-Watchdog"
        )
        _watchdog_thread.start()
        return _watchdog_thread


def stop_watchdog(timeout=10):

    global _watchdog_thread

    thread = _watchdog_thread

    if thread is not None and thread.is_alive():
        thread.join(timeout)

    return thread is None or not thread.is_alive()


def get_service_thread(name):

    return _services.get(name)
