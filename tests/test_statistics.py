import asyncio
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from config import COMMENT, MAGIC_NUMBER
from core import command_handler
from core import lifecycle
from core import mt5_service
from core import statistics
from core import statistics_scheduler
from core import trade_storage
from tests.test_mt5_phase4b import FakeMT5Phase4B, close_deal, open_deal, position

TZ = statistics.REPORT_TZ


@contextmanager
def isolated_statistics_files(active=None, pending=None, history=None, state=None):
    original = (
        trade_storage.DATA_FILE,
        trade_storage.PENDING_FILE,
        trade_storage.HISTORY_FILE,
        statistics.REPORT_STATE_FILE,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        trade_storage.DATA_FILE = os.path.join(temp_dir, "active_trades.json")
        trade_storage.PENDING_FILE = os.path.join(
            temp_dir,
            "pending_position_identities.json",
        )
        trade_storage.HISTORY_FILE = os.path.join(temp_dir, "trade_history.json")
        statistics.REPORT_STATE_FILE = os.path.join(
            temp_dir,
            "statistics_report_state.json",
        )

        for path, data in (
            (trade_storage.DATA_FILE, active),
            (trade_storage.PENDING_FILE, pending),
            (trade_storage.HISTORY_FILE, history),
            (statistics.REPORT_STATE_FILE, state),
        ):
            if data is not None:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)

        try:
            yield statistics.REPORT_STATE_FILE
        finally:
            (
                trade_storage.DATA_FILE,
                trade_storage.PENDING_FILE,
                trade_storage.HISTORY_FILE,
                statistics.REPORT_STATE_FILE,
            ) = original


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def local_time(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def epoch(value):
    return int(value.astimezone(timezone.utc).timestamp())


def cashflow(deal, profit=0.0, commission=0.0, swap=0.0, fee=0.0):
    deal.profit = profit
    deal.commission = commission
    deal.swap = swap
    deal.fee = fee
    return deal


def trade(chat_id, message_id, positions, symbol="XAUUSD.s"):
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "symbol": symbol,
        "side": "BUY",
        "positions": positions,
    }


def stored_position(ticket, identifier, tp, **updates):
    item = {
        "ticket": ticket,
        "position_ticket": ticket,
        "position_identifier": identifier,
        "position_id": identifier,
        "order_ticket": ticket,
        "deal_ticket": ticket + 1000,
        "symbol": "XAUUSD.s",
        "side": "BUY",
        "volume": 0.01,
        "entry": 100.0,
        "sl": 95.0,
        "tp": tp,
        "magic": MAGIC_NUMBER,
        "comment": COMMENT,
        "closed": False,
        "break_even": False,
        "order_attempt_started_at": local_time(2026, 6, 30, 10).isoformat(),
    }
    item.update(updates)
    return item


class FakeMT5Statistics(FakeMT5Phase4B):
    def __init__(self):
        super().__init__()
        self.account = SimpleNamespace(
            login=123456,
            server="Demo-Server",
            company="PU Prime",
            balance=10000.0,
            equity=10003.5,
            currency="EUR",
        )

    def account_info(self):
        return self.account


