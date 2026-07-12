import asyncio
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import COMMENT, MAGIC_NUMBER
from core import executor
from core import signal_processor
from core import synchronizer
from core.direction import infer_direction, validate_now_price
from core.parser import parse_signal, validate_signal


def event(text, message_id=100):
    return SimpleNamespace(
        raw_text=text,
        media=False,
        id=message_id,
        chat_id=1,
    )


class DirectionInferenceTests(unittest.TestCase):

    def test_buy_inferred_from_sl_below_all_tps(self):
        result = infer_direction(4027, [4039, 4043])

        self.assertTrue(result.valid)
        self.assertEqual(result.inferred_direction, "BUY")
        self.assertEqual(result.final_side, "BUY")

    def test_sell_inferred_from_sl_above_all_tps(self):
        result = infer_direction(4055, [4045, 4040])

        self.assertTrue(result.valid)
        self.assertEqual(result.inferred_direction, "SELL")
        self.assertEqual(result.final_side, "SELL")

    def test_textual_sell_overridden_by_buy_geometry(self):
        result = infer_direction(4027, [4039], textual_direction="SELL")

        self.assertTrue(result.valid)
        self.assertEqual(result.inferred_direction, "BUY")
        self.assertTrue(result.direction_conflict)

    def test_textual_buy_overridden_by_sell_geometry(self):
        result = infer_direction(4055, [4045], textual_direction="BUY")

        self.assertTrue(result.valid)
        self.assertEqual(result.inferred_direction, "SELL")
        self.assertTrue(result.direction_conflict)

    def test_correct_text_direction_produces_no_conflict(self):
        result = infer_direction(4027, [4039], textual_direction="BUY")

        self.assertTrue(result.valid)
        self.assertFalse(result.direction_conflict)

    def test_mixed_tps_rejected(self):
        result = infer_direction(4035, [4030, 4040])

        self.assertFalse(result.valid)
        self.assertIsNone(result.final_side)

    def test_sl_equal_to_tp_rejected(self):
        result = infer_direction(4035, [4035])

        self.assertFalse(result.valid)
        self.assertIsNone(result.final_side)

    def test_no_tp_rejected(self):
        result = infer_direction(4035, [])

        self.assertFalse(result.valid)
        self.assertIn("TP", result.reason)

    def test_invalid_numeric_values_rejected(self):
        cases = [
            ("bad", [4039]),
            (math.nan, [4039]),
            (4027, [math.inf]),
        ]

        for sl, tps in cases:
            with self.subTest(sl=sl, tps=tps):
                self.assertFalse(infer_direction(sl, tps).valid)

    def test_valid_buy_entry_range(self):
        result = infer_direction(4027, [4039, 4043], 4031, 4035)

        self.assertTrue(result.valid)
        self.assertEqual(result.entry_low, 4031)
        self.assertEqual(result.entry_high, 4035)

    def test_valid_sell_entry_range(self):
        result = infer_direction(4040, [4025, 4020], 4031, 4035)

        self.assertTrue(result.valid)
        self.assertEqual(result.inferred_direction, "SELL")

    def test_invalid_buy_entry_geometry_rejected(self):
        result = infer_direction(4027, [4035], 4040, 4040)

        self.assertFalse(result.valid)
        self.assertEqual(result.inferred_direction, "BUY")
        self.assertIn("Entry geometry invalid for BUY", result.reason)

    def test_invalid_sell_entry_geometry_rejected(self):
        result = infer_direction(4040, [4025], 4020, 4020)

        self.assertFalse(result.valid)
        self.assertEqual(result.inferred_direction, "SELL")
        self.assertIn("Entry geometry invalid for SELL", result.reason)

    def test_reversed_entry_range_normalized(self):
        result = infer_direction(4027, [4039], 4035, 4031)

        self.assertTrue(result.valid)
        self.assertEqual(result.entry_low, 4031)
        self.assertEqual(result.entry_high, 4035)

    def test_now_price_validation_for_buy(self):
        result = validate_now_price("BUY", 4027, [4039, 4043], 4032)

        self.assertTrue(result.valid)
        self.assertEqual(result.nearest_tp, 4039)

    def test_now_price_validation_for_sell(self):
        result = validate_now_price("SELL", 4040, [4025, 4020], 4032)

        self.assertTrue(result.valid)
        self.assertEqual(result.nearest_tp, 4025)

    def test_now_price_outside_valid_structure_rejected(self):
        result = validate_now_price("BUY", 4027, [4039], 4045)

        self.assertFalse(result.valid)
        self.assertIn("NOW executable price invalid for BUY", result.reason)


