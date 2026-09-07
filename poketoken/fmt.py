"""Number formatting — port of `TokenFormatter.swift`."""
from __future__ import annotations


def _trim(x: float, decimals: int) -> str:
    return f"{x:.{decimals}f}".rstrip("0").rstrip(".")


def compact(value: int) -> str:
    """987 -> '987', 12_345 -> '12.3K', 190_612_940 -> '190.6M', 1_240_000_000 -> '1.24B'."""
    a = abs(value)
    sign = "-" if value < 0 else ""
    if a < 1_000:
        return str(value)
    if a < 1_000_000:
        return sign + _trim(a / 1_000, 1) + "K"
    if a < 1_000_000_000:
        return sign + _trim(a / 1_000_000, 1) + "M"
    return sign + _trim(a / 1_000_000_000, 2) + "B"


def grouped(value: int) -> str:
    return f"{value:,}"


def cost(usd: float) -> str:
    return f"${usd:.2f}"


def cost_compact(usd: float) -> str:
    if usd < 100:
        return f"${usd:.1f}"
    if usd < 10_000:
        return f"${usd:.0f}"
    return f"${usd / 1_000:.1f}K"


def percent(value: float) -> str:
    return f"{value:.0f}%" if value == round(value) else f"{value:.1f}%"


def bar(fraction: float, width: int = 20) -> str:
    f = max(0.0, min(1.0, fraction))
    filled = int(round(f * width))
    return "█" * filled + "░" * (width - filled)
