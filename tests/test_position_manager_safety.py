import os
import unittest
os.environ.setdefault("BOT_TOKEN", "123456789:test-token")
os.environ.setdefault("ADMIN_ID", "1")

from types import SimpleNamespace
from unittest.mock import patch

from core import mt5_service
from core import position_manager


def sample_trade():
    return {
        "chat_id": 1,
        "message_id": 10,
        "symbol": "XAUUSD.s",
        "side": "BUY",
        "sl": 1,
        "raw_message": "raw",
        "positions": [
            {"ticket": 100, "tp": 110, "entry": 100, "closed": False, "break_even": False},
            {"ticket": 101, "tp": 120, "entry": 101, "closed": False, "break_even": False},
        ],
    }


class PositionManagerSafetyTests(unittest.TestCase):

    def setUp(self):
        position_manager._pending_close_history_warnings.clear()

    def test_break_even_not_triggered_on_mt5_failure(self):
        trade = sample_trade()

        with patch.object(
            position_manager,
            "query_position",
            return_value=mt5_service.PositionQueryResult(
                status=mt5_service.MT5_UNAVAILABLE,
                ticket=100,
                error="down"
            )
        ), patch.object(position_manager, "modify_trade") as modify_trade, \
            patch.object(position_manager, "update_trade") as update_trade:
            position_manager.process_trades_once([trade])

        self.assertFalse(trade["positions"][0]["closed"])
        self.assertFalse(trade["positions"][1]["break_even"])
        modify_trade.assert_not_called()
        update_trade.assert_not_called()


    def test_break_even_not_triggered_without_confirmed_take_profit(self):
        trade = sample_trade()

        def query(ticket):
            return mt5_service.PositionQueryResult(
                status=mt5_service.POSITION_ABSENT,
                ticket=ticket
            )

        with patch.object(position_manager, "query_position", side_effect=query), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": False,
                    "pending": True,
                    "reason": "No close deal found",
                    "deal": None,
                    "metadata": {},
                }
            ), patch.object(position_manager, "modify_trade") as modify_trade, \
            patch.object(position_manager, "update_trade") as update_trade:
            position_manager.process_trades_once([trade])

        self.assertFalse(trade["positions"][0]["closed"])
        self.assertFalse(trade["positions"][1]["break_even"])
        modify_trade.assert_not_called()
        update_trade.assert_not_called()

    def test_break_even_triggered_after_confirmed_tp1_take_profit(self):
        trade = sample_trade()

        def query(ticket):
            if ticket == 100:
                return mt5_service.PositionQueryResult(
                    status=mt5_service.POSITION_ABSENT,
                    ticket=ticket
                )
            return mt5_service.PositionQueryResult(
                status=mt5_service.POSITION_OPEN,
                ticket=ticket,
                position=SimpleNamespace(price_open=101.5)
            )

        with patch.object(position_manager, "query_position", side_effect=query), \
            patch.object(
                position_manager,
                "confirm_position_closed",
                return_value={
                    "confirmed": True,
                    "pending": False,
                    "reason": "take_profit",
                    "close_reason": "take_profit",
                    "deal": object(),
                    "metadata": {
                        "close_reason": "take_profit",
                        "close_price": 110,
                        "close_deal_ticket": 333,
                        "close_volume": 0.01,
                    },
                }
            ), patch.object(
                position_manager,
                "modify_trade",
                return_value={"success": True}
            ) as modify_trade, patch.object(position_manager, "notify_break_even"), \
            patch.object(position_manager, "notify_position_closed"), \
            patch.object(position_manager, "update_trade") as update_trade:
            position_manager.process_trades_once([trade])

        self.assertTrue(trade["positions"][0]["closed"])
        self.assertTrue(trade["positions"][1]["break_even"])
        modify_trade.assert_called_once_with(101, sl=101.5)
        update_trade.assert_called_once_with(trade)

    def test_repeated_pending_close_history_warning_is_rate_limited(self):
        trade = {
            "chat_id": 1,
            "message_id": 10,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "positions": [
                {
                    "ticket": 100,
                    "tp": 110,
                    "entry": 100,
                    "closed": False,
                    "break_even": False,
                },
            ],
        }

        with patch.object(
            position_manager,
            "query_position",
            return_value=mt5_service.PositionQueryResult(
                status=mt5_service.POSITION_ABSENT,
                ticket=100,
            )
        ), patch.object(
            position_manager,
            "confirm_position_closed",
            return_value={
                "confirmed": False,
                "pending": True,
                "reason": "No close deal found for position identity",
                "deal": None,
                "metadata": {},
            }
        ), patch.object(position_manager, "logger") as logger:
            position_manager.process_trades_once([trade])
            position_manager.process_trades_once([trade])

        pending_warnings = [
            call for call in logger.warning.call_args_list
            if "Position absent but close history pending" in call.args[0]
        ]
        self.assertEqual(len(pending_warnings), 1)

    def test_pending_close_history_state_change_bypasses_rate_limit(self):
        trade = {
            "chat_id": 1,
            "message_id": 10,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "positions": [
                {
                    "ticket": 100,
                    "tp": 110,
                    "entry": 100,
                    "closed": False,
                    "break_even": False,
                },
            ],
        }

        confirmations = [
            {
                "confirmed": False,
                "pending": True,
                "reason": "No close deal found for position identity",
                "deal": None,
                "metadata": {},
            },
            {
                "confirmed": False,
                "pending": True,
                "reason": "History temporarily unavailable",
                "deal": None,
                "metadata": {},
            },
        ]

        with patch.object(
            position_manager,
            "query_position",
            return_value=mt5_service.PositionQueryResult(
                status=mt5_service.POSITION_ABSENT,
                ticket=100,
            )
        ), patch.object(
            position_manager,
            "confirm_position_closed",
            side_effect=confirmations,
        ), patch.object(position_manager, "logger") as logger:
            position_manager.process_trades_once([trade])
            position_manager.process_trades_once([trade])

        pending_warnings = [
            call for call in logger.warning.call_args_list
            if "Position absent but close history pending" in call.args[0]
        ]
        self.assertEqual(len(pending_warnings), 2)


if __name__ == "__main__":
    unittest.main()
