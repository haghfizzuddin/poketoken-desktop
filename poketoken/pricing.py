"""Per-token pricing (USD per token).

Port of upstream `ModelPricing.swift`, updated for the Claude 5 family. Unlike upstream,
cache writes are split by TTL because Claude Code logs carry the 5m/1h breakdown:
5-minute writes cost 1.25x input, 1-hour writes cost 2x input.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float

    @staticmethod
    def per_million(i: float, o: float, cw5: float, cw1: float, cr: float) -> "Rate":
        m = 1_000_000
        return Rate(i / m, o / m, cw5 / m, cw1 / m, cr / m)


ZERO = Rate(0, 0, 0, 0, 0)

_FABLE_51 = Rate.per_million(10, 50, 12.5, 20, 0.25)   # cache reads are 0.025x on Fable 5.1
_FABLE_5 = Rate.per_million(10, 50, 12.5, 20, 1.0)
_OPUS = Rate.per_million(5, 25, 6.25, 10, 0.5)
_SONNET_5 = Rate.per_million(2, 10, 2.5, 4, 0.2)
_SONNET_46 = Rate.per_million(3, 15, 3.75, 6, 0.3)
_HAIKU = Rate.per_million(1, 5, 1.25, 2, 0.1)

TABLE: dict[str, Rate] = {
    "claude-fable-5-1": _FABLE_51,
    "claude-mythos-5-1": _FABLE_51,
    "claude-fable-5": _FABLE_5,
    "claude-mythos-5": _FABLE_5,
    "claude-opus-5": _OPUS,
    "claude-opus-4-8": _OPUS,
    "claude-opus-4-7": _OPUS,
    "claude-opus-4-6": _OPUS,
    "claude-sonnet-5": _SONNET_5,
    "claude-sonnet-4-6": _SONNET_46,
    "claude-haiku-4-5": _HAIKU,
    "claude-haiku-4-5-20251001": _HAIKU,
}


def rate(model: str) -> Rate:
    """Exact match first, then family fallback (version drift), else zero (unpriced)."""
    r = TABLE.get(model)
    if r is not None:
        return r
    m = model.lower()
    if m.startswith("grok") or m.startswith("antigravity/"):
        return ZERO
    if "fable-5-1" in m or "mythos-5-1" in m:
        return _FABLE_51
    if "fable" in m or "mythos" in m:
        return _FABLE_5
    if "opus" in m:
        return _OPUS
    if "sonnet-5" in m:
        return _SONNET_5
    if "sonnet" in m:
        return _SONNET_46
    if "haiku" in m:
        return _HAIKU
    return ZERO


def cost(model: str, input: int, output: int, cache_write_5m: int, cache_write_1h: int, cache_read: int) -> float:
    r = rate(model)
    return (input * r.input + output * r.output + cache_write_5m * r.cache_write_5m
            + cache_write_1h * r.cache_write_1h + cache_read * r.cache_read)
