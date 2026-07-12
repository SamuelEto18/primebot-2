import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import COMMENT, MAGIC_NUMBER
from core import executor
from core import mt5_service


class FakeMT5Phase4B:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 2
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_RETURN = 4
    SYMBOL_TRADE_EXECUTION_MARKET = 4
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_INVALID_FILL = 10030
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_REASON_CLIENT = 0
    DEAL_REASON_MOBILE = 10
    DEAL_REASON_WEB = 11
    DEAL_REASON_TP = 2
    DEAL_REASON_SL = 3

    def __init__(self):
        self.terminal_available = True
        self.positions = []
        self.deals = []
        self.orders = []
        self.history_calls = []
        self.order_history_calls = []
        self.results = []
        self.sent_requests = []
        self.info = SimpleNamespace(
            visible=True,
            filling_mode=self.SYMBOL_FILLING_FOK | self.SYMBOL_FILLING_IOC,
            trade_exemode=0,
        )
        self.tick = SimpleNamespace(ask=100.25, bid=100.0)
        self.last_error_value = (1, "error")

    def terminal_info(self):
        return object() if self.terminal_available else None

    def initialize(self):
        return True

    def shutdown(self):
        return True

    def account_info(self):
        return object()

    def symbol_info(self, symbol):
        return self.info

    def symbol_select(self, symbol, enabled):
        self.info.visible = enabled
        return True

    def symbol_info_tick(self, symbol):
        return self.tick

    def order_send(self, request):
        self.sent_requests.append(dict(request))

        if self.results:
            return self.results.pop(0)

        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=111, deal=222, price=100.3, comment="done")

    def positions_get(self, **kwargs):
        ticket = kwargs.get("ticket")
        symbol = kwargs.get("symbol")
        positions = self.positions

        if ticket is not None:
            positions = [p for p in positions if p.ticket == ticket]

        if symbol is not None:
            positions = [p for p in positions if p.symbol == symbol]

        return tuple(positions)

    def history_deals_get(self, *args, **kwargs):
        self.history_calls.append({
            "args": args,
            "kwargs": dict(kwargs),
        })
        position_id = kwargs.get("position")
        deals = self.deals

        if position_id is not None:
            deals = [
                deal for deal in deals
                if getattr(deal, "position_id", None) == position_id
            ]

        return tuple(deals)

    def history_orders_get(self, *args, **kwargs):
        self.order_history_calls.append({
            "args": args,
            "kwargs": dict(kwargs),
        })
        ticket = kwargs.get("ticket")
        orders = self.orders

        if ticket is not None:
            orders = [order for order in orders if getattr(order, "ticket", None) == ticket]

        return tuple(orders)

    def last_error(self):
        return self.last_error_value


def position(ticket=900, identifier=500, symbol="XAUUSD.s", magic=MAGIC_NUMBER, comment=COMMENT, price_open=100.4, volume=0.01, position_type=FakeMT5Phase4B.POSITION_TYPE_BUY, sl=99, tp=110, time=1893456000):
    return SimpleNamespace(
        ticket=ticket,
        identifier=identifier,
        symbol=symbol,
        type=position_type,
        volume=volume,
        magic=magic,
        comment=comment,
        price_open=price_open,
        sl=sl,
        tp=tp,
        time=time,
    )


def open_deal(
    ticket=222,
    order=111,
    position_id=500,
    price=100.4,
    symbol="XAUUSD.s",
    volume=0.01,
    time=1893456001,
    reason=0,
    magic=MAGIC_NUMBER,
    comment=COMMENT,
):
    return SimpleNamespace(
        ticket=ticket,
        order=order,
        position_id=position_id,
        symbol=symbol,
        type=FakeMT5Phase4B.DEAL_TYPE_BUY,
        entry=FakeMT5Phase4B.DEAL_ENTRY_IN,
        reason=reason,
        magic=magic,
        comment=comment,
        price=price,
        volume=volume,
        time=time,
    )


def close_deal(
    position_id=500,
    reason=FakeMT5Phase4B.DEAL_REASON_TP,
    price=110,
    volume=0.01,
    ticket=333,
    order=444,
    symbol="XAUUSD.s",
    entry=FakeMT5Phase4B.DEAL_ENTRY_OUT,
    deal_type=FakeMT5Phase4B.DEAL_TYPE_SELL,
    magic=0,
    comment="",
    time=1893456002,
    profit=None,
):
    return SimpleNamespace(
        ticket=ticket,
        order=order,
        position_id=position_id,
        symbol=symbol,
        type=deal_type,
        entry=entry,
        reason=reason,
        magic=magic,
        comment=comment,
        price=price,
        volume=volume,
        time=time,
        profit=profit,
    )