class SchedulingTests(unittest.TestCase):
    def test_weekly_boundaries(self):
        friday = local_time(2026, 7, 3, 23, 59)
        saturday = local_time(2026, 7, 4, 0, 0)
        previous = local_time(2026, 6, 27, 0, 0)
        state = {
            "weekly": {"last_successful_period_end": previous.isoformat()},
            "monthly": {"last_successful_period_end": local_time(2026, 7, 1).isoformat()},
        }

        self.assertEqual(
            statistics.due_report_periods(friday, state=state),
            [],
        )

        due = statistics.due_report_periods(saturday, state=state)
        weekly = [period for period in due if period.report_type == "weekly"][0]

        self.assertEqual(weekly.period_end, saturday)
        self.assertEqual(weekly.period_start, local_time(2026, 6, 29))

    def test_monthly_boundaries_include_leap_and_month_lengths(self):
        leap = statistics.completed_period("monthly", local_time(2024, 3, 1))
        april = statistics.completed_period("monthly", local_time(2026, 5, 1))
        july = statistics.completed_period("monthly", local_time(2026, 8, 1))

        self.assertEqual(leap.period_start, local_time(2024, 2, 1))
        self.assertEqual((leap.period_end - leap.period_start).days, 29)
        self.assertEqual((april.period_end - april.period_start).days, 30)
        self.assertEqual((july.period_end - july.period_start).days, 31)

    def test_last_calendar_day_before_midnight_is_not_monthly_due(self):
        state = {
            "weekly": {"last_successful_period_end": local_time(2026, 7, 25).isoformat()},
            "monthly": {"last_successful_period_end": local_time(2026, 7, 1).isoformat()},
        }

        due = statistics.due_report_periods(
            local_time(2026, 7, 31, 23, 59),
            state=state,
        )

        self.assertEqual(due, [])

    def test_vienna_dst_transition_uses_zoneinfo_offsets(self):
        before = statistics.next_weekly_boundary(local_time(2026, 3, 27, 12))
        after = statistics.next_weekly_boundary(local_time(2026, 4, 3, 12))

        self.assertEqual(before, local_time(2026, 3, 28))
        self.assertEqual(after, local_time(2026, 4, 4))
        self.assertNotEqual(before.utcoffset(), after.utcoffset())

    def test_weekly_and_monthly_can_both_be_due(self):
        state = {
            "weekly": {"last_successful_period_end": local_time(2026, 7, 25).isoformat()},
            "monthly": {"last_successful_period_end": local_time(2026, 7, 1).isoformat()},
        }

        due = statistics.due_report_periods(local_time(2026, 8, 1), state=state)

        self.assertEqual(
            [period.report_type for period in due],
            ["weekly", "monthly"],
        )


