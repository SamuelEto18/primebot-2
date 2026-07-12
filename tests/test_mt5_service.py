import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import mt5_service


class FakeMT5:
    POSITION_TYPE_BUY = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 2
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 0
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_REASON_CLIENT = 0
    DEAL_REASON_TP = 2

    def __init__(self):
        self.initialized = False
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self.fail_initialize = False
        self.terminal_available = True
        self.positions = []
        self.positions_none = False
        self.deals = []
        self.last_error_value = (1, "error")
        self.events = []
        self.sleep_positions = False

    def initialize(self):
        self.events.append("initialize_start")
        self.initialize_calls += 1
        if self.fail_initialize:
            self.events.append("initialize_end")
            return False
        self.initialized = True
        self.terminal_available = True
        self.events.append("initialize_end")
        return True

    def shutdown(self):
        self.shutdown_calls += 1
        self.initialized = False

    def terminal_info(self):
        return object() if self.terminal_available else None

    def account_info(self):
        return object() if self.terminal_available else None

    def positions_get(self, **kwargs):
        self.events.append("positions_start")
        if self.sleep_positions:
            time.sleep(0.05)
        self.events.append("positions_end")
        if self.positions_none:
            return None
        ticket = kwargs.get("ticket")
        if ticket is None:
            return tuple(self.positions)
        return tuple(p for p in self.positions if p.ticket == ticket)

    def history_deals_get(self, *args, **kwargs):
        position_id = kwargs.get("position")
        deals = self.deals

        if position_id is not None:
            deals = [
                deal for deal in deals
                if getattr(deal, "position_id", None) == position_id
            ]

        return tuple(deals)

    def last_error(self):
        return self.last_error_value


class MT5ServiceTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeMT5()
        mt5_service.set_mt5_api(self.fake)
        mt5_service._initialized = False
        mt5_service._reconnecting = False

    def test_successful_initialization(self):
        self.assertTrue(mt5_service.initialize())
        self.assertEqual(self.fake.initialize_calls, 1)

    def test_failed_initialization(self):
        self.fake.fail_initialize = True
        with self.assertRaises(RuntimeError):
            mt5_service.initialize()

    def test_duplicate_initialization_prevention(self):
        self.assertTrue(mt5_service.initialize())
        self.assertTrue(mt5_service.initialize())
        self.assertEqual(self.fake.initialize_calls, 1)

    def test_reconnect_success(self):
        self.assertTrue(mt5_service.reconnect())
        self.assertEqual(self.fake.shutdown_calls, 1)
        self.assertEqual(self.fake.initialize_calls, 1)

    def test_reconnect_failure(self):
        self.fake.fail_initialize = True
        self.assertFalse(mt5_service.reconnect())

    def test_position_open(self):
        self.fake.positions = [SimpleNamespace(ticket=123)]
        result = mt5_service.query_position(123)
        self.assertEqual(result.status, mt5_service.POSITION_OPEN)

    def test_mt5_unavailable(self):
        self.fake.terminal_available = False
        result = mt5_service.query_position(123)
        self.assertEqual(result.status, mt5_service.MT5_UNAVAILABLE)

    def test_failed_position_query(self):
        self.fake.positions_none = True
        result = mt5_service.query_position(123)
        self.assertEqual(result.status, mt5_service.POSITION_QUERY_ERROR)

    def test_confirmed_tp_closure(self):
        self.fake.deals = [
            SimpleNamespace(position_id=123, entry=self.fake.DEAL_ENTRY_OUT, reason=self.fake.DEAL_REASON_TP, price=10, volume=0.01)
        ]
        result = mt5_service.confirm_position_closed_by_tp(123, 10)
        self.assertTrue(result["confirmed"])

    def test_closure_without_confirmed_take_profit(self):
        self.fake.deals = [
            SimpleNamespace(position_id=123, entry=self.fake.DEAL_ENTRY_OUT, reason=99, price=8, volume=0.01)
        ]
        result = mt5_service.confirm_position_closed_by_tp(123, 10)
        self.assertFalse(result["confirmed"])

    def test_concurrent_reconnect_and_position_query_serialization(self):
        self.fake.sleep_positions = True

        query_thread = threading.Thread(target=lambda: mt5_service.query_position(123))
        query_thread.start()
        time.sleep(0.01)
        reconnect_thread = threading.Thread(target=mt5_service.reconnect)
        reconnect_thread.start()
        query_thread.join()
        reconnect_thread.join()

        self.assertLess(
            self.fake.events.index("positions_end"),
            self.fake.events.index("initialize_start")
        )


if __name__ == "__main__":
    unittest.main()
