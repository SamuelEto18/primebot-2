# OBSOLETE: Manual parser smoke script. Keep parser behavior unchanged.
from core.parser import parse_signal

signal = """

XAUUSD SELL NOW 4064 - 4068

TP1 - 4061
TP2 - 4057
TP3 - 4053
TP4 - 4049

SL:4074

"""

parsed = parse_signal(signal)

print(parsed)