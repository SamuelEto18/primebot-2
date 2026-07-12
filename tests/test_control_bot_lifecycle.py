import asyncio
import importlib
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from core import runtime
from core.control_bot_lifecycle import ControlBotLifecycle


@contextmanager
def isolated_runtime_file(initial_state=None):
    original_runtime_file = runtime.RUNTIME_FILE

    with tempfile.TemporaryDirectory() as temp_dir:
        runtime.RUNTIME_FILE = os.path.join(temp_dir, "runtime.json")

        if initial_state is not None:
            with open(runtime.RUNTIME_FILE, "w", encoding="utf-8") as handle:
                json.dump(initial_state, handle)

        try:
            yield runtime.RUNTIME_FILE
        finally:
            runtime.RUNTIME_FILE = original_runtime_file


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def wait_until(predicate, timeout=2):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if predicate():
            return True

        time.sleep(0.01)

    return predicate()


class FakeLogger:

    def __init__(self):
        self.errors = []
        self.exceptions = []

    def error(self, message):
        self.errors.append(message)

    def exception(self, message):
        self.exceptions.append(message)

    def info(self, message):
        pass

    def warning(self, message):
        pass


class FakeUpdater:

    def __init__(self):
        self.polling_started = False
        self.polling_stopped = False

    async def start_polling(self):
        self.polling_started = True

    async def stop(self):
        self.polling_stopped = True


class FakeApplication:

    def __init__(self, fail_initialize=False):
        self.updater = FakeUpdater()
        self.fail_initialize = fail_initialize
        self.initialized = False
        self.started = False
        self.stopped = False
        self.shutdown_done = False

    async def initialize(self):
        if self.fail_initialize:
            raise RuntimeError("startup failed")
        self.initialized = True

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def shutdown(self):
        self.shutdown_done = True


class LifecycleTests(unittest.TestCase):

    def make_lifecycle(self, factory):
        self.events = []
        return ControlBotLifecycle(
            application_factory=factory,
            logger=FakeLogger(),
            on_started=lambda: self.events.append("started"),
            on_stopped=lambda: self.events.append("stopped"),
            on_error=lambda message: self.events.append(f"error:{message}"),
        )

    def test_successful_startup(self):
        apps = []
        lifecycle = self.make_lifecycle(lambda: apps.append(FakeApplication()) or apps[-1])

        self.assertTrue(lifecycle.start(timeout=2))
        self.assertTrue(lifecycle.is_running())
        self.assertEqual(self.events, ["started"])
        self.assertTrue(apps[0].updater.polling_started)

        self.assertTrue(lifecycle.stop(timeout=2))

    def test_startup_failure(self):
        lifecycle = self.make_lifecycle(lambda: FakeApplication(fail_initialize=True))

        self.assertFalse(lifecycle.start(timeout=2))
        self.assertFalse(lifecycle.is_running())
        self.assertIn("error:startup failed", self.events)

    def test_clean_shutdown(self):
        apps = []
        lifecycle = self.make_lifecycle(lambda: apps.append(FakeApplication()) or apps[-1])

        self.assertTrue(lifecycle.start(timeout=2))
        self.assertTrue(lifecycle.stop(timeout=2))

        app = apps[0]
        self.assertTrue(app.updater.polling_stopped)
        self.assertTrue(app.stopped)
        self.assertTrue(app.shutdown_done)
        self.assertIn("stopped", self.events)

    def test_heartbeat_advances_while_running(self):
        initial = runtime._default_state()
        initial["telegram_control_bot"] = False
        initial["telegram_control_bot_last_seen"] = None
        heartbeats = []

        def started():
            runtime.update_runtime(
                telegram_control_bot=True,
                telegram_control_bot_last_seen="2026-07-01 00:00:00",
                telegram_control_bot_error=None,
            )

        def heartbeat():
            value = f"2026-07-01 00:00:0{len(heartbeats) + 1}"
            runtime.update_runtime(
                telegram_control_bot=True,
                telegram_control_bot_last_seen=value,
            )
            heartbeats.append(value)

        with isolated_runtime_file(initial) as path:
            lifecycle = ControlBotLifecycle(
                application_factory=lambda: FakeApplication(),
                logger=FakeLogger(),
                on_started=started,
                on_stopped=runtime.mark_control_bot_stopped,
                on_error=runtime.mark_control_bot_error,
                on_heartbeat=heartbeat,
                heartbeat_interval=0.02,
            )

            self.assertTrue(lifecycle.start(timeout=2))
            self.assertTrue(wait_until(lambda: len(heartbeats) >= 2))
            state = read_json(path)

            self.assertTrue(lifecycle.stop(timeout=2))

        self.assertTrue(state["telegram_control_bot"])
        self.assertIn(state["telegram_control_bot_last_seen"], heartbeats[1:])

    def test_heartbeat_stops_during_shutdown(self):
        heartbeats = []
        lifecycle = ControlBotLifecycle(
            application_factory=lambda: FakeApplication(),
            logger=FakeLogger(),
            on_heartbeat=lambda: heartbeats.append(time.monotonic()),
            heartbeat_interval=0.02,
        )

        self.assertTrue(lifecycle.start(timeout=2))
        self.assertTrue(wait_until(lambda: len(heartbeats) >= 2))
        self.assertTrue(lifecycle.stop(timeout=2))

        count_after_stop = len(heartbeats)
        time.sleep(0.08)

        self.assertEqual(len(heartbeats), count_after_stop)

    def test_failed_lifecycle_records_unhealthy_error_state(self):
        with isolated_runtime_file() as path:
            lifecycle = ControlBotLifecycle(
                application_factory=lambda: FakeApplication(fail_initialize=True),
                logger=FakeLogger(),
                on_started=runtime.mark_control_bot_started,
                on_stopped=runtime.mark_control_bot_stopped,
                on_error=runtime.mark_control_bot_error,
                on_heartbeat=runtime.mark_control_bot_heartbeat,
                heartbeat_interval=0.02,
            )

            self.assertFalse(lifecycle.start(timeout=2))
            self.assertTrue(wait_until(
                lambda: not lifecycle.get_status()["thread_alive"]
            ))
            state = read_json(path)

        self.assertFalse(state["telegram_control_bot"])
        self.assertEqual(state["telegram_control_bot_error"], "startup failed")
        self.assertEqual(state["last_error"], "startup failed")

    def test_successful_restart(self):
        apps = []
        lifecycle = self.make_lifecycle(lambda: apps.append(FakeApplication()) or apps[-1])

        self.assertTrue(lifecycle.start(timeout=2))
        self.assertTrue(lifecycle.restart(timeout=2))
        self.assertTrue(lifecycle.is_running())
        self.assertEqual(len(apps), 2)

        self.assertTrue(lifecycle.stop(timeout=2))

    def test_failed_restart(self):
        apps = [FakeApplication(), FakeApplication(fail_initialize=True)]
        lifecycle = self.make_lifecycle(lambda: apps.pop(0))

        self.assertTrue(lifecycle.start(timeout=2))
        self.assertFalse(lifecycle.restart(timeout=2))
        self.assertFalse(lifecycle.is_running())
        self.assertIn("error:startup failed", self.events)

    def test_duplicate_start_prevention(self):
        apps = []
        lifecycle = self.make_lifecycle(lambda: apps.append(FakeApplication()) or apps[-1])

        self.assertTrue(lifecycle.start(timeout=2))
        self.assertTrue(lifecycle.start(timeout=2))
        self.assertEqual(len(apps), 1)

        self.assertTrue(lifecycle.stop(timeout=2))