def real_open_deal():
    return open_deal(
        ticket=214484273,
        order=280608772,
        position_id=280608772,
        price=4125.89,
        reason=FakeMT5Phase4B.DEAL_REASON_SL,
        time=1893456001,
    )


def real_manual_close_deal():
    return close_deal(
        ticket=214491640,
        order=280619893,
        position_id=280608772,
        reason=FakeMT5Phase4B.DEAL_REASON_CLIENT,
        price=4124.50,
        volume=0.01,
        time=1893456010,
        profit=-1.21,
    )


class MT5IdentityTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeMT5Phase4B()
        mt5_service.set_mt5_api(self.fake)
        mt5_service._initialized = False
        mt5_service._reconnecting = False

    def test_result_order_differs_from_actual_position_ticket(self):
        self.fake.positions = [position(ticket=900, identifier=500)]
        self.fake.deals = [open_deal(ticket=222, order=111, position_id=500)]
        self.fake.results = [SimpleNamespace(retcode=self.fake.TRADE_RETCODE_DONE, order=111, deal=222, price=100.3, comment="done")]

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertEqual(result["ticket"], 900)
        self.assertEqual(result["order_ticket"], 111)
        self.assertEqual(result["deal_ticket"], 222)
        self.assertEqual(result["position_identifier"], 500)

    def test_multiple_same_symbol_hedging_positions_resolve_by_position_id(self):
        self.fake.positions = [
            position(ticket=900, identifier=500),
            position(ticket=901, identifier=501),
        ]
        self.fake.deals = [open_deal(ticket=222, order=111, position_id=501)]
        self.fake.results = [SimpleNamespace(retcode=self.fake.TRADE_RETCODE_DONE, order=111, deal=222, price=100.3, comment="done")]

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertEqual(result["ticket"], 901)

    def test_manual_same_symbol_position_is_not_selected(self):
        self.fake.positions = [
            position(ticket=1, identifier=1, magic=0, comment="manual"),
            position(ticket=900, identifier=500),
        ]
        self.fake.deals = [open_deal(position_id=500)]

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertEqual(result["ticket"], 900)

    def test_unresolved_ambiguous_position_is_rejected(self):
        self.fake.positions = [
            position(ticket=900, identifier=500),
            position(ticket=901, identifier=501),
        ]
        self.fake.deals = []

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertFalse(result["success"])
        self.assertTrue(result["unresolved"])
        self.assertIsNone(result["ticket"])


class MT5OpenPriceTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeMT5Phase4B()
        mt5_service.set_mt5_api(self.fake)

    def test_opening_deal_price_is_authoritative(self):
        self.fake.positions = [position(price_open=100.45)]
        self.fake.deals = [open_deal(position_id=500)]
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertEqual(result["price"], 100.4)
        self.assertEqual(result["entry_source"], "opening_deal_price")

    def test_result_price_used_as_fallback(self):
        self.fake.positions = [position(price_open=None)]
        self.fake.deals = [open_deal(position_id=500, price=None)]
        self.fake.results = [SimpleNamespace(retcode=self.fake.TRADE_RETCODE_DONE, order=111, deal=222, price=100.33, comment="done")]
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertEqual(result["price"], 100.33)
        self.assertEqual(result["entry_source"], "execution_result")

    def test_requested_price_used_as_final_fallback(self):
        self.fake.positions = [position(price_open=None)]
        self.fake.deals = [open_deal(position_id=500, price=None)]
        self.fake.results = [SimpleNamespace(retcode=self.fake.TRADE_RETCODE_DONE, order=111, deal=222, comment="done")]
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertEqual(result["price"], 100.25)
        self.assertEqual(result["entry_source"], "requested_price")


