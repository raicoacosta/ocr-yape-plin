"""Invariants of the `Extraction` value every extractor adapter returns."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from payments_bot.domain.money import MAX_AMOUNT_MINOR
from payments_bot.ports.amount_extractor import Extraction, ExtractionStatus

MODEL = "anthropic.claude-opus-5-5"
LIMA = ZoneInfo("America/Lima")
CARRY_AN_AMOUNT = [ExtractionStatus.OK, ExtractionStatus.NEEDS_CONFIRMATION]


def test_ok_extraction_carries_amount_and_currency() -> None:
    extraction = Extraction(
        status=ExtractionStatus.OK,
        model=MODEL,
        amount_minor=25000,
        currency="PEN",
        paid_on=date(2026, 1, 15),
        paid_at=datetime(2026, 1, 15, 9, 30, tzinfo=LIMA),
    )

    assert extraction.amount_minor == 25000


@pytest.mark.parametrize("status", CARRY_AN_AMOUNT)
@pytest.mark.parametrize(
    ("amount_minor", "currency"),
    [(None, "PEN"), (25000, None), (-1, "PEN"), (0, "PEN"), (MAX_AMOUNT_MINOR + 1, "PEN")],
)
def test_an_amount_must_be_positive_capped_and_in_a_currency(
    status: ExtractionStatus, amount_minor: int | None, currency: str | None
) -> None:
    with pytest.raises(ValueError):
        Extraction(status=status, model=MODEL, amount_minor=amount_minor, currency=currency)


@pytest.mark.parametrize("status", CARRY_AN_AMOUNT)
def test_the_largest_ledger_amount_is_accepted(status: ExtractionStatus) -> None:
    extraction = Extraction(
        status=status, model=MODEL, amount_minor=MAX_AMOUNT_MINOR, currency="PEN"
    )

    assert extraction.amount_minor == MAX_AMOUNT_MINOR


def test_only_an_ok_extraction_may_be_recorded_without_asking() -> None:
    ok = Extraction(status=ExtractionStatus.OK, model=MODEL, amount_minor=25000, currency="PEN")
    candidate = Extraction(
        status=ExtractionStatus.NEEDS_CONFIRMATION,
        model=MODEL,
        amount_minor=25000,
        currency="PEN",
        error="model confidence is low",
    )

    assert ok.auto_recordable
    assert not candidate.auto_recordable
    assert candidate.amount_minor == 25000


@pytest.mark.parametrize(
    "status",
    [
        ExtractionStatus.UNREADABLE,
        ExtractionStatus.NOT_A_RECEIPT,
        ExtractionStatus.REFUSED,
        ExtractionStatus.FAILED,
    ],
)
def test_other_statuses_never_carry_an_amount_and_are_not_recordable(
    status: ExtractionStatus,
) -> None:
    with pytest.raises(ValueError):
        Extraction(status=status, model=MODEL, amount_minor=25000, currency="PEN")
    assert not Extraction(status=status, model=MODEL).auto_recordable


def test_paid_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        Extraction(
            status=ExtractionStatus.UNREADABLE,
            model=MODEL,
            paid_on=date(2026, 1, 15),
            paid_at=datetime(2026, 1, 15, 9, 30),
        )


def test_a_date_only_receipt_has_a_date_and_no_invented_time() -> None:
    extraction = Extraction(
        status=ExtractionStatus.UNREADABLE, model=MODEL, paid_on=date(2026, 1, 15)
    )

    assert extraction.paid_on == date(2026, 1, 15)
    assert extraction.paid_at is None


@pytest.mark.parametrize("paid_on", [None, date(2026, 1, 16)], ids=["missing", "other-day"])
def test_paid_on_must_be_the_date_of_paid_at(paid_on: date | None) -> None:
    with pytest.raises(ValueError):
        Extraction(
            status=ExtractionStatus.UNREADABLE,
            model=MODEL,
            paid_on=paid_on,
            paid_at=datetime(2026, 1, 15, 9, 30, tzinfo=LIMA),
        )
