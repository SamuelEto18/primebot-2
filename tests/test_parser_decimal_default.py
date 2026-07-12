import unittest

from core.parser import parse_number, parse_signal, validate_signal


class DecimalCommaParsingTests(unittest.TestCase):

    def test_decimal_comma_sl(self):
        signal = parse_signal("""
        XAUUSD
        Entry 4018
        SL 4010,5
        TP1 4022
        """)

        self.assertEqual(signal.sl, 4010.5)
        self.assertTrue(validate_signal(signal))

    def test_decimal_comma_tp(self):
        signal = parse_signal("""
        XAUUSD
        Entry 4018
        SL 4010
        TP1 4022,5
        """)

        self.assertEqual(signal.tps, [4022.5])
        self.assertTrue(validate_signal(signal))

    def test_decimal_comma_entry(self):
        signal = parse_signal("""
        XAUUSD
        Entry 4018,5
        SL 4010
        TP1 4022
        """)

        self.assertEqual(signal.entry_low, 4018.5)
        self.assertEqual(signal.entry_high, 4018.5)
        self.assertTrue(validate_signal(signal))

    def test_decimal_comma_entry_range(self):
        signal = parse_signal("""
        XAUUSD
        4018,5-4023,5
        SL 4028
        TP1 4014
        """)

        self.assertEqual(signal.entry_low, 4018.5)
        self.assertEqual(signal.entry_high, 4023.5)
        self.assertEqual(signal.final_side, "SELL")
        self.assertTrue(validate_signal(signal))

    def test_decimal_dot_values_unchanged(self):
        signal = parse_signal("""
        XAUUSD
        Entry 4018.5
        SL 4010.25
        TP1 4022.75
        """)

        self.assertEqual(signal.entry_low, 4018.5)
        self.assertEqual(signal.sl, 4010.25)
        self.assertEqual(signal.tps, [4022.75])
        self.assertTrue(validate_signal(signal))

    def test_mixed_integer_and_decimal_comma_values(self):
        signal = parse_signal("""
        XAUUSD
        4018-4023,5
        SL 4028
        TP1 4014,5
        """)

        self.assertEqual(signal.entry_low, 4018.0)
        self.assertEqual(signal.entry_high, 4023.5)
        self.assertEqual(signal.sl, 4028.0)
        self.assertEqual(signal.tps, [4014.5])
        self.assertTrue(validate_signal(signal))

    def test_malformed_numeric_values_rejected(self):
        malformed_values = [
            "4044,5,6",
            "4044.5.6",
            "4044,.5",
            "4044.,5",
        ]

        for value in malformed_values:
            with self.subTest(value=value):
                self.assertIsNone(parse_number(value))

        signal = parse_signal("""
        XAUUSD
        Entry 4018
        SL 4044,5,6
        TP1 4010
        """)

        self.assertIsNone(signal.sl)
        self.assertFalse(validate_signal(signal))

    def test_decimal_separator_examples(self):
        self.assertEqual(parse_number("4044,5"), 4044.5)
        self.assertEqual(parse_number("4044.5"), 4044.5)
        self.assertEqual(parse_number("0,01"), 0.01)
        self.assertEqual(parse_number("0.01"), 0.01)


class DefaultSymbolTests(unittest.TestCase):

    def test_complete_no_symbol_sell_signal_defaults_to_xauusd(self):
        signal = parse_signal("""
        4018-4023
        SL 4028
        TP1 4014
        TP2 4010
        TP3 4005
        """)

        self.assertEqual(signal.symbol, "XAUUSD.s")
        self.assertEqual(
            signal.symbol_source,
            "default_xauusd_complete_signal",
        )
        self.assertEqual(signal.final_side, "SELL")
        self.assertTrue(validate_signal(signal))

    def test_complete_no_symbol_buy_signal_defaults_to_xauusd(self):
        signal = parse_signal("""
        4018-4023
        SL 4010
        TP1 4028
        TP2 4032
        """)

        self.assertEqual(signal.symbol, "XAUUSD.s")
        self.assertEqual(
            signal.symbol_source,
            "default_xauusd_complete_signal",
        )
        self.assertEqual(signal.final_side, "BUY")
        self.assertTrue(validate_signal(signal))

    def test_no_symbol_now_signal_with_sl_tp_defaults_safely(self):
        signal = parse_signal("""
        BUY NOW
        SL 4010
        TP1 4020
        """)

        self.assertEqual(signal.symbol, "XAUUSD.s")
        self.assertEqual(
            signal.symbol_source,
            "default_xauusd_complete_signal",
        )
        self.assertTrue(signal.now_signal)
        self.assertEqual(signal.final_side, "BUY")
        self.assertTrue(validate_signal(signal))

    def test_sl_only_message_is_not_defaulted(self):
        signal = parse_signal("SL 4042")

        self.assertIsNone(signal.symbol)
        self.assertIsNone(signal.symbol_source)
        self.assertFalse(validate_signal(signal))

    def test_tp_only_message_is_not_defaulted(self):
        signal = parse_signal("TP 4057")

        self.assertIsNone(signal.symbol)
        self.assertIsNone(signal.symbol_source)
        self.assertFalse(validate_signal(signal))

    def test_direction_only_correction_is_not_defaulted(self):
        signal = parse_signal("sorry, it is BUY")

        self.assertIsNone(signal.symbol)
        self.assertIsNone(signal.symbol_source)
        self.assertFalse(validate_signal(signal))

    def test_explicit_btcusd_remains_btcusd(self):
        signal = parse_signal("""
        BTCUSD
        Entry 66000
        SL 65000
        TP1 67000
        """)

        self.assertEqual(signal.symbol, "BTCUSD")
        self.assertEqual(signal.symbol_source, "explicit")
        self.assertTrue(validate_signal(signal))

    def test_explicit_unsupported_symbol_is_rejected(self):
        signal = parse_signal("""
        EURUSD
        Entry 1.081
        SL 1.070
        TP1 1.090
        """)

        self.assertIsNone(signal.symbol)
        self.assertEqual(signal.symbol_source, "explicit_unsupported")
        self.assertFalse(validate_signal(signal))

    def test_invalid_mixed_geometry_is_not_defaulted(self):
        signal = parse_signal("""
        4018-4023
        SL 4020
        TP1 4014
        TP2 4030
        """)

        self.assertIsNone(signal.symbol)
        self.assertIsNone(signal.symbol_source)
        self.assertFalse(validate_signal(signal))


if __name__ == "__main__":
    unittest.main()
