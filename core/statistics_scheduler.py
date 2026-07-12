import time
from datetime import datetime
from threading import Event, RLock, Thread, current_thread

from core.lifecycle import is_shutdown_requested, wait_for_shutdown
from core.logger import logger
from core.runtime import (
    mark_statistics_scheduler_error,
    mark_statistics_scheduler_heartbeat,
    mark_statistics_scheduler_started,
    mark_statistics_scheduler_stopped,
    mark_statistics_scheduler_success,
)
from core.statistics import (
    REPORT_TZ,
    StatisticsUnavailable,
    due_report_periods,
    generate_report_messages,
    mark_report_delivered,
    next_monthly_boundary,
    next_weekly_boundary,
)
from core.telegram_control import send_messages_blocking

DEFAULT_CHECK_INTERVAL_SECONDS = 30
MAX_BACKOFF_SECONDS = 15 * 60

_scheduler_lock = RLock()
_scheduler_thread = None
_scheduler_stop_event = None
_last_error = None
_retry_state = {}


def _period_key(period):
    return (period.report_type, period.period_end.isoformat(timespec="seconds"))


def _retry_delay(attempts):
    return min(60 * (2 ** max(0, attempts - 1)), MAX_BACKOFF_SECONDS)


def _retry_allowed(key, monotonic_now=None):
    retry = _retry_state.get(key)

    if retry is None:
        return True

    if monotonic_now is None:
        monotonic_now = time.monotonic()

    return monotonic_now >= retry.get("next_retry_at", 0)


def _record_retry(key, error, monotonic_now=None):
    if monotonic_now is None:
        monotonic_now = time.monotonic()

    retry = _retry_state.setdefault(
        key,
        {
            "attempts": 0,
            "next_retry_at": monotonic_now,
            "last_error": None,
        },
    )
    retry["attempts"] += 1
    retry["last_error"] = str(error)
    retry["next_retry_at"] = monotonic_now + _retry_delay(retry["attempts"])


def _clear_retry(key):
    _retry_state.pop(key, None)


def run_due_statistics_reports_once(
    now=None,
    sender=None,
    monotonic_now=None,
):
    sender = sender or send_messages_blocking
    report_now = now if now is not None else datetime.now(REPORT_TZ)
    sent = []

    for period in due_report_periods(now=report_now):
        key = _period_key(period)

        if not _retry_allowed(key, monotonic_now=monotonic_now):
            logger.info(
                "Statistics report retry deferred | "
                f"Type={period.report_type} PeriodEnd={period.period_end.isoformat()}"
            )
            continue

        try:
            messages = generate_report_messages(
                period.report_type,
                current=False,
                now=report_now,
            )
        except StatisticsUnavailable as exc:
            logger.error(
                "Statistics report generation unavailable | "
                f"Type={period.report_type} PeriodEnd={period.period_end.isoformat()} "
                f"Error={exc}"
            )
            _record_retry(key, exc, monotonic_now=monotonic_now)
            mark_statistics_scheduler_error(str(exc))
            continue
        except Exception as exc:
            logger.exception(
                "Statistics report generation failed | "
                f"Type={period.report_type} PeriodEnd={period.period_end.isoformat()}"
            )
            _record_retry(key, exc, monotonic_now=monotonic_now)
            mark_statistics_scheduler_error(str(exc))
            continue

        logger.info(
            "Statistics report delivery started | "
            f"Type={period.report_type} PeriodEnd={period.period_end.isoformat()} "
            f"Messages={len(messages)}"
        )

        try:
            delivered = sender(messages)
        except Exception as exc:
            delivered = False
            error = exc
        else:
            error = "Telegram delivery returned false"

        if not delivered:
            logger.error(
                "Statistics report delivery failed | "
                f"Type={period.report_type} PeriodEnd={period.period_end.isoformat()} "
                f"Error={error}"
            )
            _record_retry(key, error, monotonic_now=monotonic_now)
            mark_statistics_scheduler_error(str(error))
            continue

        mark_report_delivered(period.report_type, period.period_end)
        mark_statistics_scheduler_success(period.report_type, period.period_end)
        _clear_retry(key)
        sent.append(period)
        logger.info(
            "Statistics report delivery succeeded | "
            f"Type={period.report_type} PeriodEnd={period.period_end.isoformat()}"
        )

    return sent


def _scheduler_loop(check_interval):
    global _last_error

    logger.info("Statistics scheduler started")
    logger.info(
        "Statistics scheduler next boundaries | "
        f"Weekly={next_weekly_boundary().isoformat()} "
        f"Monthly={next_monthly_boundary().isoformat()}"
    )
    mark_statistics_scheduler_started()

    try:
        while not is_shutdown_requested():
            with _scheduler_lock:
                stop_event = _scheduler_stop_event

            if stop_event is not None and stop_event.is_set():
                break

            try:
                mark_statistics_scheduler_heartbeat()
                run_due_statistics_reports_once()
                _last_error = None
            except Exception as exc:
                _last_error = str(exc)
                logger.exception("Statistics scheduler loop error")
                mark_statistics_scheduler_error(str(exc))

            if stop_event is not None and stop_event.wait(check_interval):
                break

            if wait_for_shutdown(0):
                break
    finally:
        mark_statistics_scheduler_stopped()
        logger.info("Statistics scheduler stopped")


def start_statistics_scheduler(check_interval=DEFAULT_CHECK_INTERVAL_SECONDS):
    global _scheduler_thread
    global _scheduler_stop_event

    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread

        if is_shutdown_requested():
            return None

        _scheduler_stop_event = Event()
        _scheduler_thread = Thread(
            target=_scheduler_loop,
            args=(check_interval,),
            daemon=True,
            name="PrimeBot-StatisticsScheduler",
        )
        _scheduler_thread.start()
        return _scheduler_thread


def stop_statistics_scheduler(timeout=10):
    global _scheduler_thread

    with _scheduler_lock:
        thread = _scheduler_thread
        stop_event = _scheduler_stop_event

        if stop_event is not None:
            stop_event.set()

    if thread is not None and thread is not current_thread() and thread.is_alive():
        thread.join(timeout)

    return thread is None or not thread.is_alive()


def is_statistics_scheduler_running():
    with _scheduler_lock:
        return _scheduler_thread is not None and _scheduler_thread.is_alive()


def get_statistics_scheduler_status():
    with _scheduler_lock:
        return {
            "running": is_statistics_scheduler_running(),
            "last_error": _last_error,
            "thread_alive": (
                _scheduler_thread is not None
                and _scheduler_thread.is_alive()
            ),
        }


def reset_statistics_scheduler_for_tests():
    global _scheduler_thread
    global _scheduler_stop_event
    global _last_error

    stop_statistics_scheduler(timeout=1)

    with _scheduler_lock:
        _scheduler_thread = None
        _scheduler_stop_event = None
        _last_error = None
        _retry_state.clear()
