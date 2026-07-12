import asyncio
from threading import RLock
from datetime import datetime

from telethon import TelegramClient, events

from core.lifecycle import is_shutdown_requested
from core.signal_processor import (
    process_new_message,
    process_edited_message
)
from core.logger import logger
from core.runtime import (
    mark_listener_heartbeat,
    mark_listener_started,
    mark_listener_stopped,
)
from core.settings import load_settings

_client = None
_running = False
_last_error = None
_last_activity = None
_lock = RLock()


def _now():
    return datetime.now()


def _create_client(settings):

    client = TelegramClient(
        settings.session_name,
        settings.api_id,
        settings.api_hash
    )

    @client.on(events.NewMessage(chats=settings.channel_id))
    async def new_message_handler(event):
        _mark_activity()
        await process_new_message(event)

    @client.on(events.MessageEdited(chats=settings.channel_id))
    async def edited_message_handler(event):
        _mark_activity()
        await process_edited_message(event)

    return client


def _mark_activity():

    global _last_activity

    with _lock:
        _last_activity = _now()

    mark_listener_heartbeat()


def start_listener():

    global _client
    global _running
    global _last_error

    with _lock:
        if _running and _client is not None:
            return

    settings = load_settings(validate=True)

    logger.info("Connecting to Telegram...")

    client = _create_client(settings)

    with _lock:
        _client = client
        _last_error = None

    try:
        client.start()

        with _lock:
            _running = True

        logger.info("Telegram connected")

        mark_listener_started()
        _mark_activity()
        logger.info(f"Listening on channel {settings.channel_id}")

        if not is_shutdown_requested():
            client.run_until_disconnected()

    except Exception as exc:
        with _lock:
            _last_error = str(exc)
            _running = False
        raise
    finally:
        with _lock:
            _running = False
            if _client is client:
                _client = None
        mark_listener_stopped()


def stop_listener():

    global _running

    with _lock:
        client = _client

    if client is None:
        mark_listener_stopped()
        return True

    try:
        disconnected = client.disconnect()

        if hasattr(disconnected, "__await__"):
            loop = getattr(client, "loop", None)

            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(disconnected, loop)
                future.result(timeout=10)
            elif loop is not None:
                loop.run_until_complete(disconnected)

    except Exception as exc:
        logger.error(f"Telegram listener disconnect failed: {exc}")
        return False
    finally:
        with _lock:
            _running = False
        mark_listener_stopped()

    return True


def is_listener_running():

    with _lock:
        return _running


def get_listener_status():

    with _lock:
        return {
            "running": _running,
            "last_error": _last_error,
            "last_activity": _last_activity,
        }
