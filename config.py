LOT_SIZE = 0.01

SYMBOL_SUFFIX = ".s"

MAGIC_NUMBER = 987655

DEVIATION = 20

COMMENT = "PrimeBot2"

# PrimeBot 2 Telegram source identity. CHANNEL_ID must match this value.
PRIMEBOT2_TELEGRAM_CHANNEL_ID = -1002792547449

# PrimeBot 2 sticker-management values. The exact Telegram document IDs are
# supplied through the environment; empty allowlists disable both commands.
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
