import asyncio
import time
from threading import Event, RLock, Thread

from telegram import Bot

from core.lifecycle import is_shutdown_requested
from core.logger import logger
from core.settings import load_settings

_loop = None
_loop_thread = None
_bot = None
_stopping = False
_lock = RLock()
_loop_ready = Event()
_pending_futures = set()


async def _send(message):

    settings = load_settings(validate=True)
    bot = _get_bot(settings.bot_token)

    await bot.send_message(
        chat_id=settings.admin_id,
        text=message
    )


def _get_bot(token):

    global _bot

    if _bot is None:
        _bot = Bot(token)

    return _bot


def _run_loop(loop):

    asyncio.set_event_loop(loop)
    _loop_ready.set()
    loop.run_forever()


def _get_loop():

    global _loop
    global _loop_thread
    global _stopping

    with _lock:
        if _stopping or is_shutdown_requested():
            return None

        if _loop is not None and _loop.is_running():
            return _loop

        _loop_ready.clear()
        _loop = asyncio.new_event_loop()
        _loop_thread = Thread(
            target=_run_loop,
            args=(_loop,),
            daemon=True,
            name="PrimeBot-Notifications"
        )
        _loop_thread.start()
        _loop_ready.wait(5)

        return _loop


def _log_notification_result(future):

    with _lock:
        _pending_futures.discard(future)

    try:
        future.result()
    except Exception as exc:
        logger.error(f"Telegram notification failed: {exc}")


def send(message):

    try:
        if is_shutdown_requested():
            return

        loop = _get_loop()

        if loop is None:
            return

        future = asyncio.run_coroutine_threadsafe(
            _send(message),
            loop
        )

        with _lock:
            _pending_futures.add(future)

        future.add_done_callback(_log_notification_result)
    except Exception as exc:
        logger.error(f"Telegram notification scheduling failed: {exc}")


def send_blocking(message, timeout=30):
    try:
        if is_shutdown_requested():
            return False

        loop = _get_loop()

        if loop is None:
            return False

        future = asyncio.run_coroutine_threadsafe(
            _send(message),
            loop
        )

        with _lock:
            _pending_futures.add(future)

        try:
            future.result(timeout=timeout)
            return True
        except Exception as exc:
            logger.error(f"Telegram notification failed: {exc}")
            return False
        finally:
            with _lock:
                _pending_futures.discard(future)

    except Exception as exc:
        logger.error(f"Telegram notification scheduling failed: {exc}")
        return False


def send_messages_blocking(messages, timeout=30):
    for message in messages:
        if not send_blocking(message, timeout=timeout):
            return False

    return True


def stop_notifications(timeout=5):

    global _loop
    global _loop_thread
    global _bot
    global _stopping

    with _lock:
        _stopping = True
        loop = _loop
        thread = _loop_thread
        pending = list(_pending_futures)

    deadline = time.time() + timeout

    for future in pending:
        remaining = max(0, deadline - time.time())

        if remaining <= 0:
            break

        try:
            future.result(timeout=remaining)
        except Exception as exc:
            logger.error(f"Telegram notification drain failed: {exc}")

    if loop is not None and loop.is_running():
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception as exc:
            logger.error(f"Telegram notification loop stop failed: {exc}")

    if thread is not None and thread.is_alive():
        thread.join(max(0, deadline - time.time()))

    with _lock:
        if loop is not None and not loop.is_closed():
            try:
                loop.close()
            except Exception as exc:
                logger.error(f"Telegram notification loop close failed: {exc}")

        _loop = None
        _loop_thread = None
        _bot = None
        _pending_futures.clear()

    return True


def reset_notifications_for_tests():

    global _stopping

    stop_notifications(timeout=1)

    with _lock:
        _stopping = False
