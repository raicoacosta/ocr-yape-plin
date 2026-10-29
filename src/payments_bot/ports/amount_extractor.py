"""Extraction port: read a payment receipt image and report the payment it shows.

An extractor never raises for a bad image or a provider failure. Every outcome is an
`Extraction` with an explicit status, so the caller decides what to ask the user instead of
recording a guessed amount.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ExtractionStatus(StrEnum):
    OK = "ok"
    UNREADABLE = "unreadable"
    """It is a receipt, but no trustworthy amount and currency could be read."""
    NOT_A_RECEIPT = "not_a_receipt"
    REFUSED = "refused"
    """The model declined to answer (`stop_reason == "refusal"`)."""
    FAILED = "failed"
    """Unsupported image, provider error, or an answer that broke the output contract."""


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
    currency: str | None = None
    reference: str | None = None
    """Operation number printed on the receipt."""
    paid_at: datetime | None = None
    """Timezone-aware payment time."""
    provider: str | None = None
    """App or bank that issued the receipt, e.g. "Yape"."""
    confidence: Confidence | None = None
    raw_json: str | None = None
    """The model's raw answer, kept for the ledger's `extracted_json` audit column."""
    usage: TokenUsage | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status is ExtractionStatus.OK:
            if self.amount_minor is None or self.amount_minor < 0 or self.currency is None:
                raise ValueError("an ok extraction needs a non-negative amount and a currency")
        elif self.amount_minor is not None:
            raise ValueError(f"a {self.status} extraction must not carry an amount")
        if self.paid_at is not None and self.paid_at.utcoffset() is None:
            raise ValueError("paid_at must be timezone-aware")


class AmountExtractor(Protocol):
    def extract(self, image: bytes) -> Extraction:
        """Read one receipt image. Never raises for bad input or provider failures."""
        ...