class ReportStateAndSchedulerTests(unittest.TestCase):
    def setUp(self):
        statistics_scheduler.reset_statistics_scheduler_for_tests()

    def tearDown(self):
        statistics_scheduler.reset_statistics_scheduler_for_tests()
        lifecycle.reset_lifecycle()

    def test_duplicate_schedule_checks_send_only_once_after_restart_catchup(self):
        state = {
            "weekly": {"last_successful_period_end": local_time(2026, 6, 27).isoformat()},
            "monthly": {"last_successful_period_end": local_time(2026, 7, 1).isoformat()},
        }
        sent = []

        with isolated_statistics_files(state=state) as state_path, \
                patch.object(
                    statistics_scheduler,
                    "generate_report_messages",
                    return_value=["weekly"],
                ), patch.object(
                    statistics_scheduler,
                    "mark_statistics_scheduler_success",
                ), patch.object(
                    statistics_scheduler,
                    "mark_statistics_scheduler_error",
                ):
            statistics_scheduler.run_due_statistics_reports_once(
                now=local_time(2026, 7, 4),
                sender=lambda messages: sent.append(messages) or True,
                monotonic_now=0,
            )
            statistics_scheduler.run_due_statistics_reports_once(
                now=local_time(2026, 7, 4),
                sender=lambda messages: sent.append(messages) or True,
                monotonic_now=1,
            )
            saved = read_json(state_path)

        self.assertEqual(sent, [["weekly"]])
        self.assertEqual(
            saved["weekly"]["last_successful_period_end"],
            local_time(2026, 7, 4).isoformat(),
        )

    def test_failed_delivery_does_not_mark_sent_and_successful_retry_marks_sent(self):
        state = {
            "weekly": {"last_successful_period_end": local_time(2026, 6, 27).isoformat()},
            "monthly": {"last_successful_period_end": local_time(2026, 7, 1).isoformat()},
        }

        with isolated_statistics_files(state=state) as state_path, \
                patch.object(
                    statistics_scheduler,
                    "generate_report_messages",
                    return_value=["weekly"],
                ), patch.object(
                    statistics_scheduler,
                    "mark_statistics_scheduler_success",
                ), patch.object(
                    statistics_scheduler,
                    "mark_statistics_scheduler_error",
                ):
            first = statistics_scheduler.run_due_statistics_reports_once(
                now=local_time(2026, 7, 4),
                sender=lambda _messages: False,
                monotonic_now=0,
            )
            after_failure = read_json(state_path)
            second = statistics_scheduler.run_due_statistics_reports_once(
                now=local_time(2026, 7, 4),
                sender=lambda _messages: True,
                monotonic_now=61,
            )
            after_success = read_json(state_path)

        self.assertEqual(first, [])
        self.assertEqual(
            after_failure["weekly"]["last_successful_period_end"],
            local_time(2026, 6, 27).isoformat(),
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(
            after_success["weekly"]["last_successful_period_end"],
            local_time(2026, 7, 4).isoformat(),
        )

    def test_scheduler_thread_duplicate_start_stop_and_exception_survival(self):
        lifecycle.reset_lifecycle()
        lifecycle.mark_running()
        calls = {"count": 0}

        def flaky_run_once():
            calls["count"] += 1

            if calls["count"] == 1:
                raise RuntimeError("boom")

            return []

        with patch.object(
            statistics_scheduler,
            "run_due_statistics_reports_once",
            side_effect=flaky_run_once,
        ), patch.object(statistics_scheduler, "mark_statistics_scheduler_started"), \
                patch.object(statistics_scheduler, "mark_statistics_scheduler_heartbeat"), \
                patch.object(statistics_scheduler, "mark_statistics_scheduler_error"), \
                patch.object(statistics_scheduler, "mark_statistics_scheduler_stopped"):
            first = statistics_scheduler.start_statistics_scheduler(check_interval=0.01)
            second = statistics_scheduler.start_statistics_scheduler(check_interval=0.01)
            time.sleep(0.05)

            self.assertIs(first, second)
            self.assertTrue(statistics_scheduler.is_statistics_scheduler_running())
            self.assertTrue(statistics_scheduler.stop_statistics_scheduler(timeout=2))


class StatisticsCalculationTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMT5Statistics()
        mt5_service.set_mt5_api(self.fake)

    def _primebot_fixture(self):
        open_time = local_time(2026, 6, 30, 10)
        close_one = local_time(2026, 7, 1, 12)
        close_two = local_time(2026, 7, 1, 13)
        close_three = local_time(2026, 7, 1, 14)
        close_four = local_time(2026, 7, 1, 15)
        close_five = local_time(2026, 7, 1, 16)

        self.fake.deals = [
            open_deal(ticket=1900, order=900, position_id=500, time=epoch(open_time)),
            cashflow(
                close_deal(
                    position_id=500,
                    reason=self.fake.DEAL_REASON_TP,
                    ticket=2900,
                    order=3900,
                    time=epoch(close_one),
                    profit=10.0,
                ),
                profit=10.0,
                commission=-1.0,
                swap=-0.2,
                fee=-0.1,
            ),
            open_deal(ticket=1901, order=901, position_id=501, time=epoch(open_time)),
            cashflow(
                close_deal(
                    position_id=501,
                    reason=self.fake.DEAL_REASON_CLIENT,
                    price=120.0,
                    ticket=2901,
                    order=3901,
                    time=epoch(close_two),
                    profit=-5.0,
                ),
                profit=-5.0,
                commission=-1.0,
            ),
            open_deal(ticket=1902, order=902, position_id=502, time=epoch(open_time)),
            cashflow(
                close_deal(
                    position_id=502,
                    reason=self.fake.DEAL_REASON_SL,
                    price=100.02,
                    ticket=2902,
                    order=3902,
                    time=epoch(close_four),
                    profit=-0.03,
                ),
                profit=-0.03,
            ),
            open_deal(ticket=1903, order=903, position_id=503, time=epoch(open_time)),
            open_deal(ticket=1904, order=904, position_id=504, time=epoch(open_time)),
            cashflow(
                close_deal(
                    position_id=504,
                    reason=self.fake.DEAL_REASON_CLIENT,
                    volume=0.004,
                    ticket=2904,
                    order=3904,
                    time=epoch(close_three),
                    profit=2.0,
                ),
                profit=2.0,
                commission=-0.5,
            ),
            cashflow(
                close_deal(
                    position_id=504,
                    reason=self.fake.DEAL_REASON_TP,
                    volume=0.006,
                    ticket=2905,
                    order=3905,
                    time=epoch(close_three),
                    profit=3.0,
                ),
                profit=3.0,
                commission=-0.5,
            ),
            open_deal(ticket=1905, order=905, position_id=505, time=epoch(open_time)),
            cashflow(
                close_deal(
                    position_id=505,
                    reason=self.fake.DEAL_REASON_SL,
                    price=150.0,
                    ticket=2906,
                    order=3906,
                    time=epoch(close_five),
                    profit=-2.0,
                ),
                profit=-2.0,
            ),
            open_deal(
                ticket=1999,
                order=999,
                position_id=999,
                time=epoch(open_time),
                magic=0,
                comment="manual",
            ),
            cashflow(
                close_deal(
                    position_id=999,
                    ticket=2999,
                    order=3999,
                    time=epoch(close_one),
                    profit=99.0,
                ),
                profit=99.0,
            ),
        ]

        open_live = position(ticket=903, identifier=503)
        open_live.profit = 3.5
        manual_live = position(ticket=999, identifier=999, magic=0, comment="manual")
        manual_live.profit = 99.0
        self.fake.positions = [open_live, manual_live]

        active = [
            trade(1, 12, [
                stored_position(903, 503, 130.0),
            ]),
        ]
        history = [
            trade(1, 10, [
                stored_position(900, 500, 110.0, closed=True),
                stored_position(901, 501, 120.0, closed=True),
            ]),
            trade(1, 13, [
                stored_position(904, 504, 140.0, closed=True),
            ]),
            trade(1, 14, [
                stored_position(902, 502, 125.0, closed=True, break_even=True),
            ]),
            trade(1, 15, [
                stored_position(905, 505, 150.0, closed=True),
            ]),
        ]
        return active, history

    def test_statistics_use_primebot_identity_and_authoritative_deal_cashflow(self):
        active, history = self._primebot_fixture()

        with isolated_statistics_files(active=active, pending=[], history=history):
            report = statistics.build_statistics_report(
                "weekly",
                now=local_time(2026, 7, 4),
            )

        metrics = report.metrics

        self.assertEqual(metrics["executed_signals"], 5)
        self.assertEqual(metrics["completed_signals"], 4)
        self.assertEqual(metrics["open_signals_from_period"], 1)
        self.assertEqual(metrics["positions_opened"], 6)
        self.assertEqual(metrics["positions_closed"], 5)
        self.assertEqual(metrics["positions_still_open"], 1)
        self.assertEqual(metrics["tp_closes"], 2)
        self.assertEqual(metrics["sl_closes"], 1)
        self.assertEqual(metrics["break_even_closes"], 1)
        self.assertEqual(metrics["manual_closes"], 1)
        self.assertEqual(metrics["profitable_positions"], 2)
        self.assertEqual(metrics["losing_positions"], 3)
        self.assertAlmostEqual(metrics["gross_profit"], 15.0)
        self.assertAlmostEqual(metrics["gross_loss"], -7.03)
        self.assertAlmostEqual(metrics["realized_profit"], 7.97)
        self.assertAlmostEqual(metrics["commissions"], -3.0)
        self.assertAlmostEqual(metrics["swaps"], -0.2)
        self.assertAlmostEqual(metrics["fees"], -0.1)
        self.assertAlmostEqual(metrics["net_realized"], 4.67)
        self.assertAlmostEqual(metrics["floating_pl"], 3.5)
        self.assertAlmostEqual(metrics["profit_factor"], 15.0 / 7.03)
        self.assertEqual(metrics["max_consecutive_profitable_completed_signals"], 2)
        self.assertEqual(metrics["max_consecutive_losing_completed_signals"], 2)
        self.assertAlmostEqual(metrics["closed_result_drawdown"], -2.03)
        self.assertEqual(metrics["best_signal"].label, "XAUUSD.s 1/13")
        self.assertEqual(metrics["worst_signal"].label, "XAUUSD.s 1/15")
        self.assertEqual(report.open_groups[0]["positions"], 1)
        self.assertAlmostEqual(report.symbol_rows[0]["net_realized"], 4.67)
        self.assertEqual(report.symbol_rows[0]["completed_signals"], 4)
        self.assertEqual(report.symbol_rows[0]["closed_positions"], 5)

    def test_empty_period_produces_valid_zero_activity_report(self):
        self.fake.positions = []
        self.fake.deals = []

        with isolated_statistics_files(active=[], pending=[], history=[]):
            report = statistics.build_statistics_report(
                "weekly",
                now=local_time(2026, 7, 4),
            )
            text = statistics.format_statistics_report(report)

        self.assertEqual(report.metrics["executed_signals"], 0)
        self.assertEqual(report.metrics["net_realized"], 0.0)
        self.assertIn("No activity", text)

    def test_manual_preview_does_not_update_automatic_state(self):
        self.fake.positions = []
        self.fake.deals = []
        state = {
            "weekly": {"last_successful_period_end": local_time(2026, 6, 27).isoformat()},
            "monthly": {"last_successful_period_end": local_time(2026, 7, 1).isoformat()},
        }

        with isolated_statistics_files(active=[], pending=[], history=[], state=state) as path:
            messages = statistics.generate_report_messages(
                "weekly",
                current=True,
                now=local_time(2026, 7, 2),
            )
            saved = read_json(path)

        self.assertIn("INCOMPLETE PERIOD PREVIEW", messages[0])
        self.assertEqual(saved, state)

    def test_telegram_message_splitting_respects_limit(self):
        text = "\n".join(f"line {index} " + ("x" * 80) for index in range(80))
        messages = statistics.split_telegram_message(text, limit=500)

        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 500 for message in messages))
        self.assertTrue(messages[0].startswith("line 0"))


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeUpdate:
    def __init__(self, user_id):
        self.effective_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.callback_query = None


