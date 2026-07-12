from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Position:
    ticket: int
    tp: float
    entry: float
    closed: bool = False
    break_even: bool = False


@dataclass
class Signal:
    symbol: str
    side: Optional[str]

    entry_low: Optional[float]
    entry_high: Optional[float]

    sl: Optional[float]
    tps: List[float]

    raw: str

    textual_direction: Optional[str] = None
    inferred_direction: Optional[str] = None
    final_side: Optional[str] = None
    direction_source: Optional[str] = None
    direction_conflict: bool = False
    direction_error: Optional[str] = None
    validation_error: Optional[str] = None
    now_signal: bool = False
    symbol_source: Optional[str] = None

    message_id: int = 0
    chat_id: int = 0
    
    edited: bool = False

    positions: List[Position] = field(default_factory=list)
