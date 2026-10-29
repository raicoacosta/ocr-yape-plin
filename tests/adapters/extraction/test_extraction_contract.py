"""Invariants of the `Extraction` value every extractor adapter returns."""

from datetime import UTC, datetime

import pytest

from payments_bot.ports.amount_extractor import Extraction, ExtractionStatus

MODEL = "anthropic.claude-opus-5-5"


def test_ok_extraction_carries_amount_and_currency() -> None:
    extraction = Extraction(
        status=ExtractionStatus.OK,
        model=MODEL,
        amount_minor=25000,
        currency="PEN",
        paid_at=datetime(2026, 10, 23, 1, 28, tzinfo=UTC),
    )

    assert extraction.amount_minor == 25000


@pytest.mark.parametrize(
    ("amount_minor", "currency"),
    [(None, "PEN"), (25000, None), (-1, "PEN")],
)
def test_ok_extraction_requires_a_non_negative_amount_and_a_currency(
    amount_minor: int | None, currency: str | None
) -> None:
    with pytest.raises(ValueError):
        Extraction(
            status=ExtractionStatus.OK, model=MODEL, amount_minor=amount_minor, currency=currency
        )


@pytest.mark.parametrize(
    "status",
    [
        ExtractionStatus.UNREADABLE,
        ExtractionStatus.NOT_A_RECEIPT,
        ExtractionStatus.REFUSED,
        ExtractionStatus.FAILED,
    ],
)
def test_non_ok_extraction_never_carries_an_amount(status: ExtractionStatus) -> None:
    with pytest.raises(ValueError):
        Extraction(status=status, model=MODEL, amount_minor=25000, currency="PEN")


def test_paid_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        Extraction(
            status=ExtractionStatus.UNREADABLE,
            model=MODEL,
            paid_at=datetime(2026, 10, 22, 20, 28),
        )