class ParserDirectionTests(unittest.TestCase):

    def test_example_a_buy_valid(self):
        signal = parse_signal(
            """
            XAUUSD buy NOW 4035 - 4031
            TP1 4039
            TP2 4043
            TP3 4047
            TP4 4052
            SL 4027
            """
        )

        self.assertTrue(validate_signal(signal))
        self.assertEqual(signal.textual_direction, "BUY")
        self.assertEqual(signal.inferred_direction, "BUY")
        self.assertEqual(signal.final_side, "BUY")
        self.assertFalse(signal.direction_conflict)

    def test_example_b_textual_sell_final_buy(self):
        signal = parse_signal(
            """
            XAUUSD sell NOW 4035 - 4031
            TP1 4039
            TP2 4043
            TP3 4047
            SL 4027
            """
        )

        self.assertTrue(validate_signal(signal))
        self.assertEqual(signal.textual_direction, "SELL")
        self.assertEqual(signal.inferred_direction, "BUY")
        self.assertEqual(signal.final_side, "BUY")
        self.assertTrue(signal.direction_conflict)

    def test_example_c_textual_buy_final_sell(self):
        signal = parse_signal(
            """
            XAUUSD buy NOW 4031 - 4035
            TP1 4025
            TP2 4020
            SL 4040
            """
        )

        self.assertTrue(validate_signal(signal))
        self.assertEqual(signal.inferred_direction, "SELL")
        self.assertEqual(signal.final_side, "SELL")
        self.assertTrue(signal.direction_conflict)

    def test_example_e_entry_geometry_invalid(self):
        signal = parse_signal(
            """
            XAUUSD BUY
            Entry 4040
            SL 4027
            TP1 4035
            """
        )

        self.assertFalse(validate_signal(signal))
        self.assertEqual(signal.inferred_direction, "BUY")
        self.assertIsNone(signal.final_side)
        self.assertIn("Entry geometry invalid", signal.validation_error)