class FakeUser:

    def __init__(self, user_id):
        self.id = user_id


class FakeQuery:

    def __init__(self):
        self.answered = False
        self.edited = False
        self.data = "status"

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, *args, **kwargs):
        self.edited = True


class FakeUpdate:

    def __init__(self):
        self.effective_user = FakeUser(999)
        self.callback_query = FakeQuery()


class FakeMessage:

    def __init__(self):
        self.replied = False
        self.text = None
        self.reply_markup = None

    async def reply_text(self, text, reply_markup=None):
        self.replied = True
        self.text = text
        self.reply_markup = reply_markup


class FakeCommandUpdate:

    def __init__(self, user_id=999):
        self.effective_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.callback_query = None


class CallbackSafetyTests(unittest.TestCase):

    def test_authorized_command_refreshes_last_seen(self):
        try:
            command_handler = importlib.import_module("core.command_handler")
        except ModuleNotFoundError as exc:
            self.skipTest(f"telegram dependency unavailable: {exc}")

        update = FakeCommandUpdate()

        with patch.object(
            command_handler,
            "load_settings",
            return_value=SimpleNamespace(admin_id=999),
        ), patch.object(
            command_handler,
            "mark_control_bot_heartbeat",
        ) as heartbeat:
            asyncio.run(command_handler._run_command(update, "ping"))

        heartbeat.assert_called_once()
        self.assertTrue(update.message.replied)

    def test_unauthorized_callback_is_answered_without_disclosure(self):
        os.environ.setdefault("BOT_TOKEN", "123:TEST")
        os.environ.setdefault("ADMIN_ID", "1")

        try:
            command_handler = importlib.import_module("core.command_handler")
        except ModuleNotFoundError as exc:
            self.skipTest(f"telegram dependency unavailable: {exc}")

        update = FakeUpdate()

        with patch.object(
            command_handler,
            "load_settings",
            return_value=SimpleNamespace(admin_id=1),
        ), patch.object(
            command_handler,
            "mark_control_bot_heartbeat",
        ) as heartbeat:
            asyncio.run(command_handler.keyboard_callback(update, None))

        self.assertTrue(update.callback_query.answered)
        self.assertFalse(update.callback_query.edited)
        heartbeat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
