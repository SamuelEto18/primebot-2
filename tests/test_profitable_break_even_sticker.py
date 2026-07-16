import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

from telethon.tl.types import DocumentAttributeSticker

from config import (
    COMMENT,
    MAGIC_NUMBER,
    PRIMEBOT2_TELEGRAM_CHANNEL_ID,
)
from core import break_even
from core import break_even_storage
from core import mt5_service
from core import position_manager
from core import signal_processor


APPROVED_DOCUMENT_ID = 5422500716344283971
STICKER_SET_ID = 2713762944105054219


def symbol_info():
    return SimpleNamespace(
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
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
    )


def sticker_event(
    message_id=3424,
    document_id=APPROVED_DOCUMENT_ID,
    chat_id=PRIMEBOT2_TELEGRAM_CHANNEL_ID,
    emoji="\u303d\ufe0f",
    sticker=True,
):
    attributes = (
        [DocumentAttributeSticker(alt=emoji, stickerset=STICKER_SET_ID)]
        if sticker
        else [SimpleNamespace(file_name="sticker.webp")]
    )
    document = SimpleNamespace(id=document_id, attributes=attributes)
    return SimpleNamespace(
        id=message_id,
        message_id=message_id,
        chat_id=chat_id,
        raw_text="",
        media=SimpleNamespace(document=document),
        document=document,
        message=SimpleNamespace(document=document),
    )


class ProfitableBreakEvenStickerTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.state_file = os.path.join(
            self.temp_dir.name,
            "break_even_actions.json",
        )
        self.state_patch = patch.object(
            break_even_storage,
            "STATE_FILE",
            self.state_file,
        )
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        break_even._pending_warnings.clear()

    def _runtime_stack(
        self,
        positions=None,
        paused=False,
        auto_execute=True,
        bid=105.0,
        ask=105.1,
        modify_result=None,
    ):
        stack = ExitStack()
        mocks = {}
        mocks["positions_get"] = stack.enter_context(
            patch.object(break_even, "positions_get", return_value=positions or [])
        )
        stack.enter_context(patch.object(break_even, "is_paused", return_value=paused))
        stack.enter_context(
            patch.object(
                break_even,
                "is_auto_execute",
                return_value=auto_execute,
            )
        )
        stack.enter_context(patch.object(break_even, "position_type_buy", return_value=0))
        stack.enter_context(patch.object(break_even, "position_type_sell", return_value=1))
        stack.enter_context(
            patch.object(break_even, "symbol_info", return_value=symbol_info())
        )
        stack.enter_context(
            patch.object(
                break_even,
                "symbol_info_tick",
                return_value=SimpleNamespace(bid=bid, ask=ask),
            )
        )
        mocks["modify_trade"] = stack.enter_context(
            patch.object(
                break_even,
                "modify_trade",
                return_value=(
                    modify_result
                    or {"success": True, "comment": "done", "noop": False}
                ),
            )
        )
        mocks["notify"] = stack.enter_context(
            patch.object(break_even, "notify_break_even_summary")
        )
        stack.enter_context(patch.object(break_even, "notify_error"))
        return stack, mocks

    def _seed_pending(self):
        item = position()
        stack, mocks = self._runtime_stack(
            positions=[item],
            bid=100.8,
            ask=100.9,
        )

        with stack:
            self.assertTrue(break_even.handle_break_even_sticker(sticker_event()))

        mocks["modify_trade"].assert_not_called()
        state = break_even_storage.load_break_even_state()
        self.assertEqual(len(state["pending"]), 1)
        return item

    def test_approved_document_id_triggers_break_even(self):
        item = position()
        stack, mocks = self._runtime_stack(positions=[item])

        with stack:
            asyncio.run(signal_processor.process_new_message(sticker_event()))

        mocks["modify_trade"].assert_called_once_with(
            101,
            sl=101.0,
            expected_symbol="XAUUSD.s",
            expected_magic=MAGIC_NUMBER,
            expected_comment=COMMENT,
        )
        summary = mocks["notify"].call_args.args[0]
        self.assertEqual(summary["moved"], 1)

    def test_other_sticker_from_same_sticker_set_is_ignored(self):
        stack, mocks = self._runtime_stack(positions=[position()])

        with stack:
            asyncio.run(
                signal_processor.process_new_message(
                    sticker_event(document_id=APPROVED_DOCUMENT_ID + 1)
                )
            )

        mocks["positions_get"].assert_not_called()
        mocks["modify_trade"].assert_not_called()

    def test_same_emoji_with_other_document_id_is_ignored(self):
        stack, mocks = self._runtime_stack(positions=[position()])

        with stack:
            handled = break_even.handle_break_even_sticker(
                sticker_event(
                    document_id=999999999,
                    emoji="\u303d\ufe0f",
                )
            )

        self.assertFalse(handled)
        mocks["positions_get"].assert_not_called()

    def test_non_sticker_media_is_ignored(self):
        stack, mocks = self._runtime_stack(positions=[position()])

        with stack:
            asyncio.run(
                signal_processor.process_new_message(
                    sticker_event(sticker=False)
                )
            )

        mocks["positions_get"].assert_not_called()
        mocks["modify_trade"].assert_not_called()

    def test_wrong_chat_id_is_ignored(self):
        stack, mocks = self._runtime_stack(positions=[position()])

        with stack:
            handled = break_even.handle_break_even_sticker(
                sticker_event(chat_id=-100123)
            )

        self.assertFalse(handled)
        mocks["positions_get"].assert_not_called()

    def test_repeated_delivery_of_same_message_executes_once(self):
        stack, mocks = self._runtime_stack(positions=[position()])

        with stack:
            event = sticker_event(message_id=5000)
            self.assertTrue(break_even.handle_break_even_sticker(event))
            self.assertTrue(break_even.handle_break_even_sticker(event))

        mocks["modify_trade"].assert_called_once()
        state = break_even_storage.load_break_even_state()
        self.assertEqual(list(state["actions"]), [f"{PRIMEBOT2_TELEGRAM_CHANNEL_ID}:5000"])

    def test_new_message_using_same_sticker_executes_again(self):
        stack, mocks = self._runtime_stack(positions=[position()])

        with stack:
            break_even.handle_break_even_sticker(sticker_event(message_id=5001))
            break_even.handle_break_even_sticker(sticker_event(message_id=5002))

        self.assertEqual(mocks["modify_trade"].call_count, 2)

    def test_buy_target_is_normalized_entry_plus_one(self):
        info = SimpleNamespace(digits=2, point=0.01, trade_tick_size=0.05)
        self.assertEqual(
            break_even.profitable_break_even_price(
                "XAUUSD.s",
                "BUY",
                100.023,
                info=info,
            ),
            101.0,
        )

    def test_sell_target_is_normalized_entry_minus_one(self):
        info = SimpleNamespace(digits=2, point=0.01, trade_tick_size=0.05)
        self.assertEqual(
            break_even.profitable_break_even_price(
                "XAUUSD.s",
                "SELL",
                100.027,
                info=info,
            ),
            99.05,
        )

    def test_automatic_tp1_uses_shared_profitable_break_even_helper(self):
        trade = {
            "chat_id": 1,
            "message_id": 10,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "positions": [
                {
                    "ticket": 100,
                    "closed": True,
                    "take_profit_confirmed": True,
                },
                {
                    "ticket": 101,
                    "closed": False,
                    "break_even": False,
                    "tp": 120,
                },
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
            patch.object(break_even, "position_type_buy", return_value=0), \
            patch.object(break_even, "position_type_sell", return_value=1), \
            patch.object(break_even, "symbol_info", return_value=symbol_info()), \
            patch.object(
                break_even,
                "symbol_info_tick",
                return_value=SimpleNamespace(bid=110, ask=110.1),
            ), patch.object(
                break_even,
                "modify_trade",
                return_value={"success": True, "noop": False, "comment": "done"},
            ) as modify_trade:
            changed = position_manager._move_remaining_to_break_even(trade)

        self.assertTrue(changed)
        self.assertEqual(
            modify_trade.call_args.kwargs["sl"],
            102.5,
        )

    def test_already_better_buy_stop_is_not_worsened(self):
        item = position(sl=102.0)
        stack, mocks = self._runtime_stack(positions=[item])

        with stack:
            result = break_even.apply_profitable_break_even(item)

        self.assertEqual(result["status"], break_even.STATUS_ALREADY_PROTECTED)
        mocks["modify_trade"].assert_not_called()

    def test_already_better_sell_stop_is_not_worsened(self):
        item = position(side=1, sl=98.0, tp=80.0)
        stack, mocks = self._runtime_stack(
            positions=[item],
            bid=94.9,
            ask=95.0,
        )

        with stack:
            result = break_even.apply_profitable_break_even(item)

        self.assertEqual(result["status"], break_even.STATUS_ALREADY_PROTECTED)
        mocks["modify_trade"].assert_not_called()

    def test_manual_position_is_ignored(self):
        result = break_even.apply_profitable_break_even(
            position(magic=0, comment="manual")
        )
        self.assertEqual(result["status"], break_even.STATUS_IGNORED)

    def test_wrong_magic_is_ignored(self):
        result = break_even.apply_profitable_break_even(position(magic=123))
        self.assertEqual(result["status"], break_even.STATUS_IGNORED)

    def test_wrong_comment_is_ignored(self):
        result = break_even.apply_profitable_break_even(position(comment="PrimeBot1"))
        self.assertEqual(result["status"], break_even.STATUS_IGNORED)

    def test_wrong_symbol_is_ignored(self):
        result = break_even.apply_profitable_break_even(position(symbol="BTCUSD"))
        self.assertEqual(result["status"], break_even.STATUS_IGNORED)

    def test_foreign_positions_are_counted_in_summary(self):
        foreign = [
            position(ticket=201, magic=0, comment="manual"),
            position(ticket=202, magic=123),
            position(ticket=203, comment="PrimeBot1"),
            position(ticket=204, symbol="BTCUSD"),
        ]
        stack, mocks = self._runtime_stack(positions=foreign)

        with stack:
            break_even.handle_break_even_sticker(sticker_event())

        mocks["modify_trade"].assert_not_called()
        summary = mocks["notify"].call_args.args[0]
        self.assertEqual(summary["ignored"], 4)
        self.assertTrue(summary["no_primebot2_positions"])

    def test_modify_trade_rechecks_ownership_and_preserves_tp(self):
        live = position(tp=120.0)
        sent = []
        fake = SimpleNamespace(
            TRADE_ACTION_SLTP=6,
            TRADE_RETCODE_DONE=10009,
            POSITION_TYPE_BUY=0,
            POSITION_TYPE_SELL=1,
        )
        fake.positions_get = Mock(return_value=[live])
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
            )

            self.assertTrue(result["success"])
            self.assertEqual(sent[0]["tp"], 120.0)

            live.comment = "PrimeBot1"
            blocked = mt5_service.modify_trade(
                live.ticket,
                sl=102.0,
                expected_symbol="XAUUSD.s",
                expected_magic=MAGIC_NUMBER,
                expected_comment=COMMENT,
            )

        self.assertFalse(blocked["success"])
        self.assertTrue(blocked["ownership_mismatch"])
        self.assertEqual(fake.order_send.call_count, 1)

    def test_dry_run_makes_no_mt5_order_send(self):
        stack, mocks = self._runtime_stack(
            positions=[position()],
            auto_execute=False,
        )

        with stack:
            break_even.handle_break_even_sticker(sticker_event())

        mocks["modify_trade"].assert_not_called()
        summary = mocks["notify"].call_args.args[0]
        self.assertEqual(summary["mode"], "dry_run")
        self.assertEqual(summary["simulated"], 1)

    def test_paused_mode_makes_no_mt5_call(self):
        stack, mocks = self._runtime_stack(
            positions=[position()],
            paused=True,
        )

        with stack:
            break_even.handle_break_even_sticker(sticker_event())

        mocks["positions_get"].assert_not_called()
        mocks["modify_trade"].assert_not_called()
        self.assertTrue(mocks["notify"].call_args.args[0]["paused"])

    def test_temporarily_invalid_stop_becomes_pending(self):
        self._seed_pending()
        state = break_even_storage.load_break_even_state()
        pending = next(iter(state["pending"].values()))
        self.assertIn("stop/freeze distance", pending["reason"])

    def test_pending_action_retries_safely(self):
        item = self._seed_pending()

        with patch.object(break_even, "is_paused", return_value=False), \
            patch.object(break_even, "is_auto_execute", return_value=True), \
            patch.object(
                break_even,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_OPEN,
                    ticket=item.ticket,
                    position=item,
                ),
            ), patch.object(break_even, "position_type_buy", return_value=0), \
            patch.object(break_even, "position_type_sell", return_value=1), \
            patch.object(break_even, "symbol_info", return_value=symbol_info()), \
            patch.object(
                break_even,
                "symbol_info_tick",
                return_value=SimpleNamespace(bid=105, ask=105.1),
            ), patch.object(
                break_even,
                "modify_trade",
                return_value={"success": True, "noop": False, "comment": "done"},
            ) as modify_trade, patch.object(
                break_even,
                "notify_break_even_retry_summary",
            ):
            summary = break_even.retry_pending_break_even_actions(force=True)

        self.assertEqual(summary["moved"], 1)
        modify_trade.assert_called_once()
        self.assertFalse(break_even_storage.load_break_even_state()["pending"])

    def test_duplicate_retry_does_not_duplicate_modifications(self):
        item = self._seed_pending()

        with patch.object(break_even, "is_paused", return_value=False), \
            patch.object(break_even, "is_auto_execute", return_value=True), \
            patch.object(
                break_even,
                "query_position",
                return_value=mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_OPEN,
                    ticket=item.ticket,
                    position=item,
                ),
            ), patch.object(break_even, "position_type_buy", return_value=0), \
            patch.object(break_even, "position_type_sell", return_value=1), \
            patch.object(break_even, "symbol_info", return_value=symbol_info()), \
            patch.object(
                break_even,
                "symbol_info_tick",
                return_value=SimpleNamespace(bid=105, ask=105.1),
            ), patch.object(
                break_even,
                "modify_trade",
                return_value={"success": True, "noop": False, "comment": "done"},
            ) as modify_trade, patch.object(
                break_even,
                "notify_break_even_retry_summary",
            ):
            first = break_even.retry_pending_break_even_actions(force=True)
            second = break_even.retry_pending_break_even_actions(force=True)

        self.assertEqual(first["moved"], 1)
        self.assertEqual(second["retried"], 0)
        modify_trade.assert_called_once()

    def test_no_open_positions_produces_safe_notification(self):
        stack, mocks = self._runtime_stack(positions=[])

        with stack:
            break_even.handle_break_even_sticker(sticker_event())

        mocks["modify_trade"].assert_not_called()
        summary = mocks["notify"].call_args.args[0]
        self.assertTrue(summary["no_primebot2_positions"])
        self.assertEqual(summary["positions_discovered"], 0)


if __name__ == "__main__":
    unittest.main()
