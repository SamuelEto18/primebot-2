import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core import lifecycle


class MainLifecycleTests(unittest.TestCase):

    def setUp(self):
        lifecycle.reset_lifecycle()

    def tearDown(self):
        lifecycle.reset_lifecycle()

    def test_normal_startup_lifecycle_cleans_up(self):
        import main

        events = []

        patches = [
            patch.object(
                main,
                "validate_settings",
                side_effect=lambda: events.append("validate"),
            ),
            patch.object(
                main,
                "initialize_process_runtime",
                side_effect=lambda: events.append("runtime"),
            ),
            patch.object(
                main,
                "mark_running",
                side_effect=lambda: events.append("running"),
            ),
            patch.object(
                main,
                "notify_start",
                side_effect=lambda: events.append("notify"),
            ),
            patch.object(
                main,
                "_initialize_mt5_at_startup",
                side_effect=lambda: events.append("mt5"),
            ),
            patch.object(
                main,
                "start_command_bot",
                side_effect=lambda: events.append("control"),
            ),
            patch.object(
                main,
                "start_watchdog",
                side_effect=lambda *args: events.append("watchdog"),
            ),
            patch.object(main, "wait_for_shutdown", return_value=True),
            patch.object(
                main,
                "_cleanup",
                side_effect=lambda: events.append("cleanup"),
            ),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4],             patches[5], patches[6], patches[7], patches[8]:
            main.main()

        self.assertEqual(
            events,
            [
                "validate",
                "runtime",
                "running",
                "notify",
                "mt5",
                "control",
                "watchdog",
                "cleanup",
            ]
        )

    def test_partial_startup_failure_is_cleaned_up(self):
        import main

        events = []

        patches = [
            patch.object(main, "validate_settings"),
            patch.object(main, "initialize_process_runtime"),
            patch.object(main, "mark_running"),
            patch.object(main, "notify_start"),
            patch.object(main, "_initialize_mt5_at_startup"),
            patch.object(
                main,
                "start_command_bot",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(
                main,
                "notify_error",
                side_effect=lambda message: events.append("error"),
            ),
            patch.object(
                main,
                "_cleanup",
                side_effect=lambda: events.append("cleanup"),
            ),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4],             patches[5], patches[6], patches[7]:
            main.main()

        self.assertEqual(events, ["error", "cleanup"])

    def test_keyboard_interrupt_cleanup(self):
        import main

        events = []

        patches = [
            patch.object(main, "validate_settings"),
            patch.object(main, "initialize_process_runtime"),
            patch.object(main, "mark_running"),
            patch.object(main, "notify_start"),
            patch.object(main, "_initialize_mt5_at_startup"),
            patch.object(main, "start_command_bot"),
            patch.object(main, "start_watchdog"),
            patch.object(main, "wait_for_shutdown", side_effect=KeyboardInterrupt),
            patch.object(main, "notify_error"),
            patch.object(
                main,
                "_cleanup",
                side_effect=lambda: events.append("cleanup"),
            ),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4],             patches[5], patches[6], patches[7], patches[8] as notify_error,             patches[9]:
            main.main()

        notify_error.assert_not_called()
        self.assertEqual(events, ["cleanup"])

    def test_repeated_shutdown_calls_are_safe(self):
        import main

        calls = []

        patches = [
            patch.object(
                main,
                "stop_watchdog",
                side_effect=lambda: calls.append("watchdog"),
            ),
            patch.object(
                main,
                "stop_command_bot",
                side_effect=lambda: calls.append("control"),
            ),
            patch.object(
                main,
                "stop_listener",
                side_effect=lambda: calls.append("listener"),
            ),
            patch.object(
                main,
                "_join_service_thread",
                side_effect=lambda name: calls.append(name),
            ),
            patch.object(
                main,
                "stop_notifications",
                side_effect=lambda: calls.append("notifications"),
            ),
            patch.object(
                main,
                "shutdown_mt5",
                side_effect=lambda: calls.append("mt5"),
            ),
            patch.object(
                main,
                "mark_runtime_shutdown_complete",
                side_effect=lambda: calls.append("runtime"),
            ),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4],             patches[5], patches[6]:
            main._cleanup()
            main._cleanup()

        self.assertTrue(lifecycle.is_shutdown_completed())
        self.assertEqual(calls.count("watchdog"), 2)


class ServiceShutdownTests(unittest.TestCase):

    def setUp(self):
        lifecycle.reset_lifecycle()

    def tearDown(self):
        lifecycle.reset_lifecycle()

    def test_watchdog_does_not_restart_during_shutdown(self):
        from core import watchdog

        called = []
        lifecycle.request_shutdown()

        watchdog._restart_dead_service(
            "Example",
            lambda: called.append("target"),
            lambda message: called.append(message),
        )

        self.assertEqual(called, [])

    def test_position_manager_loop_exits_on_shutdown(self):
        from core import position_manager

        lifecycle.request_shutdown()

        with patch.object(position_manager, "mark_position_manager_started")             as started, patch.object(position_manager, "mark_position_manager_stopped")             as stopped, patch.object(position_manager, "load_trades")             as load_trades:
            position_manager.monitor_positions()

        started.assert_called_once()
        stopped.assert_called_once()
        load_trades.assert_not_called()

    def test_cleanup_stops_control_listener_and_notifications(self):
        import main

        calls = []

        patches = [
            patch.object(
                main,
                "stop_watchdog",
                side_effect=lambda: calls.append("watchdog"),
            ),
            patch.object(
                main,
                "stop_command_bot",
                side_effect=lambda: calls.append("control"),
            ),
            patch.object(
                main,
                "stop_listener",
                side_effect=lambda: calls.append("listener"),
            ),
            patch.object(
                main,
                "_join_service_thread",
                side_effect=lambda name: calls.append(name),
            ),
            patch.object(
                main,
                "stop_notifications",
                side_effect=lambda: calls.append("notifications"),
            ),
            patch.object(
                main,
                "shutdown_mt5",
                side_effect=lambda: calls.append("mt5"),
            ),
            patch.object(
                main,
                "mark_runtime_shutdown_complete",
                side_effect=lambda: calls.append("runtime"),
            ),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4],             patches[5], patches[6]:
            main._cleanup()

        self.assertIn("control", calls)
        self.assertIn("listener", calls)
        self.assertIn("notifications", calls)

    def test_notification_loop_stops_repeatedly(self):
        from core import telegram_control

        telegram_control.reset_notifications_for_tests()
        self.assertTrue(telegram_control.stop_notifications())
        self.assertTrue(telegram_control.stop_notifications())


class RuntimeStartupStateTests(unittest.TestCase):

    def test_admin_state_survives_and_started_resets(self):
        from core import runtime

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_file = os.path.join(temp_dir, "runtime.json")
            with open(runtime_file, "w", encoding="utf-8") as handle:
                json.dump({
                    "paused": True,
                    "auto_execute": True,
                    "started": "2000-01-01 00:00:00",
                }, handle)

            original = runtime.RUNTIME_FILE
            runtime.RUNTIME_FILE = runtime_file

            try:
                runtime.initialize_process_runtime()
                state = runtime.get_runtime()
            finally:
                runtime.RUNTIME_FILE = original

        self.assertTrue(state["paused"])
        self.assertTrue(state["auto_execute"])
        self.assertNotEqual(state["started"], "2000-01-01 00:00:00")
        self.assertFalse(state["telegram_listener"])
        self.assertFalse(state["telegram_control_bot"])
        self.assertFalse(state["position_manager"])


if __name__ == "__main__":
    unittest.main()
