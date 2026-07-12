from core.command_handler import start_command_bot, stop_command_bot
from core.lifecycle import (
    mark_running,
    mark_shutdown_completed,
    request_shutdown,
    wait_for_shutdown,
)
from core.listener import start_listener, stop_listener
from core.mt5_service import initialize as initialize_mt5
from core.mt5_service import shutdown as shutdown_mt5
from core.notifier import notify_error, notify_start
from core.position_manager import monitor_positions, run_startup_recovery
from core.runtime import (
    initialize_process_runtime,
    mark_mt5_connected,
    mark_mt5_error,
    mark_runtime_shutdown_complete,
    pause_bot,
)
from core.settings import ConfigurationError, validate_settings
from core.statistics_scheduler import (
    start_statistics_scheduler,
    stop_statistics_scheduler,
)
from core.telegram_control import stop_notifications
from core.watchdog import get_service_thread, start_watchdog, stop_watchdog
from core.logger import logger


def _initialize_mt5_at_startup():

    try:
        initialize_mt5()
        mark_mt5_connected()
        logger.info("MT5 startup initialization successful")
    except Exception as exc:
        logger.exception("MT5 startup initialization failed")
        pause_bot()
        mark_mt5_error(str(exc))
        notify_error(
            "MT5 startup initialization failed. "
            "Trading paused. Watchdog will retry.\n\n"
            f"{exc}"
        )


def _join_service_thread(name, timeout=10):

    thread = get_service_thread(name)

    if thread is not None and thread.is_alive():
        thread.join(timeout)


def _cleanup():

    logger.info("PrimeBot shutdown requested")

    request_shutdown()

    try:
        stop_watchdog()
    except Exception as exc:
        logger.error(f"Watchdog shutdown failed: {exc}")

    try:
        stop_command_bot()
    except Exception as exc:
        logger.error(f"Control Bot shutdown failed: {exc}")

    try:
        stop_statistics_scheduler()
    except Exception as exc:
        logger.error(f"Statistics scheduler shutdown failed: {exc}")

    try:
        stop_listener()
        _join_service_thread("Telegram Listener")
    except Exception as exc:
        logger.error(f"Telegram listener shutdown failed: {exc}")

    try:
        _join_service_thread("Position Manager")
    except Exception as exc:
        logger.error(f"Position Manager shutdown failed: {exc}")

    try:
        stop_notifications()
    except Exception as exc:
        logger.error(f"Notification shutdown failed: {exc}")

    try:
        shutdown_mt5()
    except Exception as exc:
        logger.error(f"MT5 shutdown failed: {exc}")

    try:
        mark_runtime_shutdown_complete()
    except Exception as exc:
        logger.error(f"Runtime shutdown marker failed: {exc}")

    mark_shutdown_completed()
    logger.info("PrimeBot shutdown complete")


def main():

    started = False

    try:
        logger.info("=" * 60)
        logger.info("PrimeBot Starting")
        logger.info("=" * 60)

        validate_settings()
        initialize_process_runtime()
        mark_running()
        started = True

        notify_start()

        _initialize_mt5_at_startup()
        run_startup_recovery()

        start_command_bot()

        try:
            start_statistics_scheduler()
        except Exception as exc:
            logger.exception("Statistics scheduler startup failed")

        start_watchdog(
            start_listener,
            monitor_positions
        )

        while not wait_for_shutdown(1):
            pass

    except KeyboardInterrupt:
        logger.info("PrimeBot stopped by KeyboardInterrupt")
    except ConfigurationError as exc:
        logger.error(f"Configuration error: {exc}")
    except Exception as exc:
        logger.exception("PrimeBot runtime error")
        notify_error(str(exc))
    finally:
        if started:
            _cleanup()


if __name__ == "__main__":
    main()
