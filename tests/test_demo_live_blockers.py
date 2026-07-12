import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from config import COMMENT, MAGIC_NUMBER
from core import control_center
from core import executor
from core import mt5_service
from core import position_manager
from core import signal_processor
from core import trade_storage
from core.parser import parse_signal
from tests.test_mt5_phase4b import (
    FakeMT5Phase4B,
    close_deal,
    open_deal,
    position,
    real_manual_close_deal,
    real_open_deal,
)


@contextmanager
def isolated_trade_files(active=None, pending=None, history=None):
    original = (
        trade_storage.DATA_FILE,
        trade_storage.PENDING_FILE,
        trade_storage.HISTORY_FILE,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        trade_storage.DATA_FILE = os.path.join(temp_dir, "active_trades.json")
        trade_storage.PENDING_FILE = os.path.join(
            temp_dir,
            "pending_position_identities.json",
        )
        trade_storage.HISTORY_FILE = os.path.join(temp_dir, "trade_history.json")

        for path, data in (
            (trade_storage.DATA_FILE, active),
            (trade_storage.PENDING_FILE, pending),
            (trade_storage.HISTORY_FILE, history),
        ):
            if data is not None:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)

        try:
            yield
        finally:
            (
                trade_storage.DATA_FILE,
                trade_storage.PENDING_FILE,
                trade_storage.HISTORY_FILE,
            ) = original


def signal(tps=None, raw=None):
    return SimpleNamespace(
        chat_id=1,
        message_id=1112,
        symbol="XAUUSD.s",
        side="BUY",
        textual_direction="BUY",
        inferred_direction="BUY",
        final_side="BUY",
        direction_source="sl_tp_geometry",
        direction_conflict=False,
        entry_low=None,
        entry_high=None,
        sl=4118,
        tps=tps or [4132, 4136, 4140, 4145],
        raw=raw or "XAUUSD BUY\nTP1 4132\nSL 4118",
    )


def success_result(ticket, identifier, tp, index):
    return {
        "success": True,
        "accepted": True,
        "accepted_identity_pending": False,
        "identity_status": "resolved",
        "identity_resolution": "opening_deal_position_id",
        "ticket": ticket,
        "position_ticket": ticket,
        "position_identifier": identifier,
        "position_id": identifier,
        "order_ticket": 1000 + index,
        "deal_ticket": 2000 + index,
        "symbol": "XAUUSD.s",
        "side": "BUY",
        "volume": 0.01,
        "requested_price": 4125.8,
        "fill_price": 4125.89,
        "price_open": 4125.89,
        "price": 4125.89,
        "entry_source": "position_price_open",
        "sl": 4118,
        "tp": tp,
        "magic": MAGIC_NUMBER,
        "comment": COMMENT,
        "order_attempt_started_at": "2026-07-02T18:00:00",
    }


def pending_result(tp, index=1):
    return {
        "success": False,
        "accepted": True,
        "accepted_identity_pending": True,
        "identity_status": "pending",
        "ticket": None,
        "position_ticket": None,
        "order_ticket": 1000 + index,
        "deal_ticket": 2000 + index,
        "symbol": "XAUUSD.s",
        "side": "BUY",
        "volume": 0.01,
        "requested_price": 4125.8,
        "fill_price": 4125.89,
        "price": 4125.89,
        "sl": 4118,
        "tp": tp,
        "magic": MAGIC_NUMBER,
        "comment": COMMENT,
        "retcode": 10009,
        "result_comment": "done",
        "selected_filling": 1,
        "filling_attempts": [1],
        "order_attempt_started_at": "2026-07-02T18:00:00",
    }


class MT5IdentityResolutionBlockerTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeMT5Phase4B()
        self.fake.info.point = 0.01
        self.fake.info.digits = 2
        mt5_service.set_mt5_api(self.fake)
        self.original_poll = mt5_service.OPEN_POSITION_IDENTITY_POLL_DELAYS
        mt5_service.OPEN_POSITION_IDENTITY_POLL_DELAYS = (0,)

    def tearDown(self):
        mt5_service.OPEN_POSITION_IDENTITY_POLL_DELAYS = self.original_poll

    def _open(self, tp, deal_position_id, order, deal):
        self.fake.results = [
            SimpleNamespace(
                retcode=self.fake.TRADE_RETCODE_DONE,
                order=order,
                deal=deal,
                price=100.3,
                comment="done",
            )
        ]
        self.fake.deals = [
            open_deal(
                ticket=deal,
                order=order,
                position_id=deal_position_id,
                price=100.3,
            )
        ]
        return mt5_service.open_trade("XAUUSD.s", "BUY", 99, tp)

    def test_four_same_symbol_positions_resolve_through_deal_position_id(self):
        self.fake.positions = [
            position(ticket=900, identifier=500, tp=110),
            position(ticket=901, identifier=501, tp=120),
            position(ticket=902, identifier=502, tp=130),
            position(ticket=903, identifier=503, tp=140),
        ]

        results = [
            self._open(tp, 500 + index, 111 + index, 222 + index)
            for index, tp in enumerate([110, 120, 130, 140])
        ]

        self.assertEqual([result["ticket"] for result in results], [900, 901, 902, 903])
        self.assertEqual(
            [result["identity_resolution"] for result in results],
            ["opening_deal_position_id"] * 4,
        )

    def test_fallback_matching_requires_unique_tp(self):
        self.fake.positions = [
            position(ticket=900, identifier=500, tp=110),
            position(ticket=901, identifier=501, tp=120),
        ]
        self.fake.deals = []

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 120)

        self.assertTrue(result["success"])
        self.assertEqual(result["ticket"], 901)
        self.assertEqual(result["identity_resolution"], "strict_fallback")

    def test_already_assigned_ticket_is_excluded(self):
        self.fake.positions = [
            position(ticket=900, identifier=500, tp=110),
            position(ticket=901, identifier=501, tp=110),
        ]
        self.fake.deals = []

        result = mt5_service.open_trade(
            "XAUUSD.s",
            "BUY",
            99,
            110,
            excluded_position_tickets={900},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["ticket"], 901)

    def test_wrong_magic_comment_side_and_old_positions_are_excluded(self):
        self.fake.positions = [
            position(ticket=1, identifier=1, magic=0),
            position(ticket=2, identifier=2, comment="manual"),
            position(
                ticket=3,
                identifier=3,
                position_type=self.fake.POSITION_TYPE_SELL,
            ),
            position(ticket=4, identifier=4, time=1000),
            position(ticket=900, identifier=500),
        ]
        self.fake.deals = [open_deal(position_id=500)]

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertEqual(result["ticket"], 900)

    def test_price_tolerance_uses_symbol_point(self):
        self.fake.info.point = 0.1
        self.fake.positions = [position(price_open=102.2)]
        self.fake.deals = []
        self.fake.results = [
            SimpleNamespace(
                retcode=self.fake.TRADE_RETCODE_DONE,
                order=111,
                deal=222,
                price=100.3,
                comment="done",
            )
        ]

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertEqual(result["ticket"], 900)

    def test_authoritative_opening_deal_position_id_ignores_requested_price_gap(self):
        self.fake.tick.ask = 4079.91
        self.fake.positions = [
            position(
                ticket=74493420,
                identifier=74493420,
                price_open=4080.14,
                sl=4072,
                tp=4086,
            )
        ]
        self.fake.deals = [
            open_deal(
                ticket=71289172,
                order=74493420,
                position_id=74493420,
                price=4080.14,
            )
        ]
        self.fake.results = [SimpleNamespace(
            retcode=self.fake.TRADE_RETCODE_DONE,
            order=74493420,
            deal=71289172,
            price=4079.91,
            comment="done",
        )]

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 4072, 4086)

        self.assertAlmostEqual(abs(4080.14 - 4079.91), 0.23)
        self.assertAlmostEqual(self.fake.info.point * 20, 0.20)
        self.assertTrue(result["success"])
        self.assertEqual(result["position_id"], 74493420)
        self.assertEqual(result["fill_price"], 4080.14)
        self.assertEqual(result["entry_source"], "opening_deal_price")
        self.assertEqual(result["magic"], MAGIC_NUMBER)
        self.assertEqual(result["comment"], COMMENT)

    def test_fallback_matching_keeps_requested_price_tolerance(self):
        self.fake.tick.ask = 4079.91
        self.fake.positions = [
            position(price_open=4080.14, sl=4072, tp=4086)
        ]
        self.fake.deals = []
        self.fake.results = [SimpleNamespace(
            retcode=self.fake.TRADE_RETCODE_DONE,
            order=74493420,
            deal=None,
            price=4079.91,
            comment="done",
        )]

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 4072, 4086)

        self.assertFalse(result["success"])
        self.assertTrue(result["accepted_identity_pending"])

    def test_delayed_deal_visibility_resolves(self):
        calls = {"history": 0}
        self.fake.positions = [
            position(ticket=900, identifier=500),
            position(ticket=901, identifier=501),
        ]

        def history(date_from, date_to, **kwargs):
            calls["history"] += 1
            if calls["history"] == 1:
                return tuple()
            return (open_deal(position_id=500),)

        self.fake.history_deals_get = history
        mt5_service.OPEN_POSITION_IDENTITY_POLL_DELAYS = (0, 0)

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertGreaterEqual(calls["history"], 2)

    def test_delayed_position_visibility_resolves(self):
        calls = {"positions": 0}
        visible_position = position(ticket=900, identifier=500)

        def positions_get(**kwargs):
            calls["positions"] += 1
            if calls["positions"] == 1:
                return tuple()
            return (visible_position,)

        self.fake.positions_get = positions_get
        self.fake.deals = [open_deal(position_id=500)]
        mt5_service.OPEN_POSITION_IDENTITY_POLL_DELAYS = (0, 0)

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertGreaterEqual(calls["positions"], 2)

    def test_accepted_unresolved_order_is_not_resent(self):
        self.fake.positions = []
        self.fake.deals = []

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertFalse(result["success"])
        self.assertTrue(result["accepted_identity_pending"])
        self.assertEqual(len(self.fake.sent_requests), 1)


