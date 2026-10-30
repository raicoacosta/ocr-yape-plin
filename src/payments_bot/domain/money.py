"""Money is always an integer number of minor units (céntimos), never a float."""

import re

from payments_bot.domain.errors import InvalidAmountError, InvalidCurrencyError

DEFAULT_CURRENCY = "PEN"
MINOR_UNITS_PER_MAJOR = 100
MAX_AMOUNT_MINOR = 10**11
"""Largest amount any single entry may carry: 1,000,000,000.00 in major units.

Far above any real receipt, and low enough that summing every entry can never overflow
the 64-bit integers the storage uses.
"""

MAX_AMOUNT_INTEGER_DIGITS = 15
"""Longest integer part `parse_amount` accepts.

Room for every amount up to `MAX_AMOUNT_MINOR` and then some, while over-long text (a model
answer, a typo) is refused instead of being rounded or reaching `int()`'s digit limit.
"""

_CANONICAL_AMOUNT = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def parse_amount(text: str) -> int:
    """Convert a canonical decimal string such as ``"40.50"`` into minor units (4050).

    The conversion is exact integer arithmetic; text with more than
    `MAX_AMOUNT_INTEGER_DIGITS` integer digits is refused, never rounded.
    """
    match = _CANONICAL_AMOUNT.fullmatch(text)
    if match is None:
        raise InvalidAmountError(f"not a canonical amount: {_excerpt(text)}")
    major, minor = match.group(1), match.group(2) or ""
    if len(major) > MAX_AMOUNT_INTEGER_DIGITS:
        raise InvalidAmountError(
            f"amount has more than {MAX_AMOUNT_INTEGER_DIGITS} integer digits: {_excerpt(text)}"
        )
    return int(major) * MINOR_UNITS_PER_MAJOR + int(minor.ljust(2, "0"))


def validate_currency(code: str) -> None:
    """Reject anything that is not shaped like an ISO 4217 code."""
    if not _CURRENCY_CODE.fullmatch(code):
        raise InvalidCurrencyError(f"not an ISO 4217 currency code: {code!r}")


def _excerpt(text: str, limit: int = 40) -> str:
    return repr(text) if len(text) <= limit else f"{text[:limit]!r}... ({len(text)} characters)"
