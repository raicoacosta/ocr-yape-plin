"""Append-only ledger rules.

History is never edited: a correction is a new adjustment entry that references the
original payment, like an accounting counter-entry.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from payments_bot.domain.errors import (
    AlreadyVoidedError,
    CurrencyMismatchError,
    InvalidAmountError,
    InvalidCurrencyError,
    InvalidEntryError,
    MissingReasonError,
    NoChangeError,
    NotAPaymentError,
)
from payments_bot.domain.money import DEFAULT_CURRENCY, MAX_AMOUNT_MINOR, validate_currency

MAX_ENTRY_ID = 2**63 - 1
"""Largest entry id the ledger can hold (a 64-bit signed integer); ids start at 1."""
MAX_USER_ID = 2**63 - 1
"""Largest `created_by` the ledger can hold; user ids (Telegram's) start at 1."""
EARLIEST_STORABLE_YEAR = 1000
LATEST_STORABLE_YEAR = 9999
"""Timestamps are stored as UTC text with a four-digit year that cannot start with 0."""

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_FREE_TEXT_FIELDS = ("reason", "reference", "image_key", "extracted_json")


class EntryKind(StrEnum):
    PAYMENT = "payment"
    ADJUSTMENT = "adjustment"


class EntrySource(StrEnum):
    OCR = "ocr"
    MANUAL = "manual"
    IMPORT = "import"


@dataclass(frozen=True, slots=True, kw_only=True)
class Entry:
    """One immutable ledger line; `id` is None until the entry is persisted."""

    kind: EntryKind
    amount_minor: int
    source: EntrySource
    created_by: int
    created_at: datetime
    currency: str = DEFAULT_CURRENCY
    id: int | None = None
    corrects_id: int | None = None
    reason: str | None = None
    reference: str | None = None
    paid_at: datetime | None = None
    image_sha256: str | None = None
    image_key: str | None = None
    extracted_json: str | None = None

    def __post_init__(self) -> None:
        _validate_common(self)
        if self.kind is EntryKind.PAYMENT:
            _validate_payment(self)
        else:
            _validate_adjustment(self)


def ensure_adjusts(target: Entry, adjustment: Entry) -> None:
    """Raise unless `adjustment` is a valid counter-entry for the payment `target`."""
    _require_payment(target)
    if adjustment.kind is not EntryKind.ADJUSTMENT or adjustment.corrects_id != target.id:
        raise InvalidEntryError(f"entry {adjustment.id} does not adjust payment {target.id}")
    if adjustment.currency != target.currency:
        raise CurrencyMismatchError(
            f"adjustment in {adjustment.currency} cannot correct a {target.currency} payment"
        )


def effective_amount(payment: Entry, adjustments: Iterable[Entry]) -> int:
    """The payment amount after applying every adjustment that corrects it."""
    _require_payment(payment)
    total = payment.amount_minor
    for adjustment in adjustments:
        ensure_adjusts(payment, adjustment)
        total += adjustment.amount_minor
    return total


def build_correction(
    payment: Entry,
    adjustments: Sequence[Entry],
    *,
    new_amount_minor: int,
    reason: str,
    created_by: int,
    created_at: datetime,
) -> Entry:
    """Adjustment that moves the payment's effective amount to `new_amount_minor`."""
    _require_payment(payment)
    if payment.id is None:
        raise InvalidEntryError("a payment must be persisted before it can be corrected")
    clean_reason = _require_reason(reason)
    if isinstance(new_amount_minor, bool) or not isinstance(new_amount_minor, int):
        raise InvalidAmountError(
            f"a corrected amount must be integer minor units: {new_amount_minor!r}"
        )
    if new_amount_minor < 0:
        raise InvalidAmountError(f"a corrected amount cannot be negative: {new_amount_minor}")
    if new_amount_minor > MAX_AMOUNT_MINOR:
        raise InvalidAmountError(
            f"a corrected amount cannot exceed {MAX_AMOUNT_MINOR}: {new_amount_minor}"
        )
    delta = new_amount_minor - effective_amount(payment, adjustments)
    if delta == 0:
        raise NoChangeError(f"payment {payment.id} already amounts to {new_amount_minor}")
    return Entry(
        kind=EntryKind.ADJUSTMENT,
        amount_minor=delta,
        currency=payment.currency,
        source=EntrySource.MANUAL,
        created_by=created_by,
        created_at=created_at,
        corrects_id=payment.id,
        reason=clean_reason,
    )


def build_void(
    payment: Entry,
    adjustments: Sequence[Entry],
    *,
    reason: str,
    created_by: int,
    created_at: datetime,
) -> Entry:
    """Adjustment that brings the payment's effective amount to zero."""
    if effective_amount(payment, adjustments) == 0:
        raise AlreadyVoidedError(f"payment {payment.id} is already void")
    return build_correction(
        payment,
        adjustments,
        new_amount_minor=0,
        reason=reason,
        created_by=created_by,
        created_at=created_at,
    )


def balance(entries: Iterable[Entry]) -> dict[str, int]:
    """Sum of every entry, per currency."""
    totals: dict[str, int] = {}
    for entry in entries:
        totals[entry.currency] = totals.get(entry.currency, 0) + entry.amount_minor
    return totals


def _validate_common(entry: Entry) -> None:
    """Everything here must also be storable: a domain-valid entry never fails in storage."""
    if not isinstance(entry.kind, EntryKind) or not isinstance(entry.source, EntrySource):
        raise InvalidEntryError("kind and source must be EntryKind and EntrySource members")
    if isinstance(entry.amount_minor, bool) or not isinstance(entry.amount_minor, int):
        raise InvalidEntryError(f"amount must be integer minor units: {entry.amount_minor!r}")
    if abs(entry.amount_minor) > MAX_AMOUNT_MINOR:
        raise InvalidAmountError(
            f"amount exceeds {MAX_AMOUNT_MINOR} minor units: {entry.amount_minor}"
        )
    if not isinstance(entry.currency, str):
        raise InvalidCurrencyError(f"not an ISO 4217 currency code: {entry.currency!r}")
    validate_currency(entry.currency)
    _validate_positive_int("created_by", entry.created_by, MAX_USER_ID)
    if entry.id is not None:
        _validate_positive_int("id", entry.id, MAX_ENTRY_ID)
    if entry.corrects_id is not None:
        _validate_positive_int("corrects_id", entry.corrects_id, MAX_ENTRY_ID)
    _validate_moment("created_at", entry.created_at)
    if entry.created_at.utcoffset() != timedelta(0):
        raise InvalidEntryError("created_at must be a timezone-aware UTC datetime")
    if entry.paid_at is not None:
        _validate_moment("paid_at", entry.paid_at)
    if entry.image_sha256 is not None and (
        not isinstance(entry.image_sha256, str) or not _SHA256_HEX.fullmatch(entry.image_sha256)
    ):
        raise InvalidEntryError("image_sha256 must be 64 lowercase hex characters")
    for field in _FREE_TEXT_FIELDS:
        _validate_text(field, getattr(entry, field))


def _validate_positive_int(field: str, value: object, maximum: int) -> None:
    """An int (never a bool, a float or digit text) from 1 to `maximum`."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEntryError(f"{field} must be an int: {value!r}")
    if not 1 <= value <= maximum:
        raise InvalidEntryError(f"{field} must be between 1 and {maximum}: {value}")


def _validate_moment(field: str, value: object) -> None:
    """A timezone-aware datetime whose UTC instant falls in the years storage can hold."""
    if not isinstance(value, datetime):
        raise InvalidEntryError(f"{field} must be a datetime: {value!r}")
    if value.utcoffset() is None:
        raise InvalidEntryError(f"{field} must be timezone-aware")
    try:
        year = value.astimezone(UTC).year
    except OverflowError:  # the UTC instant is before year 1 or after year 9999
        year = None
    if year is None or not EARLIEST_STORABLE_YEAR <= year <= LATEST_STORABLE_YEAR:
        raise InvalidEntryError(
            f"{field} must fall in the years {EARLIEST_STORABLE_YEAR} to "
            f"{LATEST_STORABLE_YEAR} in UTC: {value.isoformat()}"
        )


def _validate_text(field: str, value: str | None) -> None:
    """Free text must be storable as is: no NUL character, nothing UTF-8 cannot encode."""
    if value is None:
        return
    if not isinstance(value, str):
        raise InvalidEntryError(f"{field} must be text: {value!r}")
    if "\x00" in value:
        raise InvalidEntryError(f"{field} cannot contain a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidEntryError(f"{field} contains characters UTF-8 cannot encode") from None


def _validate_payment(entry: Entry) -> None:
    if entry.amount_minor <= 0:
        raise InvalidEntryError(f"a payment amount must be positive: {entry.amount_minor}")
    if entry.corrects_id is not None:
        raise InvalidEntryError("a payment cannot correct another entry")
    if entry.reason is not None and not entry.reason.strip():
        raise InvalidEntryError("a payment reason, when given, cannot be blank")


def _validate_adjustment(entry: Entry) -> None:
    if entry.corrects_id is None:
        raise InvalidEntryError("an adjustment must reference the payment it corrects")
    _require_reason(entry.reason)
    if entry.amount_minor == 0:
        raise InvalidEntryError("an adjustment amount cannot be zero")
    if entry.image_sha256 is not None or entry.image_key is not None:
        raise InvalidEntryError("an adjustment cannot carry image fields")
    if entry.source is not EntrySource.MANUAL:
        raise InvalidEntryError("adjustments are always manual")


def _require_payment(entry: Entry) -> None:
    if entry.kind is not EntryKind.PAYMENT:
        raise NotAPaymentError(f"entry {entry.id} is an adjustment, not a payment")


def _require_reason(reason: str | None) -> str:
    if reason is not None and not isinstance(reason, str):
        raise InvalidEntryError(f"reason must be text: {reason!r}")
    if reason is None or not reason.strip():
        raise MissingReasonError("a correction needs a non-blank reason")
    return reason.strip()
