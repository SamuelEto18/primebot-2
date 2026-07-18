import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import COMMENT, DEVIATION, MAGIC_NUMBER
from core import break_even, break_even_storage, mt5_service, position_manager


def symbol_info(digits=2, tick_size=0.01):
    return SimpleNamespace(
        digits=digits,
        point=10 ** -digits,
        trade_tick_size=tick_size,
        trade_stops_level=10,
        trade_freeze_level=20,
    )


def position(
    ticket=101,
    identifier=501,
    symbol="XAUUSD.s",
    magic=MAGIC_NUMBER,
    comment=COMMENT,
    side=0,
    entry=100.0,
    sl=0.0,
    tp=120.0,
):
    return SimpleNamespace(
        ticket=ticket,
        identifier=identifier,
        symbol=symbol,
        magic=magic,
        comment=comment,
        type=side,
        price_open=entry,
        sl=sl,
        tp=tp,
        volume=0.01,
    )


class ProfitableBreakEvenPriceTests(unittest.TestCase):

    def test_buy_sl_is_actual_open_price_plus_one(self):
        self.assertEqual(
            break_even.profitable_break_even_price(
                "XAUUSD.s",
                "BUY",
                100.0,
                info=symbol_info(),
            ),
            101.0,
        )

    def test_sell_sl_is_actual_open_price_minus_one(self):
        self.assertEqual(
            break_even.profitable_break_even_price(
                "XAUUSD.s",
                "SELL",
                100.0,
                info=symbol_info(),
            ),
            99.0,
        )

    def test_buy_and_sell_prices_are_rounded_to_tick_size(self):
        info = symbol_info(tick_size=0.05)
        self.assertEqual(
            break_even.profitable_break_even_price(
                "XAUUSD.s", "BUY", 100.023, info=info
            ),
            101.0,
        )
        self.assertEqual(
            break_even.profitable_break_even_price(
                "XAUUSD.s", "SELL", 100.027, info=info
            ),
            99.05,
        )

    def test_buy_better_stop_is_not_moved_backwards(self):
        item = position(sl=102.0)

        with patch.object(break_even, "position_type_buy", return_value=0), \
            patch.object(break_even, "position_type_sell", return_value=1), \
            patch.object(break_even, "symbol_info", return_value=symbol_info()), \
            patch.object(
                break_even,
                "modify_trade",
                return_value={
                    "success": True,
                    "noop": True,
                    "requested_sl": 101.0,
                    "current_sl": 102.0,
                    "side": "BUY",
                },
            ) as modify_trade:
            result = break_even.apply_profitable_break_even(item)

        self.assertEqual(result["status"], break_even.STATUS_ALREADY_PROTECTED)
        modify_trade.assert_called_once()

    def test_sell_better_stop_is_not_moved_backwards(self):
        item = position(side=1, sl=98.0, tp=80.0)

        with patch.object(break_even, "position_type_buy", return_value=0), \
            patch.object(break_even, "position_type_sell", return_value=1), \
            patch.object(break_even, "symbol_info", return_value=symbol_info()), \
            patch.object(
                break_even,
                "modify_trade",
                return_value={
                    "success": True,
                    "noop": True,
                    "requested_sl": 99.0,
                    "current_sl": 98.0,
                    "side": "SELL",
                },
            ) as modify_trade:
            result = break_even.apply_profitable_break_even(item)

        self.assertEqual(result["status"], break_even.STATUS_ALREADY_PROTECTED)
        modify_trade.assert_called_once()

    def test_live_break_even_uses_open_price_reread_under_mt5_lock(self):
        stale = position(entry=100.0, sl=90.0)
        live = position(entry=101.23, sl=90.0, tp=120.0)
        sent = []
        fake = SimpleNamespace(
            TRADE_ACTION_SLTP=6,
            TRADE_RETCODE_DONE=10009,
            POSITION_TYPE_BUY=0,
            POSITION_TYPE_SELL=1,
        )
        fake.positions_get = Mock(return_value=[live])
        fake.account_info = Mock(return_value=SimpleNamespace(login=123456))
        fake.symbol_info = Mock(return_value=symbol_info())
        fake.symbol_info_tick = Mock(
            return_value=SimpleNamespace(bid=110.0, ask=110.1)
        )
        fake.order_send = Mock(
            side_effect=lambda request: (
                sent.append(dict(request))
                or SimpleNamespace(retcode=10009, comment="done")
            )
        )

        with patch.object(mt5_service, "mt5", fake), \
            patch.object(break_even, "position_type_buy", return_value=0), \
            patch.object(break_even, "position_type_sell", return_value=1), \
            patch.object(break_even, "symbol_info", return_value=symbol_info()):
            result = break_even.apply_profitable_break_even(
                stale,
                expected_account_login=123456,
                expected_identifier=501,
            )

        self.assertEqual(result["status"], break_even.STATUS_MOVED)
        self.assertEqual(result["target_sl"], 102.23)
        self.assertEqual(sent[0]["sl"], 102.23)
        self.assertEqual(sent[0]["tp"], 120.0)

    def test_invalid_protected_stop_is_reported_without_fallback(self):
        live = position(entry=100.0, sl=90.0, tp=120.0)
        fake = SimpleNamespace(
            TRADE_ACTION_SLTP=6,
            TRADE_RETCODE_DONE=10009,
            POSITION_TYPE_BUY=0,
            POSITION_TYPE_SELL=1,
        )
        fake.positions_get = Mock(return_value=[live])
        fake.account_info = Mock(return_value=SimpleNamespace(login=123456))
        fake.symbol_info = Mock(return_value=symbol_info())
        fake.symbol_info_tick = Mock(
            return_value=SimpleNamespace(bid=101.05, ask=101.06)
        )
        fake.order_send = Mock()

        with patch.object(mt5_service, "mt5", fake), \
            patch.object(break_even, "position_type_buy", return_value=0), \
            patch.object(break_even, "position_type_sell", return_value=1), \
            patch.object(break_even, "symbol_info", return_value=symbol_info()):
            result = break_even.apply_profitable_break_even(
                live,
                expected_account_login=123456,
                expected_identifier=501,
            )

        self.assertEqual(result["status"], break_even.STATUS_PENDING)
        self.assertIn("stop/freeze distance", result["reason"])
        fake.order_send.assert_not_called()

    def test_manual_wrong_magic_comment_and_symbol_are_ignored(self):
        items = (
            position(magic=0, comment="manual"),
            position(magic=123),
            position(comment="PrimeBot1"),
            position(symbol="BTCUSD"),
        )

        for item in items:
            with self.subTest(item=item):
                self.assertEqual(
                    break_even.apply_profitable_break_even(item)["status"],
                    break_even.STATUS_IGNORED,
                )

    def test_tp_is_preserved_and_account_is_rechecked_at_mt5_boundary(self):
        live = position(tp=120.0)
        sent = []
        fake = SimpleNamespace(
            TRADE_ACTION_SLTP=6,
            TRADE_RETCODE_DONE=10009,
            POSITION_TYPE_BUY=0,
            POSITION_TYPE_SELL=1,
        )
        fake.positions_get = Mock(return_value=[live])
        fake.account_info = Mock(return_value=SimpleNamespace(login=123456))
        fake.symbol_info = Mock(return_value=symbol_info())
        fake.order_send = Mock(
            side_effect=lambda request: (
                sent.append(dict(request))
                or SimpleNamespace(retcode=10009, comment="done")
            )
        )

        with patch.object(mt5_service, "mt5", fake):
            result = mt5_service.modify_trade(
                live.ticket,
                sl=101.0,
                expected_symbol="XAUUSD.s",
                expected_magic=MAGIC_NUMBER,
                expected_comment=COMMENT,
                expected_account_login=123456,
                expected_identifier=501,
            )
            blocked = mt5_service.modify_trade(
                live.ticket,
                sl=102.0,
                expected_symbol="XAUUSD.s",
                expected_magic=MAGIC_NUMBER,
                expected_comment=COMMENT,
                expected_account_login=999999,
                expected_identifier=501,
            )

        self.assertTrue(result["success"])
        self.assertEqual(sent[0]["tp"], 120.0)
        self.assertFalse(blocked["success"])
        self.assertTrue(blocked["ownership_mismatch"])
        self.assertEqual(fake.order_send.call_count, 1)

    def test_close_trade_rechecks_magic_and_position_identity(self):
        live = position(magic=0, comment="manual")
        fake = SimpleNamespace()
        fake.account_info = Mock(return_value=SimpleNamespace(login=123456))
        fake.positions_get = Mock(return_value=[live])
        fake.symbol_info = Mock()
        fake.symbol_info_tick = Mock()
        fake.order_send = Mock()

        with patch.object(mt5_service, "mt5", fake):
            result = mt5_service.close_trade(
                live.ticket,
                expected_symbol="XAUUSD.s",
                expected_magic=MAGIC_NUMBER,
                expected_comment=COMMENT,
                expected_account_login=123456,
                expected_identifier=501,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["ownership_mismatch"])
        fake.symbol_info.assert_not_called()
        fake.order_send.assert_not_called()

    def test_close_trade_uses_safe_opposite_side_full_volume_request(self):
        live = position(side=0)
        fake = SimpleNamespace(
            POSITION_TYPE_BUY=0,
            POSITION_TYPE_SELL=1,
            ORDER_TYPE_BUY=0,
            ORDER_TYPE_SELL=1,
            TRADE_ACTION_DEAL=1,
            ORDER_TIME_GTC=0,
            TRADE_RETCODE_DONE=10009,
        )
        fake.account_info = Mock(return_value=SimpleNamespace(login=123456))
        fake.positions_get = Mock(return_value=[live])
        fake.symbol_info = Mock(return_value=symbol_info())
        fake.symbol_info_tick = Mock(
            return_value=SimpleNamespace(bid=109.5, ask=109.6)
        )
        sent = []

        def send(request, info):
            sent.append(dict(request))
            return SimpleNamespace(retcode=10009, comment="done"), 1, []

        with patch.object(mt5_service, "mt5", fake), patch.object(
            mt5_service,
            "_send_order_with_filling_retry",
            side_effect=send,
        ):
            result = mt5_service.close_trade(
                live.ticket,
                expected_symbol=live.symbol,
                expected_magic=MAGIC_NUMBER,
                expected_comment=COMMENT,
                expected_account_login=123456,
                expected_identifier=live.identifier,
            )

        self.assertTrue(result["success"])
        self.assertEqual(sent[0]["type"], fake.ORDER_TYPE_SELL)
        self.assertEqual(sent[0]["price"], 109.5)
        self.assertEqual(sent[0]["volume"], live.volume)
        self.assertEqual(sent[0]["deviation"], DEVIATION)
        self.assertEqual(sent[0]["position"], live.ticket)
        self.assertEqual(sent[0]["magic"], MAGIC_NUMBER)
        self.assertEqual(sent[0]["comment"], COMMENT)

    def test_partial_close_retcode_remains_retryable_failure(self):
        live = position(side=1)
        fake = SimpleNamespace(
            POSITION_TYPE_BUY=0,
            POSITION_TYPE_SELL=1,
            ORDER_TYPE_BUY=0,
            ORDER_TYPE_SELL=1,
            TRADE_ACTION_DEAL=1,
            ORDER_TIME_GTC=0,
            TRADE_RETCODE_DONE=10009,
            TRADE_RETCODE_DONE_PARTIAL=10010,
        )
        fake.account_info = Mock(return_value=SimpleNamespace(login=123456))
        fake.positions_get = Mock(return_value=[live])
        fake.symbol_info = Mock(return_value=symbol_info())
        fake.symbol_info_tick = Mock(
            return_value=SimpleNamespace(bid=90.0, ask=90.1)
        )

        with patch.object(mt5_service, "mt5", fake), patch.object(
            mt5_service,
            "_send_order_with_filling_retry",
            return_value=(
                SimpleNamespace(retcode=10010, comment="partial"),
                1,
                [],
            ),
        ):
            result = mt5_service.close_trade(
                live.ticket,
                expected_symbol=live.symbol,
                expected_magic=MAGIC_NUMBER,
                expected_comment=COMMENT,
                expected_account_login=123456,
                expected_identifier=live.identifier,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["retcode"], fake.TRADE_RETCODE_DONE_PARTIAL)

    def test_automatic_tp1_path_still_uses_profitable_break_even(self):
        trade = {
            "chat_id": 1,
            "message_id": 10,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "positions": [
                {"ticket": 100, "closed": True, "take_profit_confirmed": True},
                {"ticket": 101, "closed": False, "break_even": False, "tp": 120},
            ],
        }
        live = position(entry=101.5, sl=99, tp=120)

        with patch.object(
            position_manager,
            "query_position",
            return_value=mt5_service.PositionQueryResult(
                status=mt5_service.POSITION_OPEN,
                ticket=101,
                position=live,
            ),
        ), patch.object(position_manager, "has_pending_break_even", return_value=False), \
            patch.object(position_manager, "record_automatic_pending"), \
            patch.object(position_manager, "notify_break_even"), \
            patch.object(
                position_manager,
                "apply_profitable_break_even",
                return_value={
                    "status": break_even.STATUS_MOVED,
                    "target_sl": 102.5,
                },
            ) as apply_break_even:
            changed = position_manager._move_remaining_to_break_even(trade)

        self.assertTrue(changed)
        apply_break_even.assert_called_once_with(live, dry_run=False)


class AutomaticBreakEvenRetryTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.state_patch = patch.object(
            break_even_storage,
            "STATE_FILE",
            os.path.join(self.temp_dir.name, "break_even_actions.json"),
        )
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        break_even._pending_warnings.clear()

    def _seed_pending(self):
        item = position()
        result = {
            "status": break_even.STATUS_PENDING,
            "ticket": item.ticket,
            "side": "BUY",
            "target_sl": 101.0,
            "current_sl": item.sl,
            "reason": "BUY stop is inside broker stop/freeze distance",
        }
        self.assertTrue(break_even.record_automatic_pending(
            item,
            result,
            "automatic-test",
        ))
        self.assertEqual(
            len(break_even_storage.load_break_even_state()["pending"]),
            1,
        )
        return item

    def _retry_stack(self, item):
        return (
            patch.object(break_even, "is_paused", return_value=False),
            patch.object(break_even, "is_auto_execute", return_value=True),
            patch.object(
                break_even,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_OPEN,
                    ticket=item.ticket,
                    position=item,
                ),
            ),
            patch.object(break_even, "position_type_buy", return_value=0),
            patch.object(break_even, "position_type_sell", return_value=1),
            patch.object(break_even, "symbol_info", return_value=symbol_info()),
            patch.object(
                break_even,
                "modify_trade",
                return_value={
                    "success": True,
                    "noop": False,
                    "comment": "done",
                    "requested_sl": 101.0,
                    "current_sl": item.sl,
                    "side": "BUY",
                },
            ),
            patch.object(break_even, "notify_break_even_retry_summary"),
        )

    def test_pending_automatic_break_even_retries_safely(self):
        item = self._seed_pending()
        patches = self._retry_stack(item)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patches[5], patches[6] as modify_trade, patches[7]:
            summary = break_even.retry_pending_break_even_actions(force=True)

        self.assertEqual(summary["moved"], 1)
        modify_trade.assert_called_once()
        self.assertFalse(break_even_storage.load_break_even_state()["pending"])

    def test_completed_automatic_retry_is_not_duplicated(self):
        item = self._seed_pending()
        patches = self._retry_stack(item)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patches[5], patches[6] as modify_trade, patches[7]:
            first = break_even.retry_pending_break_even_actions(force=True)
            second = break_even.retry_pending_break_even_actions(force=True)

        self.assertEqual(first["moved"], 1)
        self.assertEqual(second["retried"], 0)
        modify_trade.assert_called_once()


if __name__ == "__main__":
    unittest.main()