class StatisticsCommandTests(unittest.TestCase):
    def test_weeklystats_is_authorization_protected(self):
        update = FakeUpdate(user_id=111)
        context = SimpleNamespace(args=[])

        with patch.object(
            command_handler,
            "load_settings",
            return_value=SimpleNamespace(admin_id=999),
        ), patch.object(command_handler, "generate_report_messages") as generate:
            asyncio.run(command_handler.weeklystats(update, context))

        generate.assert_not_called()
        self.assertEqual(update.message.replies, [])

    def test_monthlystats_current_sends_preview_without_trading_side_effects(self):
        update = FakeUpdate(user_id=999)
        context = SimpleNamespace(args=["current"])

        with patch.object(
            command_handler,
            "load_settings",
            return_value=SimpleNamespace(admin_id=999),
        ), patch.object(
            command_handler,
            "generate_report_messages",
            return_value=["INCOMPLETE PERIOD PREVIEW"],
        ) as generate, patch.object(command_handler, "pause_bot") as pause, \
                patch.object(command_handler, "resume_bot") as resume, \
                patch.object(command_handler, "set_auto_execute") as auto_execute:
            asyncio.run(command_handler.monthlystats(update, context))

        generate.assert_called_once_with("monthly", current=True)
        pause.assert_not_called()
        resume.assert_not_called()
        auto_execute.assert_not_called()
        self.assertEqual(update.message.replies[0][0], "INCOMPLETE PERIOD PREVIEW")


if __name__ == "__main__":
    unittest.main()
