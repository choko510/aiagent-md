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
_ISO_CODE: Final = re.compile(r"\b(JPY|USD|EUR|GBP|CNY|CAD|AUD|CHF|KRW)\b", re.IGNORECASE)
_UNIT_CURRENCY: Final[dict[str, str]] = {"円": "JPY", "ドル": "USD", "ユーロ": "EUR"}
_AMOUNT = re.compile(
    r"(?P<sign>[+\-−△])?\s*(?P<symbol>[¥￥$€£])?\s*"
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,3})?)(?P<unit>円|ドル|ユーロ)?"
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
    match = _AMOUNT.fullmatch(text)
    if not match:
        return MoneyParse(raw, None, None, "INVALID_AMOUNT_FORMAT")
    symbol = match.group("symbol")
    sign = match.group("sign")
    number = match.group("number")
    unit = match.group("unit")
    symbol_currency = _CURRENCY_SYMBOL.get(symbol or "")
    unit_currency = _UNIT_CURRENCY.get(unit or "")
    if symbol_currency is not None and unit_currency is not None and symbol_currency != unit_currency:
        return MoneyParse(raw, None, None, "CONFLICTING_CURRENCY", ambiguous=True)
    currency: str | None = symbol_currency or unit_currency
    codes = {code.upper() for code in _ISO_CODE.findall(near_text)}
    if len(codes) > 1:
        return MoneyParse(raw, None, None, "AMBIGUOUS_CURRENCY", ambiguous=True)
    if codes:
        explicit = next(iter(codes))
        if currency is not None and currency != explicit:
            return MoneyParse(raw, None, None, "CONFLICTING_CURRENCY", ambiguous=True)
        currency = explicit
    if currency is None:
        # locale is a display hint, not evidence of the source currency. A caller must
        # provide an explicit symbol/unit/ISO code in the value's nearby text.
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
    digits = normalized.replace(".", "")
    if len(digits) > 15:
        return MoneyParse(raw, None, currency, "NUMBER_TOO_LARGE")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return MoneyParse(raw, None, currency, "INVALID_NUMBER")
    if not value.is_finite():
        return MoneyParse(raw, None, currency, "INVALID_NUMBER")
    if sign in {"-", "−", "△"}:
        value = -value
    scale = _DECIMALS.get(currency, 2)
    try:
        quantized = value.quantize(Decimal(1).scaleb(-scale))
    except InvalidOperation:
        return MoneyParse(raw, None, currency, "INVALID_NUMBER")
    if quantized != value:
        return MoneyParse(raw, None, currency, "SUBPENNY_PRECISION")
    return MoneyParse(raw, format(quantized, "f"), currency, None)