class SignalProcessorDirectionTests(unittest.TestCase):

    def _process_and_capture_audit(
        self,
        text,
        auto_execute=False,
        message_id=1021,
    ):
        with self.assertLogs(signal_processor.logger, level="INFO") as captured, \
            patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(signal_processor, "is_processed", return_value=False), \
            patch.object(signal_processor, "is_auto_execute", return_value=auto_execute), \
            patch.object(signal_processor, "mark_signal_received"), \
            patch.object(signal_processor, "mark_processed"), \
            patch.object(signal_processor, "mark_trade_executed"), \
            patch.object(signal_processor, "notify_signal"), \
            patch.object(signal_processor, "notify_direction_conflict"), \
            patch.object(signal_processor, "notify_dry_run"), \
            patch.object(
                signal_processor,
                "execute_signal",
                return_value={"opened": 0},
            ) as execute_signal:
            asyncio.run(
                signal_processor.process_new_message(
                    event(text, message_id=message_id)
                )
            )

        audit_lines = [
            entry.split("PARSED SIGNAL | ", 1)[1]
            for entry in captured.output
            if "PARSED SIGNAL | " in entry
        ]

        self.assertEqual(len(audit_lines), 1)
        return audit_lines[0], captured.output, execute_signal

    def test_explicit_xauusd_signal_audit(self):
        audit, _output, execute_signal = self._process_and_capture_audit(
            """
            XAUUSD SELL
            4020-4025
            SL 4030
            TP1 4010
            TP2 4005
            TP3 4000
            """,
            auto_execute=True,
        )

        self.assertEqual(
            audit,
            "ChatID=1 Message=1021 Symbol=XAUUSD.s SymbolSource=explicit "
            "TextDirection=SELL InferredDirection=SELL FinalSide=SELL "
            "Conflict=False EntryLow=4020.0 EntryHigh=4025.0 NOW=False "
            "SL=4030.0 TPs=[4010.0, 4005.0, 4000.0] Mode=live",
        )
        execute_signal.assert_called_once()

    def test_defaulted_symbol_less_signal_audit(self):
        audit, _output, _execute_signal = self._process_and_capture_audit(
            """
            4020-4025
            SL 4030
            TP1 4010
            TP2 4005
            """
        )

        self.assertIn("Symbol=XAUUSD.s", audit)
        self.assertIn("SymbolSource=default_xauusd_complete_signal", audit)
        self.assertIn("FinalSide=SELL", audit)
        self.assertIn("Mode=dry_run", audit)

    def test_decimal_comma_signal_audit(self):
        audit, _output, _execute_signal = self._process_and_capture_audit(
            """
            XAUUSD
            4020,5-4025,5
            SL 4030,5
            TP1 4010,25
            """
        )

        self.assertIn("EntryLow=4020.5", audit)
        self.assertIn("EntryHigh=4025.5", audit)
        self.assertIn("SL=4030.5", audit)
        self.assertIn("TPs=[4010.25]", audit)

    def test_textual_direction_conflict_audit(self):
        audit, _output, _execute_signal = self._process_and_capture_audit(
            """
            XAUUSD SELL NOW 4035 - 4031
            TP1 4039
            SL 4027
            """
        )

        self.assertIn("TextDirection=SELL", audit)
        self.assertIn("InferredDirection=BUY", audit)
        self.assertIn("FinalSide=BUY", audit)
        self.assertIn("Conflict=True", audit)

    def test_dry_run_audit(self):
        audit, output, _execute_signal = self._process_and_capture_audit(
            """
            XAUUSD BUY
            Entry 4031
            TP1 4039
            SL 4027
            """
        )

        self.assertIn("Mode=dry_run", audit)
        self.assertTrue(any("DRY RUN" in entry for entry in output))

    def test_audit_logging_sends_no_mt5_order_in_dry_run(self):
        audit, _output, execute_signal = self._process_and_capture_audit(
            """
            XAUUSD BUY
            Entry 4031
            TP1 4039
            SL 4027
            """
        )

        self.assertIn("Mode=dry_run", audit)
        execute_signal.assert_not_called()

    def test_direction_only_correction_ignored(self):
        with patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(signal_processor, "notify_error") as notify_error, \
            patch.object(signal_processor, "execute_signal") as execute_signal, \
            patch.object(signal_processor, "mark_signal_received") as mark_received:
            asyncio.run(
                signal_processor.process_new_message(
                    event("sorry, it is BUY")
                )
            )

        notify_error.assert_not_called()
        execute_signal.assert_not_called()
        mark_received.assert_not_called()

    def test_dry_run_uses_inferred_direction_but_sends_no_mt5_order(self):
        text = """
        XAUUSD sell NOW 4035 - 4031
        TP1 4039
        SL 4027
        """

        with patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(signal_processor, "is_processed", return_value=False), \
            patch.object(signal_processor, "is_auto_execute", return_value=False), \
            patch.object(signal_processor, "mark_signal_received"), \
            patch.object(signal_processor, "mark_processed"), \
            patch.object(signal_processor, "notify_signal"), \
            patch.object(signal_processor, "notify_direction_conflict"), \
            patch.object(signal_processor, "notify_dry_run") as notify_dry_run, \
            patch.object(
                signal_processor,
                "execute_signal",
                return_value={"opened": 0},
            ) as execute_signal:
            asyncio.run(signal_processor.process_new_message(event(text)))

        dry_signal = notify_dry_run.call_args.args[0]
        self.assertEqual(dry_signal.textual_direction, "SELL")
        self.assertEqual(dry_signal.final_side, "BUY")
        execute_signal.assert_not_called()

    def test_dry_run_matching_direction_still_skips_execution(self):
        text = """
        XAUUSD BUY NOW 4035 - 4031
        TP1 4039
        SL 4027
        """

        with patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(signal_processor, "is_processed", return_value=False), \
            patch.object(signal_processor, "is_auto_execute", return_value=False), \
            patch.object(signal_processor, "mark_signal_received"), \
            patch.object(signal_processor, "mark_processed") as mark_processed, \
            patch.object(signal_processor, "notify_signal"), \
            patch.object(signal_processor, "notify_direction_conflict") as notify_conflict, \
            patch.object(signal_processor, "notify_dry_run") as notify_dry_run, \
            patch.object(signal_processor, "execute_signal") as execute_signal:
            asyncio.run(signal_processor.process_new_message(event(text)))

        dry_signal = notify_dry_run.call_args.args[0]
        self.assertEqual(dry_signal.final_side, "BUY")
        notify_conflict.assert_not_called()
        execute_signal.assert_not_called()
        mark_processed.assert_called_once_with(100)

    def test_live_now_price_outside_structure_executes_at_market(self):
        text = """
        XAUUSD BUY NOW
        TP1 4039
        SL 4027
        """

        with patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(signal_processor, "is_processed", return_value=False), \
            patch.object(signal_processor, "is_auto_execute", return_value=True), \
            patch.object(signal_processor, "mark_signal_received"), \
            patch.object(signal_processor, "mark_processed") as mark_processed, \
            patch.object(signal_processor, "notify_signal"), \
            patch.object(signal_processor, "notify_error") as notify_error, \
            patch.object(
                signal_processor,
                "execute_signal",
                return_value={"opened": 0},
            ) as execute_signal:
            asyncio.run(signal_processor.process_new_message(event(text)))

        notify_error.assert_not_called()
        execute_signal.assert_called_once()
        mark_processed.assert_called_once_with(100)

    def test_edited_levels_recalculate_direction_before_execution(self):
        text = """
        XAUUSD BUY
        TP1 4025
        SL 4040
        """

        with patch.object(signal_processor, "notify_edit"), \
            patch.object(signal_processor, "notify_direction_conflict"), \
            patch.object(signal_processor, "synchronize_trade") as sync:
            asyncio.run(signal_processor.process_edited_message(event(text)))

        edited_signal = sync.call_args.args[0]
        self.assertEqual(edited_signal.textual_direction, "BUY")
        self.assertEqual(edited_signal.inferred_direction, "SELL")
        self.assertEqual(edited_signal.final_side, "SELL")


