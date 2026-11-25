"""Deterministic value normalization. Unknown is never replaced by zero.

Money is parsed as a decimal string, never a binary float. A separator whose
meaning depends on locale stays ambiguous; a currency symbol without a clear
locale/context is recorded as unknown rather than assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

NORMALIZER_VERSION: Final[str] = "1.0.0"

_CURRENCY_SYMBOL: Final[dict[str, str]] = {"¥": "JPY", "￥": "JPY", "$": "USD", "€": "EUR", "£": "GBP"}
_AMOUNT = re.compile(
    r"(?P<symbol>[¥￥$€£])?\s*(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,3})?)(?P<unit>円|ドル|ユーロ)?"
)
_TAX_INCLUDED = re.compile(r"税込|税込み|税込価格|内税|incl\.?\s*tax|tax\s*included", re.IGNORECASE)
_TAX_EXCLUDED = re.compile(r"税抜|税別|外税|excl\.?\s*tax|tax\s*excluded|\+\s*tax", re.IGNORECASE)
_DECIMALS: Final[dict[str, int]] = {"JPY": 0, "USD": 2, "EUR": 2, "GBP": 2}


@dataclass(frozen=True, slots=True)
class MoneyParse:
    raw: str
    amount: str | None
    currency: str | None
    reason: str | None
    ambiguous: bool = False


def tax_condition(text: str) -> str:
    included = bool(_TAX_INCLUDED.search(text))
    excluded = bool(_TAX_EXCLUDED.search(text))
    if included and not excluded:
        return "included"
    if excluded and not included:
        return "excluded"
    return "unknown"


def parse_money(raw: str, *, locale: str | None = None, near_text: str = "") -> MoneyParse:
    text = raw.strip()
    match = _AMOUNT.search(text)
    if not match:
        return MoneyParse(raw, None, None, "NO_NUMERIC_AMOUNT")
    symbol = match.group("symbol")
    number = match.group("number")
    unit = match.group("unit")
    currency: str | None = _CURRENCY_SYMBOL.get(symbol or "") or (
        {"円": "JPY", "ドル": "USD", "ユーロ": "EUR"}.get(unit or "")
    )
    if currency is None and locale:
        language = locale.split("-", 1)[0].lower()
        if language == "ja" or (language == "en" and "¥" in near_text):
            currency = "JPY"
    if currency is None:
        return MoneyParse(raw, None, None, "UNKNOWN_CURRENCY", ambiguous=True)
    if ("," in number and "." in number) or "," in number:
        normalized = number.replace(",", "")
    elif "." in number:
        parts = number.split(".")
        if len(parts[1]) == 3 and currency == "JPY":
            # 1.200 is three digits after the dot: in a JPY context this could
            # be a thousands separator or 1.2; do not decide silently.
            return MoneyParse(raw, None, currency, "AMBIGUOUS_NUMBER_FORMAT", ambiguous=True)
        normalized = number
    else:
        normalized = number
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return MoneyParse(raw, None, currency, "INVALID_NUMBER")
    if not value.is_finite() or value < 0:
        return MoneyParse(raw, None, currency, "INVALID_NUMBER")
    scale = _DECIMALS.get(currency, 2)
    quantized = value.quantize(Decimal(1).scaleb(-scale))
    if quantized != value:
        return MoneyParse(raw, None, currency, "SUBPENNY_PRECISION")
    return MoneyParse(raw, format(quantized, "f"), currency, None)
