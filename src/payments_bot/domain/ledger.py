"""Append-only ledger rules.

History is never edited: a correction is a new adjustment entry that references the
original payment, like an accounting counter-entry.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from payments_bot.domain.errors import (
    AlreadyVoidedError,
    CurrencyMismatchError,
    InvalidAmountError,
    InvalidEntryError,
    MissingReasonError,
    NoChangeError,
    NotAPaymentError,
)
from payments_bot.domain.money import DEFAULT_CURRENCY, validate_currency

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


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
    if new_amount_minor < 0:
        raise InvalidAmountError(f"a corrected amount cannot be negative: {new_amount_minor}")
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
    if not isinstance(entry.kind, EntryKind) or not isinstance(entry.source, EntrySource):
        raise InvalidEntryError("kind and source must be EntryKind and EntrySource members")
    if isinstance(entry.amount_minor, bool) or not isinstance(entry.amount_minor, int):
        raise InvalidEntryError(f"amount must be integer minor units: {entry.amount_minor!r}")
    validate_currency(entry.currency)
    if entry.created_at.utcoffset() != timedelta(0):
        raise InvalidEntryError("created_at must be a timezone-aware UTC datetime")
    if entry.paid_at is not None and entry.paid_at.utcoffset() is None:
        raise InvalidEntryError("paid_at must be timezone-aware")
    if entry.image_sha256 is not None and not _SHA256_HEX.fullmatch(entry.image_sha256):
        raise InvalidEntryError("image_sha256 must be 64 lowercase hex characters")


def _validate_payment(entry: Entry) -> None:
    if entry.amount_minor <= 0:
        raise InvalidEntryError(f"a payment amount must be positive: {entry.amount_minor}")
    if entry.corrects_id is not None:
        raise InvalidEntryError("a payment cannot correct another entry")


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
    if reason is None or not reason.strip():
        raise MissingReasonError("a correction needs a non-blank reason")
    return reason.strip()
