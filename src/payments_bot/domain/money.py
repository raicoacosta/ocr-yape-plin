"""Money is always an integer number of minor units (céntimos), never a float."""

import re
from decimal import Decimal

from payments_bot.domain.errors import InvalidAmountError, InvalidCurrencyError

DEFAULT_CURRENCY = "PEN"
MINOR_UNITS_PER_MAJOR = 100
MAX_AMOUNT_MINOR = 10**11
"""Largest amount any single entry may carry: 1,000,000,000.00 in major units.

Far above any real receipt, and low enough that summing every entry can never overflow
the 64-bit integers the storage uses.
"""

_CANONICAL_AMOUNT = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def parse_amount(text: str) -> int:
    """Convert a canonical decimal string such as ``"40.50"`` into minor units (4050)."""
    if not _CANONICAL_AMOUNT.fullmatch(text):
        raise InvalidAmountError(f"not a canonical amount: {text!r}")
    return int(Decimal(text) * MINOR_UNITS_PER_MAJOR)


def validate_currency(code: str) -> None:
    """Reject anything that is not shaped like an ISO 4217 code."""
    if not _CURRENCY_CODE.fullmatch(code):
        raise InvalidCurrencyError(f"not an ISO 4217 currency code: {code!r}")