class SynchronizerDirectionTests(unittest.TestCase):

    def test_direction_changing_edit_after_execution_marks_manual_intervention(self):
        trade = {
            "chat_id": 1,
            "message_id": 100,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "sl": 4027,
            "raw_message": "raw",
            "positions": [
                {"ticket": 900, "closed": False, "tp": 4039},
            ],
        }
        signal = parse_signal(
            """
            XAUUSD BUY
            TP1 4025
            SL 4040
            """
        )
        signal.chat_id = 1
        signal.message_id = 100

        with patch.object(synchronizer, "get_trade", return_value=trade), \
            patch.object(synchronizer, "modify_trade") as modify_trade, \
            patch.object(synchronizer, "update_trade") as update_trade, \
            patch.object(synchronizer, "notify_manual_intervention") as notify_manual:
            synchronizer.synchronize_trade(signal)

        modify_trade.assert_not_called()
        update_trade.assert_called_once_with(trade)
        notify_manual.assert_called_once()
        self.assertTrue(trade["requires_manual_intervention"])
        self.assertEqual(trade["edited_final_side"], "SELL")

    def test_old_stored_trade_side_only_remains_readable(self):
        trade = {
            "chat_id": 1,
            "message_id": 100,
            "symbol": "XAUUSD.s",
            "side": "BUY",
            "sl": 4027,
            "raw_message": "raw",
            "positions": [
                {"ticket": 900, "closed": False, "tp": 4039},
            ],
        }
        signal = parse_signal(
            """
            XAUUSD BUY
            TP1 4041
            SL 4025
            """
        )
        signal.chat_id = 1
        signal.message_id = 100

        with patch.object(synchronizer, "get_trade", return_value=trade), \
            patch.object(
                synchronizer,
                "modify_trade",
                return_value={"success": True},
            ) as modify_trade, \
            patch.object(synchronizer, "update_trade") as update_trade, \
            patch.object(synchronizer, "notify_manual_intervention") as notify_manual:
            synchronizer.synchronize_trade(signal)

        modify_trade.assert_called_once_with(ticket=900, sl=4025.0, tp=4041.0)
        update_trade.assert_called_once_with(trade)
        notify_manual.assert_not_called()
        self.assertEqual(trade["final_side"], "BUY")