class MT5TPConfirmationTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeMT5Phase4B()
        mt5_service.set_mt5_api(self.fake)

    def test_genuine_full_tp_close_confirmed(self):
        self.fake.positions = []
        self.fake.deals = [close_deal(position_id=500, reason=self.fake.DEAL_REASON_TP, price=110, volume=0.01)]
        result = mt5_service.confirm_position_closed_by_tp(900, 110, position_identifier=500, expected_volume=0.01)
        self.assertTrue(result["confirmed"])

    def test_real_manual_close_reason_zero_confirmed_without_close_magic_comment(self):
        self.fake.positions = []
        self.fake.deals = [real_open_deal(), real_manual_close_deal()]

        result = mt5_service.confirm_position_closed(
            280608772,
            tp=4132,
            position_identifier=280608772,
            expected_volume=0.01,
            symbol="XAUUSD.s",
            side="BUY",
            opening_deal_ticket=214484273,
            opening_order_ticket=280608772,
            expected_magic=MAGIC_NUMBER,
            expected_comment=COMMENT,
        )

        self.assertTrue(result["confirmed"])
        self.assertFalse(result["pending"])
        self.assertEqual(result["close_reason"], "manual_close")
        self.assertEqual(result["metadata"]["close_deal_ticket"], 214491640)
        self.assertEqual(result["metadata"]["close_order_ticket"], 280619893)
        self.assertEqual(result["metadata"]["close_price"], 4124.50)
        self.assertEqual(result["metadata"]["close_volume"], 0.01)
        self.assertFalse(result["metadata"]["take_profit_confirmed"])
        self.assertEqual(
            self.fake.history_calls[0],
            {"args": (), "kwargs": {"position": 280608772}},
        )

    def test_exact_position_lookup_finds_close_without_date_range_lookup(self):
        self.fake.positions = []
        self.fake.deals = [real_open_deal(), real_manual_close_deal()]

        result = mt5_service.confirm_position_closed(
            280608772,
            position_identifier=280608772,
            expected_volume=0.01,
            symbol="XAUUSD.s",
            side="BUY",
        )

        self.assertTrue(result["confirmed"])
        self.assertTrue(self.fake.history_calls)
        self.assertTrue(all(not call["args"] for call in self.fake.history_calls))

    def test_multiple_closing_deals_aggregate_with_final_metadata(self):
        self.fake.positions = []
        self.fake.deals = [
            open_deal(position_id=500, time=100),
            close_deal(
                position_id=500,
                reason=self.fake.DEAL_REASON_CLIENT,
                price=109,
                volume=0.004,
                ticket=333,
                order=444,
                time=101,
            ),
            close_deal(
                position_id=500,
                reason=self.fake.DEAL_REASON_SL,
                price=108,
                volume=0.006,
                ticket=334,
                order=445,
                time=102,
            ),
        ]

        result = mt5_service.confirm_position_closed(
            900,
            position_identifier=500,
            expected_volume=0.01,
            symbol="XAUUSD.s",
            side="BUY",
        )

        self.assertTrue(result["confirmed"])
        self.assertEqual(result["metadata"]["close_deal_ticket"], 334)
        self.assertEqual(result["metadata"]["close_order_ticket"], 445)
        self.assertEqual(result["metadata"]["close_reason"], "stop_loss")
        self.assertEqual(result["metadata"]["close_volume"], 0.01)

    def test_out_by_close_entry_is_recognized(self):
        self.fake.positions = []
        self.fake.deals = [
            open_deal(position_id=500, time=100),
            close_deal(
                position_id=500,
                reason=self.fake.DEAL_REASON_CLIENT,
                entry=self.fake.DEAL_ENTRY_OUT_BY,
                time=101,
            ),
        ]

        result = mt5_service.confirm_position_closed(
            900,
            position_identifier=500,
            expected_volume=0.01,
            symbol="XAUUSD.s",
            side="BUY",
        )

        self.assertTrue(result["confirmed"])
        self.assertEqual(result["close_reason"], "manual_close")

    def test_manual_close_exactly_at_tp_price_rejected(self):
        self.fake.positions = []
        self.fake.deals = [close_deal(position_id=500, reason=99, price=110, volume=0.01)]
        result = mt5_service.confirm_position_closed_by_tp(900, 110, position_identifier=500, expected_volume=0.01)
        self.assertFalse(result["confirmed"])

    def test_stop_loss_close_at_tp_like_price_rejected(self):
        self.fake.positions = []
        self.fake.deals = [close_deal(position_id=500, reason=self.fake.DEAL_REASON_SL, price=110, volume=0.01)]
        result = mt5_service.confirm_position_closed_by_tp(900, 110, position_identifier=500, expected_volume=0.01)
        self.assertFalse(result["confirmed"])

    def test_partial_tp_close_does_not_confirm(self):
        self.fake.positions = []
        self.fake.deals = [close_deal(position_id=500, reason=self.fake.DEAL_REASON_TP, price=110, volume=0.005)]
        result = mt5_service.confirm_position_closed_by_tp(900, 110, position_identifier=500, expected_volume=0.01)
        self.assertFalse(result["confirmed"])

    def test_delayed_history_keeps_pending(self):
        self.fake.positions = []
        self.fake.history_deals_get = lambda date_from, date_to, **kwargs: None
        result = mt5_service.confirm_position_closed_by_tp(900, 110, position_identifier=500, expected_volume=0.01)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["pending"])

    def test_mismatched_position_id_rejected(self):
        self.fake.positions = []
        self.fake.deals = [close_deal(position_id=999, reason=self.fake.DEAL_REASON_TP, price=110, volume=0.01)]
        result = mt5_service.confirm_position_closed_by_tp(900, 110, position_identifier=500, expected_volume=0.01)
        self.assertFalse(result["confirmed"])

    def test_opening_deal_is_not_mistaken_for_closing_deal(self):
        self.fake.positions = []
        self.fake.deals = [open_deal(position_id=500, price=110)]

        result = mt5_service.confirm_position_closed(
            900,
            position_identifier=500,
            expected_volume=0.01,
            symbol="XAUUSD.s",
            side="BUY",
        )

        self.assertFalse(result["confirmed"])
        self.assertTrue(result["pending"])

    def test_remaining_open_volume_keeps_pending(self):
        self.fake.positions = [position(ticket=900, identifier=500, volume=0.005)]
        self.fake.deals = [close_deal(position_id=500, reason=self.fake.DEAL_REASON_TP, price=110, volume=0.005)]
        result = mt5_service.confirm_position_closed_by_tp(900, 110, position_identifier=500, expected_volume=0.01)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["pending"])


