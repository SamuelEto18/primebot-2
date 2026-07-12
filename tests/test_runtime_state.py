import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from core import runtime


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


def runtime_temp_files(path):
    directory = os.path.dirname(path)

    return [
        name for name in os.listdir(directory)
        if name.startswith("runtime-") and name.endswith(".tmp")
    ]


class RuntimeConcurrencyTests(unittest.TestCase):

    def test_many_concurrent_runtime_mutations_leave_valid_json(self):
        with isolated_runtime_file() as path:
            def worker():
                for _ in range(10):
                    runtime.mark_signal_received()

            threads = [threading.Thread(target=worker) for _ in range(8)]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

            state = read_json(path)

        self.assertEqual(state["signals_today"], 80)
        self.assertIn("last_signal_time", state)

    def test_concurrent_counter_and_heartbeat_writes_do_not_lose_fields(self):
        initial = runtime._default_state()
        initial["paused"] = True
        initial["auto_execute"] = True

        with isolated_runtime_file(initial) as path:
            def count_worker():
                for _ in range(15):
                    runtime.increment_signal()

            def heartbeat_worker():
                for _ in range(15):
                    runtime.mark_watchdog_heartbeat()

            threads = [
                threading.Thread(target=count_worker),
                threading.Thread(target=heartbeat_worker),
                threading.Thread(target=count_worker),
                threading.Thread(target=heartbeat_worker),
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

            state = read_json(path)

        self.assertEqual(state["signals_today"], 30)
        self.assertTrue(state["watchdog"])
        self.assertIsNotNone(state["watchdog_last_seen"])
        self.assertTrue(state["paused"])
        self.assertTrue(state["auto_execute"])

    def test_simulated_transient_permission_error_succeeds_after_retry(self):
        with isolated_runtime_file() as path:
            real_replace = os.replace
            calls = []

            def flaky_replace(source, destination):
                calls.append((source, destination))

                if len(calls) == 1:
                    raise PermissionError("Access is denied")

                return real_replace(source, destination)

            with patch.object(runtime.os, "replace", side_effect=flaky_replace), \
                    patch.object(runtime, "_REPLACE_RETRY_DELAYS", (0,)), \
                    patch.object(runtime.time, "sleep") as sleep:
                self.assertTrue(runtime.update_runtime(paused=True))

            state = read_json(path)
            temps = runtime_temp_files(path)

        self.assertTrue(state["paused"])
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(0)
        self.assertEqual(temps, [])

    def test_persistent_replacement_failure_preserves_previous_file(self):
        initial = runtime._default_state()
        initial["paused"] = False

        with isolated_runtime_file(initial) as path:
            with patch.object(
                runtime.os,
                "replace",
                side_effect=PermissionError("Access is denied"),
            ), patch.object(runtime, "_REPLACE_RETRY_DELAYS", (0,)):
                self.assertFalse(runtime.update_runtime(paused=True))

            state = read_json(path)
            temps = runtime_temp_files(path)

        self.assertFalse(state["paused"])
        self.assertEqual(temps, [])

    def test_paused_and_auto_execute_survive_unrelated_updates(self):
        initial = runtime._default_state()
        initial["paused"] = True
        initial["auto_execute"] = True

        with isolated_runtime_file(initial) as path:
            runtime.mark_listener_heartbeat()
            state = read_json(path)

        self.assertTrue(state["paused"])
        self.assertTrue(state["auto_execute"])
        self.assertTrue(state["telegram_listener"])


class DailyStatisticsRolloverTests(unittest.TestCase):

    def test_same_date_restart_preserves_counters(self):
        initial = runtime._default_state()
        initial.update({
            "stats_date": "2026-06-30",
            "signals_today": 3,
            "executed_today": 2,
            "errors_today": 1,
        })

        with isolated_runtime_file(initial) as path, \
                patch.object(runtime, "_today", return_value="2026-06-30"):
            runtime.initialize_process_runtime()
            state = read_json(path)

        self.assertEqual(state["signals_today"], 3)
        self.assertEqual(state["executed_today"], 2)
        self.assertEqual(state["errors_today"], 1)

    def test_next_date_access_resets_daily_counters(self):
        initial = runtime._default_state()
        initial.update({
            "stats_date": "2026-06-29",
            "signals_today": 3,
            "executed_today": 2,
            "errors_today": 1,
        })

        with isolated_runtime_file(initial) as path, \
                patch.object(runtime, "_today", return_value="2026-06-30"):
            state = runtime.get_runtime()
            persisted = read_json(path)

        self.assertEqual(state["signals_today"], 0)
        self.assertEqual(state["executed_today"], 0)
        self.assertEqual(state["errors_today"], 0)
        self.assertEqual(state["stats_date"], "2026-06-30")
        self.assertEqual(persisted["stats_date"], "2026-06-30")

    def test_pause_live_and_service_state_survive_rollover(self):
        initial = runtime._default_state()
        initial.update({
            "stats_date": "2026-06-29",
            "signals_today": 3,
            "executed_today": 2,
            "errors_today": 1,
            "paused": True,
            "auto_execute": True,
            "telegram_listener": True,
            "telegram_listener_last_seen": "2026-06-29 23:59:58",
            "last_signal_time": "2026-06-29 23:59:59",
            "last_trade_time": "2026-06-29 23:59:59",
        })

        with isolated_runtime_file(initial), \
                patch.object(runtime, "_today", return_value="2026-06-30"):
            state = runtime.get_runtime()

        self.assertTrue(state["paused"])
        self.assertTrue(state["auto_execute"])
        self.assertTrue(state["telegram_listener"])
        self.assertEqual(
            state["telegram_listener_last_seen"],
            "2026-06-29 23:59:58",
        )
        self.assertEqual(state["last_signal_time"], "2026-06-29 23:59:59")
        self.assertEqual(state["last_trade_time"], "2026-06-29 23:59:59")

    def test_old_runtime_file_without_stats_date_migrates_safely(self):
        initial = runtime._default_state()
        initial.pop("stats_date")
        initial["signals_today"] = 4
        initial["executed_today"] = 3
        initial["errors_today"] = 2

        with isolated_runtime_file(initial) as path, \
                patch.object(runtime, "_today", return_value="2026-06-30"):
            state = runtime.get_runtime()
            persisted = read_json(path)

        self.assertEqual(state["stats_date"], "2026-06-30")
        self.assertEqual(persisted["stats_date"], "2026-06-30")
        self.assertEqual(state["signals_today"], 4)
        self.assertEqual(state["executed_today"], 3)
        self.assertEqual(state["errors_today"], 2)

    def test_concurrent_rollover_and_counter_increments_remain_valid(self):
        initial = runtime._default_state()
        initial.update({
            "stats_date": "2026-06-29",
            "signals_today": 99,
            "executed_today": 88,
            "errors_today": 77,
        })

        with isolated_runtime_file(initial) as path, \
                patch.object(runtime, "_today", return_value="2026-06-30"):
            threads = [
                threading.Thread(target=runtime.mark_signal_received)
                for _ in range(20)
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

            state = read_json(path)

        self.assertEqual(state["stats_date"], "2026-06-30")
        self.assertEqual(state["signals_today"], 20)
        self.assertEqual(state["executed_today"], 0)
        self.assertEqual(state["errors_today"], 0)


if __name__ == "__main__":
    unittest.main()
