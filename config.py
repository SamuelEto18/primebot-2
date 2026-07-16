LOT_SIZE = 0.01

SYMBOL_SUFFIX = ".s"

MAGIC_NUMBER = 987655

DEVIATION = 20

COMMENT = "PrimeBot2"

# PrimeBot 2 profitable break-even sticker identity
PRIMEBOT2_TELEGRAM_CHANNEL_ID = -1002275473775
APPROVED_BE_STICKER_DOCUMENT_IDS = frozenset({5422500716344283971})
PROFITABLE_BREAK_EVEN_OFFSET = 1.00
PROFITABLE_BREAK_EVEN_SYMBOL = "XAUUSD.s"

# Safety switch
AUTO_EXECUTE = False

# Automatically move remaining trades to BE after TP1
MOVE_TO_BREAK_EVEN = True

# Allowed trading symbols
ALLOWED_SYMBOLS = [
    "XAUUSD.s",
    "BTCUSD"
]

# Logging
LOG_SIGNALS = True
LOG_EXECUTION = True
LOG_EDITS = True
LOG_BREAK_EVEN = True

# Maximum number of positions to open from one signal
MAX_POSITIONS = 10