class MT5PositionMatchingTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeMT5Phase4B()
        mt5_service.set_mt5_api(self.fake)

    def test_unknown_position_type_rejected_for_buy_and_sell(self):
        candidate = position(position_type=999)

        self.assertFalse(
            mt5_service._position_matches_primebot_request(
                candidate,
                "XAUUSD.s",
                "BUY",
                0.01,
            )
        )
        self.assertFalse(
            mt5_service._position_matches_primebot_request(
                candidate,
                "XAUUSD.s",
                "SELL",
                0.01,
            )
        )

    def test_missing_position_comment_rejected(self):
        candidate = position(comment=None)
        delattr(candidate, "comment")

        self.assertFalse(
            mt5_service._position_matches_primebot_request(
                candidate,
                "XAUUSD.s",
                "BUY",
                0.01,
            )
        )

    def test_incorrect_comment_rejected(self):
        candidate = position(comment="manual")

        self.assertFalse(
            mt5_service._position_matches_primebot_request(
                candidate,
                "XAUUSD.s",
                "BUY",
                0.01,
            )
        )

    def test_valid_buy_accepted(self):
        candidate = position(position_type=self.fake.POSITION_TYPE_BUY)

        self.assertTrue(
            mt5_service._position_matches_primebot_request(
                candidate,
                "XAUUSD.s",
                "BUY",
                0.01,
            )
        )

    def test_valid_sell_accepted(self):
        candidate = position(position_type=self.fake.POSITION_TYPE_SELL)

        self.assertTrue(
            mt5_service._position_matches_primebot_request(
                candidate,
                "XAUUSD.s",
                "SELL",
                0.01,
            )
        )


