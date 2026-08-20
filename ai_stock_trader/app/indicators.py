"""순수 파이썬 지표 계산.

pandas/numpy 버전에 따라 결과가 흔들리는 걸 피하려고 의존성 없이 직접 구현한다.
모든 함수는 candles(오래된 것 -> 최신 순) 리스트를 받고, 같은 길이의 리스트를 돌려준다.
값이 없는 구간은 None.
"""
from __future__ import annotations


def closes(candles: list[dict]) -> list[float]:
    return [float(c["close"]) for c in candles]


def highs(candles: list[dict]) -> list[float]:
    return [float(c["high"]) for c in candles]


def lows(candles: list[dict]) -> list[float]:
    return [float(c["low"]) for c in candles]


def volumes(candles: list[dict]) -> list[float]:
    return [float(c.get("volume") or 0) for c in candles]


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(candles: list[dict], period: int = 14) -> list[float | None]:
    """Wilder ATR - 손절폭 계산의 기준."""
    n = len(candles)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = [float(candles[0]["high"]) - float(candles[0]["low"])]
    for i in range(1, n):
        h, l = float(candles[i]["high"]), float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    prev = sum(trs[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def stdev(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period or period < 2:
        return out
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        m = sum(w) / period
        out[i] = (sum((x - m) ** 2 for x in w) / period) ** 0.5
    return out


def highest(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = max(values[i - period + 1:i + 1])
    return out


def lowest(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = min(values[i - period + 1:i + 1])
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(values, fast), ema(values, slow)
    line = [(a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, signal)
    sig: list[float | None] = [None] * len(line)
    j = 0
    for i, v in enumerate(line):
        if v is not None:
            sig[i] = sig_valid[j]
            j += 1
    hist = [(a - b) if (a is not None and b is not None) else None for a, b in zip(line, sig)]
    return line, sig, hist


def last(values: list, default=None):
    for v in reversed(values):
        if v is not None:
            return v
    return default
