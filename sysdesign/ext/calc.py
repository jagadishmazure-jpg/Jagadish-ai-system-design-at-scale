"""Deterministic calculations exposed as plain tools. Money math never goes through a model."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TAX_RATES = {"default": Decimal("0.0725")}


def _money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def refund_amount(item_price: float, shipping: float, reason: str, restocking_pct: float = 10.0) -> float:
    """Damaged/defective: price + shipping. Changed mind: price minus restocking fee, no shipping."""
    if item_price < 0 or shipping < 0 or not 0 <= restocking_pct <= 100:
        raise ValueError("negative or out-of-range input")
    price = _money(item_price)
    if reason in ("damaged", "not_as_described"):
        return float(price + _money(shipping))
    fee = _money(price * Decimal(str(restocking_pct)) / 100)
    return float(price - fee)


def tax(amount: float, region: str = "default") -> float:
    if amount < 0:
        raise ValueError("negative amount")
    return float(_money(_money(amount) * TAX_RATES.get(region, TAX_RATES["default"])))