class MT5FillingPolicyTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeMT5Phase4B()
        self.fake.positions = [position()]
        self.fake.deals = [open_deal(position_id=500)]
        mt5_service.set_mt5_api(self.fake)

    def test_ioc_only_symbol(self):
        self.fake.info.filling_mode = self.fake.SYMBOL_FILLING_IOC
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertTrue(result["success"])
        self.assertEqual(self.fake.sent_requests[0]["type_filling"], self.fake.ORDER_FILLING_IOC)

    def test_fok_only_symbol(self):
        self.fake.info.filling_mode = self.fake.SYMBOL_FILLING_FOK
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertTrue(result["success"])
        self.assertEqual(self.fake.sent_requests[0]["type_filling"], self.fake.ORDER_FILLING_FOK)

    def test_return_only_forbidden_for_market_execution_sends_no_order(self):
        self.fake.info.filling_mode = self.fake.SYMBOL_FILLING_RETURN
        self.fake.info.trade_exemode = self.fake.SYMBOL_TRADE_EXECUTION_MARKET

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertFalse(result["success"])
        self.assertEqual(self.fake.sent_requests, [])
        self.assertIn("No broker-supported filling policy", result["comment"])

    def test_return_filtered_when_legal_market_alternative_exists(self):
        self.fake.info.filling_mode = self.fake.SYMBOL_FILLING_RETURN | self.fake.SYMBOL_FILLING_IOC
        self.fake.info.trade_exemode = self.fake.SYMBOL_TRADE_EXECUTION_MARKET

        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)

        self.assertTrue(result["success"])
        self.assertEqual(len(self.fake.sent_requests), 1)
        self.assertEqual(self.fake.sent_requests[0]["type_filling"], self.fake.ORDER_FILLING_IOC)


    def test_explicit_empty_capabilities_do_not_invent_policy(self):
        self.fake.info.supported_fillings = []

        policies = mt5_service._supported_filling_policies(self.fake.info)

        self.assertEqual(policies, [])

    def test_missing_capability_information_uses_fallback_policies(self):
        info = SimpleNamespace(visible=True, trade_exemode=0)

        policies = mt5_service._supported_filling_policies(info)

        self.assertIn(self.fake.ORDER_FILLING_IOC, policies)

    def test_filling_rejection_retries_one_safe_alternative(self):
        self.fake.info.filling_mode = self.fake.SYMBOL_FILLING_FOK | self.fake.SYMBOL_FILLING_IOC
        self.fake.results = [
            SimpleNamespace(retcode=self.fake.TRADE_RETCODE_INVALID_FILL, order=0, deal=0, comment="bad filling"),
            SimpleNamespace(retcode=self.fake.TRADE_RETCODE_DONE, order=111, deal=222, price=100.3, comment="done"),
        ]
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertTrue(result["success"])
        self.assertEqual(len(self.fake.sent_requests), 2)

    def test_successful_first_execution_is_never_duplicated(self):
        self.fake.results = [SimpleNamespace(retcode=self.fake.TRADE_RETCODE_DONE, order=111, deal=222, price=100.3, comment="done")]
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertTrue(result["success"])
        self.assertEqual(len(self.fake.sent_requests), 1)

    def test_ambiguous_execution_checks_state_before_retry(self):
        self.fake.results = [None]
        result = mt5_service.open_trade("XAUUSD.s", "BUY", 99, 110)
        self.assertTrue(result["success"])
        self.assertEqual(len(self.fake.sent_requests), 1)


class MT5IntegrationStorageTests(unittest.TestCase):

    def test_executor_stores_actual_position_ticket(self):
        signal = SimpleNamespace(
            chat_id=1,
            message_id=2,
            symbol="XAUUSD.s",
            side="BUY",
            sl=99,
            tps=[110, 120],
            raw="raw",
        )
        calls = []

        def fake_open(symbol, side, sl, tp, **_kwargs):
            ticket = 900 + len(calls)
            calls.append(tp)
            return {
                "success": True,
                "ticket": ticket,
                "position_ticket": ticket,
                "position_identifier": 500 + len(calls),
                "order_ticket": 100 + len(calls),
                "deal_ticket": 200 + len(calls),
                "symbol": symbol,
                "side": side,
                "volume": 0.01,
                "requested_price": 100,
                "fill_price": 100.4,
                "price_open": 100.4,
                "price": 100.4,
                "entry_source": "position_price_open",
                "sl": sl,
                "magic": MAGIC_NUMBER,
                "comment": COMMENT,
            }

        with patch.object(executor, "open_trade", side_effect=fake_open), \
            patch.object(executor, "add_trade") as add_trade, \
            patch.object(executor, "notify_success"), \
            patch.object(executor, "notify_error"):
            summary = executor.execute_signal(signal)

        self.assertEqual(summary["opened"], 2)
        saved = add_trade.call_args.args[0]
        self.assertEqual(saved["positions"][0]["ticket"], 900)
        self.assertEqual(saved["positions"][0]["position_ticket"], 900)
        self.assertEqual(saved["positions"][0]["tp_index"], 1)


if __name__ == "__main__":
    unittest.main()