class PendingIdentityRecoveryTests(unittest.TestCase):

    def setUp(self):
        position_manager._pending_identity_recovery_state.clear()

    def test_closed_incident_is_recovered_from_order_and_deal_history_idempotently(self):
        pending = pending_result(4086)
        pending.update({
            "chat_id": 1,
            "message_id": 3067,
            "tp_index": 1,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "volume": 0.01,
            "requested_price": 4079.91,
            "fill_price": 4079.91,
            "price": 4079.91,
            "sl": 4072,
            "tp": 4086,
            "order_ticket": 74493420,
            "deal_ticket": 71289172,
            "magic": MAGIC_NUMBER,
            "comment": COMMENT,
            "raw_message": "XAUUSD BUY SL 4072 TP 4086",
            "order_attempt_started_at": "2026-07-02T18:00:00",
        })
        active = [{
            "chat_id": 1,
            "message_id": 3067,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "sl": 4072,
            "positions": [],
            "raw_message": pending["raw_message"],
        }]
        fake = FakeMT5Phase4B()
        fake.info.point = 0.01
        fake.info.digits = 2
        fake.orders = [SimpleNamespace(
            ticket=74493420,
            position_id=74493420,
            symbol="XAUUSD.s",
            type=fake.ORDER_TYPE_BUY,
            volume_initial=0.01,
            magic=MAGIC_NUMBER,
            comment=COMMENT,
            sl=4072,
            tp=4086,
            time_done=1893456000,
        )]
        fake.positions = []
        fake.deals = [
            open_deal(
                ticket=71289172,
                order=74493420,
                position_id=74493420,
                price=4080.14,
                time=1893456001,
            ),
            close_deal(
                position_id=74493420,
                reason=fake.DEAL_REASON_TP,
                price=4086,
                volume=0.01,
                ticket=71291681,
                order=74495867,
                time=1893456010,
                profit=5.13,
            ),
        ]
        mt5_service.set_mt5_api(fake)

        with isolated_trade_files(active=active, pending=[pending], history=[]), \
            patch.object(position_manager, "notify_identity_recovered"), \
            patch.object(position_manager, "notify_signal_archived"):
            first = position_manager.recover_pending_identities_once()
            second = position_manager.recover_pending_identities_once()
            remaining = trade_storage.load_pending_identities()
            active_after = trade_storage.load_trades()
            history = trade_storage.load_trade_history()

        self.assertEqual(first["recovered"], 1)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(remaining, [])
        self.assertEqual(active_after, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(len(history[0]["positions"]), 1)
        recovered = history[0]["positions"][0]
        self.assertEqual(recovered["position_id"], 74493420)
        self.assertEqual(recovered["order_ticket"], 74493420)
        self.assertEqual(recovered["deal_ticket"], 71289172)
        self.assertEqual(recovered["fill_price"], 4080.14)
        self.assertEqual(recovered["close_deal_ticket"], 71291681)
        self.assertEqual(recovered["close_order_ticket"], 74495867)
        self.assertEqual(recovered["close_price"], 4086)
        self.assertEqual(recovered["close_volume"], 0.01)
        self.assertEqual(recovered["close_reason"], "take_profit")
        self.assertEqual(recovered["profit"], 5.13)
        self.assertEqual(recovered["close_profit"], 5.13)
        self.assertTrue(recovered["closed"])
        self.assertTrue(recovered["closed_at"])
        self.assertEqual(fake.order_history_calls[0]["kwargs"], {"ticket": 74493420})
        self.assertIn(
            {"args": (), "kwargs": {"position": 74493420}},
            fake.history_calls,
        )
        self.assertEqual(fake.sent_requests, [])

    def test_unresolved_recovery_uses_backoff_and_throttles_warnings(self):
        pending = pending_result(4132)
        pending.update({"chat_id": 1, "message_id": 4040, "tp_index": 1})
        unresolved_result = {
            "success": False,
            "comment": "broker history pending",
        }

        with isolated_trade_files(pending=[pending]), \
            patch.object(
                position_manager,
                "recover_pending_position_identity",
                return_value=unresolved_result,
            ) as recover, \
            patch.object(
                position_manager.time,
                "monotonic",
                side_effect=(100, 100, 101, 116, 116),
            ), \
            patch.object(position_manager.logger, "warning") as warning:
            first = position_manager.recover_pending_identities_once()
            second = position_manager.recover_pending_identities_once()
            third = position_manager.recover_pending_identities_once()
            remaining = trade_storage.load_pending_identities()

        self.assertEqual(first["skipped"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(third["skipped"], 0)
        self.assertEqual(recover.call_count, 2)
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(len(remaining), 1)

    def test_pending_cleanup_retry_does_not_duplicate_durably_stored_position(self):
        pending = pending_result(4132)
        pending.update({
            "chat_id": 1,
            "message_id": 5050,
            "tp_index": 1,
            "raw_message": "raw",
        })
        active = [{
            "chat_id": 1,
            "message_id": 5050,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "positions": [],
            "raw_message": "raw",
        }]
        recovered_result = success_result(900, 500, 4132, 1)
        original_write = trade_storage._atomic_write_json
        failed_once = {"value": False}

        def fail_first_pending_removal(data, path=None, expected_type=list):
            if (
                path == trade_storage.PENDING_FILE
                and data == []
                and not failed_once["value"]
            ):
                failed_once["value"] = True
                return False
            return original_write(data, path, expected_type=expected_type)

        with isolated_trade_files(active=active, pending=[pending]), \
            patch.object(
                position_manager,
                "recover_pending_position_identity",
                return_value=recovered_result,
            ) as recover, \
            patch.object(
                trade_storage,
                "_atomic_write_json",
                side_effect=fail_first_pending_removal,
            ), \
            patch.object(position_manager, "notify_identity_recovered"):
            first = position_manager.recover_pending_identities_once()
            position_manager._pending_identity_recovery_state.clear()
            second = position_manager.recover_pending_identities_once()
            trades = trade_storage.load_trades()
            remaining = trade_storage.load_pending_identities()

        self.assertEqual(first["unresolved"], 1)
        self.assertEqual(second["recovered"], 1)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(len(trades[0]["positions"]), 1)
        self.assertEqual(remaining, [])

    def test_accepted_unresolved_order_is_persisted_and_halts_later_tps(self):
        calls = []

        def fake_open(symbol, side, sl, tp, **kwargs):
            calls.append(tp)
            return pending_result(tp)

        with isolated_trade_files(), \
            patch.object(executor, "open_trade", side_effect=fake_open), \
            patch.object(executor, "notify_identity_pending"), \
            patch.object(executor, "notify_error") as notify_error, \
            patch.object(executor, "notify_success"), \
            patch.object(executor, "recover_pending_identities_once"):
            summary = executor.execute_signal(signal())
            pending = trade_storage.load_pending_identities()
            active = trade_storage.load_trades()

        self.assertEqual(calls, [4132])
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["accepted_identity_pending"], 1)
        self.assertEqual(summary["not_attempted"], 3)
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(active), 1)
        notify_error.assert_not_called()

    def test_pending_recovery_survives_restart_and_creates_no_duplicate(self):
        pending = pending_result(4132)
        pending.update({
            "chat_id": 1,
            "message_id": 1112,
            "tp_index": 1,
            "raw_message": "raw",
        })
        active = [signal().__dict__ | {"positions": [], "raw_message": "raw"}]
        fake = FakeMT5Phase4B()
        fake.info.point = 0.01
        fake.info.digits = 2
        fake.positions = [
            position(ticket=900, identifier=500, sl=4118, tp=4132, price_open=4125.89)
        ]
        fake.deals = [
            open_deal(
                ticket=2001,
                order=1001,
                position_id=500,
                price=4125.89,
            )
        ]
        mt5_service.set_mt5_api(fake)

        with isolated_trade_files(active=active, pending=[pending]), \
            patch.object(position_manager, "notify_identity_recovered"):
            first = position_manager.recover_pending_identities_once()
            second = position_manager.recover_pending_identities_once()
            trades = trade_storage.load_trades()
            remaining = trade_storage.load_pending_identities()

        self.assertEqual(first["recovered"], 1)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(len(remaining), 0)
        self.assertEqual(len(trades[0]["positions"]), 1)
        self.assertEqual(trades[0]["positions"][0]["ticket"], 900)

    def test_unresolved_pending_record_remains_pending(self):
        pending = pending_result(4132)
        pending.update({"chat_id": 1, "message_id": 1112, "tp_index": 1})
        fake = FakeMT5Phase4B()
        fake.positions = []
        fake.deals = []
        mt5_service.set_mt5_api(fake)

        with isolated_trade_files(pending=[pending]):
            summary = position_manager.recover_pending_identities_once()
            remaining = trade_storage.load_pending_identities()

        self.assertEqual(summary["recovered"], 0)
        self.assertEqual(len(remaining), 1)

    def test_pending_identity_prevents_premature_archival(self):
        trade = {
            "chat_id": 1,
            "message_id": 1112,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "positions": [
                {"ticket": 900, "closed": True, "tp": 4132},
            ],
        }
        pending = pending_result(4136, index=2)
        pending.update({"chat_id": 1, "message_id": 1112, "tp_index": 2})

        with isolated_trade_files(active=[trade], pending=[pending]):
            position_manager.process_trades_once([trade])
            self.assertEqual(len(trade_storage.load_trades()), 1)
            self.assertEqual(len(trade_storage.load_trade_history()), 0)


class ExecutionStorageBlockerTests(unittest.TestCase):

    def test_four_resolved_positions_are_all_stored_with_tp_indexes(self):
        def fake_open(symbol, side, sl, tp, **kwargs):
            index = [4132, 4136, 4140, 4145].index(tp) + 1
            return success_result(280608771 + index, 500 + index, tp, index)

        with isolated_trade_files(), \
            patch.object(executor, "open_trade", side_effect=fake_open), \
            patch.object(executor, "notify_success"), \
            patch.object(executor, "notify_error"):
            summary = executor.execute_signal(signal())
            saved = trade_storage.load_trades()[0]

        self.assertEqual(summary["opened"], 4)
        self.assertEqual(
            [position["ticket"] for position in saved["positions"]],
            [280608772, 280608773, 280608774, 280608775],
        )
        self.assertEqual(
            [position["tp_index"] for position in saved["positions"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [position["tp"] for position in saved["positions"]],
            [4132, 4136, 4140, 4145],
        )

    def test_sl_tp_only_signal_executes_without_entry_or_now(self):
        parsed = parse_signal(
            """
            XAUUSD BUY
            TP1 4132
            SL 4118
            """
        )
        parsed.chat_id = 1
        parsed.message_id = 1112

        with isolated_trade_files(), \
            patch.object(
                executor,
                "open_trade",
                return_value=success_result(900, 500, 4132.0, 1),
            ) as open_trade, \
            patch.object(executor, "notify_success"), \
            patch.object(executor, "notify_error"):
            summary = executor.execute_signal(parsed)

        self.assertEqual(summary["opened"], 1)
        open_trade.assert_called_once_with("XAUUSD.s", "BUY", 4118.0, 4132.0)

    def test_dry_run_sends_no_mt5_orders(self):
        event = SimpleNamespace(
            raw_text="XAUUSD BUY\nTP1 4132\nSL 4118",
            media=False,
            id=1112,
            chat_id=1,
        )

        async def run():
            await signal_processor.process_new_message(event)

        with patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(signal_processor, "is_processed", return_value=False), \
            patch.object(signal_processor, "is_auto_execute", return_value=False), \
            patch.object(signal_processor, "mark_signal_received"), \
            patch.object(signal_processor, "mark_processed"), \
            patch.object(signal_processor, "notify_signal"), \
            patch.object(signal_processor, "notify_dry_run"), \
            patch.object(signal_processor, "execute_signal") as execute_signal:
            import asyncio
            asyncio.run(run())

        execute_signal.assert_not_called()


class ClosureReconciliationBlockerTests(unittest.TestCase):

    def _single_trade(self, positions=None):
        return {
            "chat_id": 1,
            "message_id": 1112,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "positions": positions or [
                {
                    "ticket": 900,
                    "position_ticket": 900,
                    "position_identifier": 500,
                    "tp": 4132,
                    "volume": 0.01,
                    "entry": 4125.89,
                    "closed": False,
                    "break_even": False,
                }
            ],
        }

    def test_manual_final_close_archives_without_break_even(self):
        trade = self._single_trade()

        with isolated_trade_files(active=[trade]), \
            patch.object(
                position_manager,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_ABSENT,
                    ticket=900,
                ),
            ), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": True,
                    "pending": False,
                    "close_reason": "manual_close",
                    "reason": "manual_close",
                    "metadata": {
                        "close_reason": "manual_close",
                        "close_price": 4126,
                        "close_deal_ticket": 333,
                        "close_volume": 0.01,
                    },
                },
            ), \
            patch.object(position_manager, "modify_trade") as modify_trade, \
            patch.object(position_manager, "notify_position_closed"), \
            patch.object(position_manager, "notify_signal_archived"):
            position_manager.process_trades_once([trade])
            active = trade_storage.load_trades()
            history = trade_storage.load_trade_history()

        modify_trade.assert_not_called()
        self.assertEqual(active, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(
            history[0]["positions"][0]["close_reason"],
            "manual_close",
        )
        self.assertFalse(history[0]["positions"][0]["take_profit_confirmed"])

    def test_stop_loss_close_archives_without_break_even(self):
        trade = self._single_trade()

        with isolated_trade_files(active=[trade]), \
            patch.object(
                position_manager,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_ABSENT,
                    ticket=900,
                ),
            ), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": True,
                    "pending": False,
                    "close_reason": "stop_loss",
                    "reason": "stop_loss",
                    "metadata": {"close_reason": "stop_loss"},
                },
            ), \
            patch.object(position_manager, "modify_trade") as modify_trade, \
            patch.object(position_manager, "notify_position_closed"), \
            patch.object(position_manager, "notify_signal_archived"):
            position_manager.process_trades_once([trade])
            history = trade_storage.load_trade_history()

        modify_trade.assert_not_called()
        self.assertEqual(len(history), 1)

    def test_delayed_history_preserves_active_trade_until_confirmed(self):
        trade = self._single_trade()

        with isolated_trade_files(active=[trade]), \
            patch.object(
                position_manager,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_ABSENT,
                    ticket=900,
                ),
            ), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": False,
                    "pending": True,
                    "reason": "No close deal found",
                    "metadata": {},
                },
            ):
            position_manager.process_trades_once([trade])
            active = trade_storage.load_trades()

        self.assertFalse(trade["positions"][0]["closed"])
        self.assertEqual(len(active), 1)

    def test_partial_closure_keeps_remaining_positions_active(self):
        trade = self._single_trade([
            {
                "ticket": 900,
                "position_ticket": 900,
                "position_identifier": 500,
                "tp_index": 1,
                "tp": 4132,
                "volume": 0.01,
                "closed": False,
                "break_even": False,
            },
            {
                "ticket": 901,
                "position_ticket": 901,
                "position_identifier": 501,
                "tp_index": 2,
                "tp": 4136,
                "volume": 0.01,
                "closed": False,
                "break_even": False,
            },
        ])

        def query(ticket):
            if ticket == 900:
                return mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_ABSENT,
                    ticket=ticket,
                )
            return mt5_service.PositionQueryResult(
                status=mt5_service.POSITION_OPEN,
                ticket=ticket,
                position=SimpleNamespace(price_open=4125.89),
            )

        with isolated_trade_files(active=[trade]), \
            patch.object(position_manager, "query_position", side_effect=query), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": True,
                    "pending": False,
                    "close_reason": "manual_close",
                    "reason": "manual_close",
                    "metadata": {"close_reason": "manual_close"},
                },
            ), \
            patch.object(position_manager, "notify_position_closed"):
            position_manager.process_trades_once([trade])
            active = trade_storage.load_trades()
            history = trade_storage.load_trade_history()

        self.assertEqual(len(active), 1)
        self.assertEqual(len(history), 0)
        self.assertTrue(active[0]["positions"][0]["closed"])
        self.assertFalse(active[0]["positions"][1]["closed"])

    def test_final_closure_archives_once(self):
        trade = self._single_trade()

        with isolated_trade_files(active=[trade]), \
            patch.object(
                position_manager,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_ABSENT,
                    ticket=900,
                ),
            ), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": True,
                    "pending": False,
                    "close_reason": "manual_close",
                    "reason": "manual_close",
                    "metadata": {"close_reason": "manual_close"},
                },
            ), \
            patch.object(position_manager, "notify_position_closed"), \
            patch.object(position_manager, "notify_signal_archived"):
            position_manager.process_trades_once([trade])
            position_manager.process_trades_once([trade])
            history = trade_storage.load_trade_history()

        self.assertEqual(len(history), 1)

    def test_legacy_stale_record_archives_after_manual_close(self):
        trade = self._single_trade([
            {
                "ticket": 280608772,
                "tp": 4132,
                "entry": 4125.89,
                "closed": False,
                "break_even": False,
            }
        ])

        with isolated_trade_files(active=[trade]), \
            patch.object(
                position_manager,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_ABSENT,
                    ticket=280608772,
                ),
            ), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": True,
                    "pending": False,
                    "close_reason": "manual_close",
                    "reason": "manual_close",
                    "metadata": {"close_reason": "manual_close"},
                },
            ), \
            patch.object(position_manager, "notify_position_closed"), \
            patch.object(position_manager, "notify_signal_archived"):
            position_manager.run_startup_recovery()
            active = trade_storage.load_trades()
            history = trade_storage.load_trade_history()

        self.assertEqual(active, [])
        self.assertEqual(len(history), 1)

    def test_real_legacy_manual_close_reconciles_from_position_history(self):
        fake = FakeMT5Phase4B()
        fake.positions = []
        fake.deals = [real_open_deal(), real_manual_close_deal()]
        mt5_service.set_mt5_api(fake)
        trade = self._single_trade([
            {
                "ticket": 280608772,
                "tp": 4132,
                "entry": 4125.89,
                "volume": 0.01,
                "closed": False,
                "break_even": False,
            }
        ])

        with isolated_trade_files(active=[trade], pending=[]), \
            patch.object(position_manager, "notify_position_closed"), \
            patch.object(position_manager, "notify_signal_archived"), \
            patch.object(position_manager, "modify_trade") as modify_trade:
            position_manager.run_startup_recovery()
            position_manager.run_startup_recovery()
            active = trade_storage.load_trades()
            history = trade_storage.load_trade_history()

        modify_trade.assert_not_called()
        self.assertEqual(active, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["message_id"], 1112)
        archived_position = history[0]["positions"][0]
        self.assertTrue(archived_position["closed"])
        self.assertEqual(archived_position["close_reason"], "manual_close")
        self.assertFalse(archived_position["take_profit_confirmed"])
        self.assertEqual(archived_position["close_deal_ticket"], 214491640)
        self.assertEqual(archived_position["close_order_ticket"], 280619893)
        self.assertEqual(archived_position["close_price"], 4124.50)
        self.assertEqual(archived_position["close_volume"], 0.01)
        self.assertTrue(all(not call["args"] for call in fake.history_calls))

    def test_manual_tp1_close_does_not_trigger_break_even(self):
        fake = FakeMT5Phase4B()
        fake.positions = [
            position(
                ticket=280608773,
                identifier=280608773,
                price_open=4125.89,
                sl=4118,
                tp=4136,
            )
        ]
        fake.deals = [real_open_deal(), real_manual_close_deal()]
        mt5_service.set_mt5_api(fake)
        trade = self._single_trade([
            {
                "ticket": 280608772,
                "position_ticket": 280608772,
                "position_identifier": 280608772,
                "tp_index": 1,
                "tp": 4132,
                "entry": 4125.89,
                "volume": 0.01,
                "closed": False,
                "break_even": False,
            },
            {
                "ticket": 280608773,
                "position_ticket": 280608773,
                "position_identifier": 280608773,
                "tp_index": 2,
                "tp": 4136,
                "entry": 4125.89,
                "volume": 0.01,
                "closed": False,
                "break_even": False,
            },
        ])

        with isolated_trade_files(active=[trade], pending=[]), \
            patch.object(position_manager, "notify_position_closed"), \
            patch.object(position_manager, "modify_trade") as modify_trade:
            position_manager.process_trades_once([trade])
            active = trade_storage.load_trades()
            history = trade_storage.load_trade_history()

        modify_trade.assert_not_called()
        self.assertEqual(len(history), 0)
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["positions"][0]["closed"])
        self.assertFalse(active[0]["positions"][0]["take_profit_confirmed"])
        self.assertFalse(active[0]["positions"][1]["break_even"])

    def test_partial_live_volume_remains_active(self):
        fake = FakeMT5Phase4B()
        fake.positions = [
            position(
                ticket=900,
                identifier=500,
                volume=0.005,
            )
        ]
        fake.deals = [
            open_deal(position_id=500, time=100),
            close_deal(
                position_id=500,
                reason=fake.DEAL_REASON_CLIENT,
                volume=0.005,
                time=101,
            ),
        ]
        mt5_service.set_mt5_api(fake)
        trade = self._single_trade([
            {
                "ticket": 900,
                "position_ticket": 900,
                "position_identifier": 500,
                "tp": 4132,
                "volume": 0.01,
                "closed": False,
                "break_even": False,
            }
        ])

        with isolated_trade_files(active=[trade], pending=[]):
            position_manager.process_trades_once([trade])
            active = trade_storage.load_trades()
            history = trade_storage.load_trade_history()

        self.assertEqual(len(history), 0)
        self.assertEqual(len(active), 1)
        self.assertFalse(active[0]["positions"][0]["closed"])
        self.assertEqual(active[0]["positions"][0]["original_volume"], 0.01)
        self.assertEqual(active[0]["positions"][0]["volume"], 0.005)
        self.assertEqual(active[0]["positions"][0]["remaining_volume"], 0.005)

    def test_status_counters_return_to_zero_after_archive(self):
        with isolated_trade_files(active=[], pending=[]), \
            patch.object(control_center, "get_runtime", return_value={
                "version": "test",
                "auto_execute": True,
                "paused": False,
                "started": "2026-07-02 18:00:00",
            }), \
            patch.object(control_center, "get_account_summary", return_value={
                "connected": True,
                "account_number": 1,
                "server": "demo",
                "broker": "broker",
            }), \
            patch.object(control_center, "get_health", return_value={
                "Listener": True,
                "Telegram": True,
                "MT5": True,
                "Storage": True,
                "Runtime": True,
                "Position Manager": True,
            }):
            status = control_center.format_status()

        self.assertIn("Open Trades: 0", status)
        self.assertIn("Active Signals: 0", status)

    def test_pending_identity_counts_as_active_signal_not_open_trade(self):
        pending = pending_result(4132)
        pending.update({"chat_id": 1, "message_id": 1112, "tp_index": 1})

        with isolated_trade_files(active=[], pending=[pending]), \
            patch.object(control_center, "get_runtime", return_value={
                "version": "test",
                "auto_execute": True,
                "paused": False,
                "started": "2026-07-02 18:00:00",
            }), \
            patch.object(control_center, "get_account_summary", return_value={
                "connected": True,
                "account_number": 1,
                "server": "demo",
                "broker": "broker",
            }), \
            patch.object(control_center, "get_health", return_value={
                "Listener": True,
                "Telegram": True,
                "MT5": True,
                "Storage": True,
                "Runtime": True,
                "Position Manager": True,
            }):
            status = control_center.format_status()

        self.assertIn("Open Trades: 0", status)
        self.assertIn("Active Signals: 1", status)


if __name__ == "__main__":
    unittest.main()
