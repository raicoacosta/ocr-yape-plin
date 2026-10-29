from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

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
from payments_bot.domain.ledger import (
    Entry,
    EntryKind,
    EntrySource,
    balance,
    build_correction,
    build_void,
    effective_amount,
    ensure_adjusts,
)

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
LIMA = timezone(-timedelta(hours=5))
USER_ID = 1001
IMAGE_HASH = "a" * 64


def make_payment(amount_minor: int = 25000, **overrides: Any) -> Entry:
    fields: dict[str, Any] = {
        "id": 1,
        "kind": EntryKind.PAYMENT,
        "amount_minor": amount_minor,
        "currency": "PEN",
        "source": EntrySource.OCR,
        "created_by": USER_ID,
        "created_at": NOW,
    }
    return Entry(**(fields | overrides))


def make_adjustment(amount_minor: int, **overrides: Any) -> Entry:
    fields: dict[str, Any] = {
        "id": None,
        "kind": EntryKind.ADJUSTMENT,
        "amount_minor": amount_minor,
        "currency": "PEN",
        "source": EntrySource.MANUAL,
        "created_by": USER_ID,
        "created_at": NOW,
        "corrects_id": 1,
        "reason": "wrong amount read",
    }
    return Entry(**(fields | overrides))


def correct(payment: Entry, adjustments: list[Entry], new_amount_minor: int, reason: str) -> Entry:
    return build_correction(
        payment,
        adjustments,
        new_amount_minor=new_amount_minor,
        reason=reason,
        created_by=USER_ID,
        created_at=NOW,
    )


def void(payment: Entry, adjustments: list[Entry], reason: str = "not ours") -> Entry:
    return build_void(payment, adjustments, reason=reason, created_by=USER_ID, created_at=NOW)


class TestEntryInvariants:
    def test_valid_payment_keeps_receipt_fields(self) -> None:
        paid_at = datetime(2026, 10, 28, 15, 30, tzinfo=LIMA)

        payment = make_payment(
            reference="00123456",
            paid_at=paid_at,
            image_sha256=IMAGE_HASH,
            image_key="receipts/a.jpg",
            extracted_json='{"amount": "250"}',
        )

        assert payment.amount_minor == 25000
        assert payment.reference == "00123456"
        assert payment.paid_at == paid_at
        assert payment.image_sha256 == IMAGE_HASH

    @pytest.mark.parametrize("amount_minor", [0, -100])
    def test_payment_requires_positive_amount(self, amount_minor: int) -> None:
        with pytest.raises(InvalidEntryError):
            make_payment(amount_minor)

    def test_payment_cannot_reference_a_corrected_entry(self) -> None:
        with pytest.raises(InvalidEntryError):
            make_payment(corrects_id=7)

    @pytest.mark.parametrize("amount_minor", [250.0, True, "250"])
    def test_amount_must_be_an_integer_number_of_minor_units(self, amount_minor: Any) -> None:
        with pytest.raises(InvalidEntryError):
            make_payment(amount_minor)

    def test_currency_must_be_iso_4217(self) -> None:
        with pytest.raises(InvalidCurrencyError):
            make_payment(currency="soles")

    @pytest.mark.parametrize(
        "created_at",
        [datetime(2026, 10, 29, 15, 0), datetime(2026, 10, 29, 10, 0, tzinfo=LIMA)],
    )
    def test_created_at_must_be_aware_utc(self, created_at: datetime) -> None:
        with pytest.raises(InvalidEntryError):
            make_payment(created_at=created_at)

    def test_paid_at_must_be_timezone_aware(self) -> None:
        with pytest.raises(InvalidEntryError):
            make_payment(paid_at=datetime(2026, 10, 28, 20, 30))

    @pytest.mark.parametrize("image_sha256", ["abc", "A" * 64, "g" * 64])
    def test_image_hash_must_be_lowercase_sha256_hex(self, image_sha256: str) -> None:
        with pytest.raises(InvalidEntryError):
            make_payment(image_sha256=image_sha256)

    def test_adjustment_requires_corrected_entry(self) -> None:
        with pytest.raises(InvalidEntryError):
            make_adjustment(-500, corrects_id=None)

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_adjustment_requires_reason(self, reason: str | None) -> None:
        with pytest.raises(MissingReasonError):
            make_adjustment(-500, reason=reason)

    def test_adjustment_amount_cannot_be_zero(self) -> None:
        with pytest.raises(InvalidEntryError):
            make_adjustment(0)

    @pytest.mark.parametrize(
        "image_field", [{"image_sha256": IMAGE_HASH}, {"image_key": "receipts/a.jpg"}]
    )
    def test_adjustment_cannot_carry_image_fields(self, image_field: dict[str, str]) -> None:
        with pytest.raises(InvalidEntryError):
            make_adjustment(-500, **image_field)

    def test_adjustment_source_must_be_manual(self) -> None:
        with pytest.raises(InvalidEntryError):
            make_adjustment(-500, source=EntrySource.OCR)


