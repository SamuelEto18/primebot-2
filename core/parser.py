import re

from core.direction import infer_direction
from core.models import Signal


NUMBER_TOKEN_PATTERN = r"\d+(?:[.,]\d+)?"
NUMBER_PATTERN = rf"(?<![\d.,]){NUMBER_TOKEN_PATTERN}(?![\d.,])"
UNSUPPORTED_SYMBOL_PATTERN = r"\b[A-Z]{3,6}USD(?:\.[A-Z0-9]+)?\b"


def parse_number(value):
    token = str(value).strip()

    if not re.fullmatch(NUMBER_TOKEN_PATTERN, token):
        return None

    if token.count(".") + token.count(",") > 1:
        return None

    try:
        return float(token.replace(",", "."))
    except ValueError:
        return None


def _extract_symbol(text):
    if re.search(r"\bBTCUSD(?:\.[A-Z0-9]+)?\b", text):
        return "BTCUSD", "explicit"

    if re.search(r"\bXAUU?USD(?:\.[A-Z0-9]+)?\b", text):
        return "XAUUSD.s", "explicit"

    if re.search(UNSUPPORTED_SYMBOL_PATTERN, text):
        return None, "explicit_unsupported"

    return None, None


def _first_direction(text):
    candidates = []
    patterns = [
        (r"\bBUY\b", "BUY"),
        (r"\bSELL\b", "SELL"),
        (r"\bBUY\s+SIGNAL\b", "BUY"),
        (r"\bSELL\s+SIGNAL\b", "SELL"),
        (r"\bCUMPĂRAȚI\b", "BUY"),
        (r"\bCUMPARATI\b", "BUY"),
        (r"\bVINDE\b", "SELL"),
    ]

    for pattern, direction in patterns:
        match = re.search(pattern, text)

        if match:
            candidates.append((match.start(), direction))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _extract_stop_loss(text):
    patterns = [
        rf"SL[:\-\s]+({NUMBER_PATTERN})",
        rf"STOP LOSS[:@\.\-\s]+({NUMBER_PATTERN})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            return parse_number(match.group(1))

    return None


def _extract_take_profits(text):
    tps = []
    patterns = [
        rf"TP\d*[:\-\.\s]+({NUMBER_PATTERN})",
        rf"TAKE PROFIT\s*[→:\-\.\s]+({NUMBER_PATTERN})",
    ]

    for pattern in patterns:
        for value in re.findall(pattern, text):
            tp = parse_number(value)

            if tp is not None and tp not in tps:
                tps.append(tp)

    return tps


def _entry_candidate_lines(text):
    for line in text.splitlines():
        normalized = line.strip()

        if not normalized:
            continue

        if re.search(r"\b(TP\d*|TAKE PROFIT|SL|STOP LOSS)\b", normalized):
            continue

        yield normalized


def _extract_entry(text):
    for line in _entry_candidate_lines(text):
        range_match = re.search(
            rf"({NUMBER_PATTERN})\s*-\s*({NUMBER_PATTERN})",
            line
        )

        if range_match:
            first = parse_number(range_match.group(1))
            second = parse_number(range_match.group(2))

            if first is not None and second is not None:
                return first, second

    for line in _entry_candidate_lines(text):
        entry_match = re.search(
            rf"\bENTRY\s*[:@\-\s]+({NUMBER_PATTERN})\b",
            line
        )

        if entry_match:
            entry = parse_number(entry_match.group(1))

            if entry is not None:
                return entry, entry

        now_match = re.search(
            rf"\bNOW\s+({NUMBER_PATTERN})\b",
            line
        )

        if now_match:
            entry = parse_number(now_match.group(1))

            if entry is not None:
                return entry, entry

    return None


def _should_default_xauusd(signal, direction_result):
    if signal.symbol is not None or signal.symbol_source is not None:
        return False

    has_entry_or_now = (
        signal.entry_low is not None
        or signal.entry_high is not None
        or signal.now_signal
    )

    return (
        has_entry_or_now
        and signal.sl is not None
        and len(signal.tps) > 0
        and direction_result.valid
        and direction_result.final_side in ("BUY", "SELL")
    )


def apply_direction_inference(signal):
    result = infer_direction(
        signal.sl,
        signal.tps,
        signal.entry_low,
        signal.entry_high,
        getattr(signal, "textual_direction", None),
    )

    signal.inferred_direction = result.inferred_direction
    signal.final_side = result.final_side
    signal.side = result.final_side
    signal.direction_source = result.direction_source
    signal.direction_conflict = result.direction_conflict
    signal.direction_error = result.reason
    signal.validation_error = result.reason

    if result.sl is not None:
        signal.sl = result.sl

    if result.tps:
        signal.tps = list(result.tps)

    if result.entry_low is not None or result.entry_high is not None:
        signal.entry_low = result.entry_low
        signal.entry_high = result.entry_high

    return result


def parse_signal(text: str):
    original = text
    text = text.upper()

    symbol, symbol_source = _extract_symbol(text)

    entry = _extract_entry(text)
    signal = Signal(
        symbol=symbol,
        side=None,
        entry_low=min(entry) if entry else None,
        entry_high=max(entry) if entry else None,
        sl=_extract_stop_loss(text),
        tps=_extract_take_profits(text),
        raw=original,
        textual_direction=_first_direction(text),
        now_signal=bool(re.search(r"\bNOW\b", text)),
        symbol_source=symbol_source,
    )

    direction_result = apply_direction_inference(signal)

    if _should_default_xauusd(signal, direction_result):
        signal.symbol = "XAUUSD.s"
        signal.symbol_source = "default_xauusd_complete_signal"

    return signal


def validate_signal(signal):
    result = apply_direction_inference(signal)

    if signal.symbol is None:
        signal.validation_error = "Missing or unsupported symbol"
        return False

    if signal.sl is None:
        signal.validation_error = "Missing SL"
        return False

    if len(signal.tps) == 0:
        signal.validation_error = "Missing TP"
        return False

    if not result.valid:
        signal.validation_error = result.reason
        return False

    return True