class ExecutorDirectionTests(unittest.TestCase):

    def _signal(self, **overrides):
        values = {
            "chat_id": 1,
            "message_id": 100,
            "symbol": "XAUUSD.s",
            "side": None,
            "textual_direction": None,
            "inferred_direction": None,
            "final_side": None,
            "direction_source": None,
            "direction_conflict": False,
            "entry_low": None,
            "entry_high": None,
            "sl": 4027,
            "tps": [4039],
            "raw": "raw",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _open_result(
        self,
        symbol,
        side,
        sl,
        tp,
        *,
        excluded_position_tickets=None,
        excluded_position_identifiers=None,
        identity_poll_delays=None,
    ):
        self.assertIsNone(identity_poll_delays)

        return {
            "success": True,
            "ticket": 900,
            "position_ticket": 900,
            "position_identifier": 500,
            "order_ticket": 100,
            "deal_ticket": 200,
            "symbol": symbol,
            "side": side,
            "volume": 0.01,
            "requested_price": 4032,
            "fill_price": 4032,
            "price_open": 4032,
            "price": 4032,
            "entry_source": "position_price_open",
            "sl": sl,
            "magic": MAGIC_NUMBER,
            "comment": COMMENT,
        }

    def _execute_with_mock_open(self, signal):
        with patch.object(executor, "open_trade", side_effect=self._open_result) as open_trade, \
            patch.object(executor, "load_trades", return_value=[]), \
            patch.object(executor, "add_trade"), \
            patch.object(executor, "notify_success"), \
            patch.object(executor, "notify_error") as notify_error:
            summary = executor.execute_signal(signal)

        return summary, open_trade, notify_error

    def test_side_sell_with_buy_shaped_levels_executes_buy(self):
        signal = self._signal(side="SELL", sl=4027, tps=[4039])

        summary, open_trade, notify_error = self._execute_with_mock_open(signal)

        self.assertEqual(summary["opened"], 1)
        open_trade.assert_called_once_with("XAUUSD.s", "BUY", 4027, 4039)
        notify_error.assert_not_called()

    def test_side_buy_with_sell_shaped_levels_executes_sell(self):
        signal = self._signal(side="BUY", sl=4040, tps=[4025])

        summary, open_trade, notify_error = self._execute_with_mock_open(signal)

        self.assertEqual(summary["opened"], 1)
        open_trade.assert_called_once_with("XAUUSD.s", "SELL", 4040, 4025)
        notify_error.assert_not_called()

    def test_side_only_signal_without_valid_levels_is_rejected(self):
        signal = self._signal(side="SELL", sl=None, tps=[])

        with patch.object(executor, "open_trade") as open_trade, \
            patch.object(executor, "notify_error") as notify_error:
            summary = executor.execute_signal(signal)

        self.assertEqual(summary["opened"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertIsNone(summary["trade"])
        open_trade.assert_not_called()
        notify_error.assert_called_once()

    def test_stale_final_side_cannot_determine_execution(self):
        signal = self._signal(
            side="SELL",
            inferred_direction="SELL",
            final_side="SELL",
            sl=4027,
            tps=[4039],
        )

        summary, open_trade, notify_error = self._execute_with_mock_open(signal)

        self.assertEqual(summary["opened"], 1)
        open_trade.assert_called_once_with("XAUUSD.s", "BUY", 4027, 4039)
        notify_error.assert_not_called()

    def test_invalid_mixed_levels_call_no_open_trade(self):
        signal = self._signal(side="BUY", sl=4035, tps=[4030, 4040])

        with patch.object(executor, "open_trade") as open_trade, \
            patch.object(executor, "notify_error") as notify_error:
            summary = executor.execute_signal(signal)

        self.assertEqual(summary["opened"], 0)
        open_trade.assert_not_called()
        notify_error.assert_called_once()

    def test_valid_parsed_signal_executes_normally(self):
        signal = parse_signal(
            """
            XAUUSD BUY NOW 4035 - 4031
            TP1 4039
            SL 4027
            """
        )
        signal.chat_id = 1
        signal.message_id = 100

        summary, open_trade, notify_error = self._execute_with_mock_open(signal)

        self.assertEqual(summary["opened"], 1)
        open_trade.assert_called_once_with("XAUUSD.s", "BUY", 4027.0, 4039.0)
        notify_error.assert_not_called()

    def test_later_tp_calls_exclude_positions_opened_earlier_in_execution(self):
        signal = self._signal(side="BUY", sl=4027, tps=[4039, 4043, 4047])
        opened_tickets = []

        def fake_open(
            symbol,
            side,
            sl,
            tp,
            *,
            excluded_position_tickets=None,
            excluded_position_identifiers=None,
            identity_poll_delays=None,
        ):
            self.assertIsNone(identity_poll_delays)
            index = len(opened_tickets)
            ticket = 900 + index
            identifier = 500 + index
            opened_tickets.append(ticket)
            return {
                "success": True,
                "ticket": ticket,
                "position_ticket": ticket,
                "position_identifier": identifier,
                "order_ticket": 100 + index,
                "deal_ticket": 200 + index,
                "symbol": symbol,
                "side": side,
                "volume": 0.01,
                "requested_price": 4032,
                "fill_price": 4032,
                "price_open": 4032,
                "price": 4032,
                "entry_source": "position_price_open",
                "sl": sl,
                "magic": MAGIC_NUMBER,
                "comment": COMMENT,
            }

        with patch.object(executor, "open_trade", side_effect=fake_open) as open_trade, \
            patch.object(executor, "load_trades", return_value=[]), \
            patch.object(executor, "add_trade"), \
            patch.object(executor, "notify_success"), \
            patch.object(executor, "notify_error") as notify_error:
            summary = executor.execute_signal(signal)

        self.assertEqual(summary["opened"], 3)
        self.assertEqual(open_trade.call_count, 3)
        notify_error.assert_not_called()

        first_kwargs = open_trade.call_args_list[0].kwargs
        second_kwargs = open_trade.call_args_list[1].kwargs
        third_kwargs = open_trade.call_args_list[2].kwargs

        self.assertNotIn("excluded_position_tickets", first_kwargs)
        self.assertEqual(second_kwargs["excluded_position_tickets"], {900})
        self.assertEqual(third_kwargs["excluded_position_tickets"], {900, 901})
        self.assertEqual(second_kwargs["excluded_position_identifiers"], {500})
        self.assertEqual(third_kwargs["excluded_position_identifiers"], {500, 501})

    def test_executor_never_uses_textual_direction_directly(self):
        signal = SimpleNamespace(
            chat_id=1,
            message_id=100,
            symbol="XAUUSD.s",
            side="SELL",
            textual_direction="SELL",
            inferred_direction="BUY",
            final_side="BUY",
            direction_source="sl_tp_geometry",
            direction_conflict=True,
            sl=4027,
            tps=[4039],
            raw="raw",
        )

        def fake_open(
            symbol,
            side,
            sl,
            tp,
            *,
            excluded_position_tickets=None,
            excluded_position_identifiers=None,
            identity_poll_delays=None,
        ):
            self.assertIsNone(identity_poll_delays)

            return {
                "success": True,
                "ticket": 900,
                "position_ticket": 900,
                "position_identifier": 500,
                "order_ticket": 100,
                "deal_ticket": 200,
                "symbol": symbol,
                "side": side,
                "volume": 0.01,
                "requested_price": 4032,
                "fill_price": 4032,
                "price_open": 4032,
                "price": 4032,
                "entry_source": "position_price_open",
                "sl": sl,
                "magic": MAGIC_NUMBER,
                "comment": COMMENT,
            }

        with patch.object(executor, "open_trade", side_effect=fake_open) as open_trade, \
            patch.object(executor, "load_trades", return_value=[]), \
            patch.object(executor, "add_trade"), \
            patch.object(executor, "notify_success"), \
            patch.object(executor, "notify_error"):
            summary = executor.execute_signal(signal)

        self.assertEqual(summary["opened"], 1)
        open_trade.assert_called_once_with("XAUUSD.s", "BUY", 4027, 4039)


if __name__ == "__main__":
    unittest.main()