class TestEffectiveAmount:
    def test_without_adjustments_is_the_payment_amount(self) -> None:
        assert effective_amount(make_payment(25000), []) == 25000

    def test_adds_every_adjustment_to_the_payment(self) -> None:
        adjustments = [make_adjustment(-5000), make_adjustment(1000)]

        assert effective_amount(make_payment(25000), adjustments) == 21000

    def test_rejects_adjustment_of_another_payment(self) -> None:
        with pytest.raises(InvalidEntryError):
            effective_amount(make_payment(), [make_adjustment(-500, corrects_id=2)])

    def test_rejects_adjustment_in_another_currency(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            effective_amount(make_payment(), [make_adjustment(-500, currency="USD")])


class TestEnsureAdjusts:
    def test_accepts_matching_adjustment(self) -> None:
        ensure_adjusts(make_payment(), make_adjustment(-500))

    def test_rejects_an_adjustment_as_target(self) -> None:
        target = make_adjustment(-500, id=2)

        with pytest.raises(NotAPaymentError):
            ensure_adjusts(target, make_adjustment(-100, corrects_id=2))

    def test_rejects_currency_mismatch(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            ensure_adjusts(make_payment(currency="PEN"), make_adjustment(-500, currency="USD"))


class TestBuildCorrection:
    def test_positive_delta_when_new_amount_is_higher(self) -> None:
        adjustment = correct(make_payment(25000), [], 30000, "  read 250, receipt says 300  ")

        assert adjustment.kind is EntryKind.ADJUSTMENT
        assert adjustment.amount_minor == 5000
        assert adjustment.corrects_id == 1
        assert adjustment.reason == "read 250, receipt says 300"
        assert adjustment.source is EntrySource.MANUAL
        assert adjustment.currency == "PEN"
        assert adjustment.created_by == USER_ID
        assert adjustment.created_at == NOW
        assert adjustment.id is None

    def test_negative_delta_when_new_amount_is_lower(self) -> None:
        adjustment = correct(make_payment(25000), [], 20000, "typo")

        assert adjustment.amount_minor == -5000

    def test_delta_is_relative_to_the_effective_amount(self) -> None:
        previous = [make_adjustment(-5000)]

        adjustment = correct(make_payment(25000), previous, 25000, "undo previous fix")

        assert adjustment.amount_minor == 5000

    def test_keeps_the_payment_currency(self) -> None:
        adjustment = correct(make_payment(1000, currency="USD"), [], 1500, "typo")

        assert adjustment.currency == "USD"

    def test_rejects_unchanged_amount(self) -> None:
        with pytest.raises(NoChangeError):
            correct(make_payment(25000), [make_adjustment(-5000)], 20000, "same")

    def test_rejects_negative_new_amount(self) -> None:
        with pytest.raises(InvalidAmountError):
            correct(make_payment(25000), [], -1, "negative")

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_rejects_blank_reason(self, reason: str) -> None:
        with pytest.raises(MissingReasonError):
            correct(make_payment(25000), [], 30000, reason)

    def test_rejects_adjustment_as_target(self) -> None:
        target = make_adjustment(-500, id=2)

        with pytest.raises(NotAPaymentError):
            correct(target, [], 1000, "adjusting an adjustment")

    def test_rejects_unsaved_payment(self) -> None:
        with pytest.raises(InvalidEntryError):
            correct(make_payment(25000, id=None), [], 30000, "not persisted")


class TestBuildVoid:
    def test_void_brings_effective_amount_to_zero(self) -> None:
        previous = [make_adjustment(-5000)]

        adjustment = void(make_payment(25000), previous)

        assert adjustment.amount_minor == -20000
        assert effective_amount(make_payment(25000), [*previous, adjustment]) == 0

    def test_rejects_already_voided_payment(self) -> None:
        with pytest.raises(AlreadyVoidedError):
            void(make_payment(25000), [make_adjustment(-25000)])

    def test_rejects_adjustment_as_target(self) -> None:
        with pytest.raises(NotAPaymentError):
            void(make_adjustment(-500, id=2), [])


class TestBalance:
    def test_empty_ledger_has_no_balance(self) -> None:
        assert balance([]) == {}

    def test_sums_every_entry_per_currency(self) -> None:
        entries = [
            make_payment(25000),
            make_payment(1000, id=2, currency="USD"),
            make_adjustment(-5000),
            make_payment(4000, id=3),
        ]

        assert balance(entries) == {"PEN": 24000, "USD": 1000}
