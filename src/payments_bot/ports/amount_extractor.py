"""Extraction port: read a payment receipt image and report the payment it shows.

An extractor never raises for a bad image or a provider failure. Every outcome is an
`Extraction` with an explicit status, so the caller decides what to ask the user instead of
recording a guessed amount.

Only `ok` may be recorded without asking. `needs_confirmation` carries a candidate amount that
the user must confirm (or correct) before anything is recorded; every other status carries no
amount at all, so the user has to type it.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from payments_bot.domain.money import MAX_AMOUNT_MINOR


class ExtractionStatus(StrEnum):
    OK = "ok"
    """A trustworthy amount: the model was highly confident and every consistency check passed.
    The only status that may be recorded without asking the user."""
    NEEDS_CONFIRMATION = "needs_confirmation"
    """A candidate amount and currency were read, but they are not trustworthy on their own (the
    model was not highly confident, or the amount repeats the receipt's security code). Never
    record it automatically: show it to the user and record only what they confirm."""
    UNREADABLE = "unreadable"
    """It is a receipt, but no trustworthy amount and currency could be read."""
    NOT_A_RECEIPT = "not_a_receipt"
    REFUSED = "refused"
    """The model declined to answer (`stop_reason == "refusal"`)."""
    FAILED = "failed"
    """Unsupported image, provider error, or an answer that broke the output contract."""


AMOUNT_STATUSES = frozenset({ExtractionStatus.OK, ExtractionStatus.NEEDS_CONFIRMATION})
"""Statuses that carry an amount and a currency; every other status carries no amount."""


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Extraction:
    status: ExtractionStatus
    model: str
    amount_minor: int | None = None
    """Between 1 and `MAX_AMOUNT_MINOR` for `ok` and `needs_confirmation`; None otherwise."""
    currency: str | None = None
    reference: str | None = None
    """Operation number printed on the receipt, as ASCII digits (digits of other scripts
    converted, leading zeros kept); None when absent or implausibly long."""
    paid_on: date | None = None
    """Payment date printed on the receipt (a Lima calendar date); None when absent or not a
    plausible receipt date."""
    paid_at: datetime | None = None
    """Timezone-aware payment time, only when the receipt prints a time of day.

    A receipt that prints only a date has `paid_on` and no `paid_at`: no midnight is invented.
    """
    provider: str | None = None
    """App or bank that issued the receipt, e.g. "Yape"."""
    confidence: Confidence | None = None
    raw_json: str | None = None
    """The model's raw answer, kept for the ledger's `extracted_json` audit column."""
    usage: TokenUsage | None = None
    error: str | None = None
    """Why the result is not `ok`: for logs, and for the question the bot asks the user."""

    def __post_init__(self) -> None:
        if self.status in AMOUNT_STATUSES:
            if self.amount_minor is None or self.currency is None:
                raise ValueError(f"a {self.status} extraction needs an amount and a currency")
            if not 0 < self.amount_minor <= MAX_AMOUNT_MINOR:
                raise ValueError(
                    f"a {self.status} amount must be between 1 and {MAX_AMOUNT_MINOR} minor units"
                )
        elif self.amount_minor is not None:
            raise ValueError(f"a {self.status} extraction must not carry an amount")
        if self.paid_at is not None:
            if self.paid_at.utcoffset() is None:
                raise ValueError("paid_at must be timezone-aware")
            if self.paid_on != self.paid_at.date():
                raise ValueError("paid_on must be the date of paid_at")

    @property
    def auto_recordable(self) -> bool:
        """True only for `ok`: the one result that may be recorded without asking the user."""
        return self.status is ExtractionStatus.OK


class AmountExtractor(Protocol):
    def extract(self, image: bytes) -> Extraction:
        """Read one receipt image. Never raises for bad input or provider failures."""
        ...
