from datetime import UTC, date, datetime, timedelta, timezone
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
from payments_bot.domain.money import MAX_AMOUNT_MINOR

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
LIMA = timezone(-timedelta(hours=5))
PLUS_FIVE = timezone(timedelta(hours=5))
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

    def test_payment_may_reach_the_maximum_amount(self) -> None:
        assert make_payment(MAX_AMOUNT_MINOR).amount_minor == MAX_AMOUNT_MINOR

    @pytest.mark.parametrize("amount_minor", [MAX_AMOUNT_MINOR + 1, 2**63])
    def test_payment_above_the_maximum_is_refused(self, amount_minor: int) -> None:
        with pytest.raises(InvalidAmountError):
            make_payment(amount_minor)

    def test_adjustment_is_bounded_by_the_maximum_too(self) -> None:
        assert make_adjustment(-MAX_AMOUNT_MINOR).amount_minor == -MAX_AMOUNT_MINOR
        with pytest.raises(InvalidAmountError):
            make_adjustment(-MAX_AMOUNT_MINOR - 1)

    @pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
    def test_payment_reason_when_given_cannot_be_blank(self, reason: str) -> None:
        with pytest.raises(InvalidEntryError):
            make_payment(reason=reason)

    @pytest.mark.parametrize("field", ["reason", "reference", "image_key", "extracted_json"])
    @pytest.mark.parametrize(
        "text", [pytest.param("a\x00b", id="nul"), pytest.param("a\ud800", id="lone-surrogate")]
    )
    def test_text_fields_refuse_nul_and_characters_utf8_cannot_store(
        self, field: str, text: str
    ) -> None:
        with pytest.raises(InvalidEntryError, match=field):
            make_payment(**{field: text})

    def test_adjustment_reason_refuses_nul(self) -> None:
        with pytest.raises(InvalidEntryError, match="reason"):
            make_adjustment(-500, reason="misread\x00")

    @pytest.mark.parametrize("currency", ["soles", "PEN\x00", "PEN\x00x"])
    def test_currency_must_be_iso_4217(self, currency: str) -> None:
        with pytest.raises(InvalidCurrencyError):
            make_payment(currency=currency)

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

    @pytest.mark.parametrize(
        "paid_at",
        [
            pytest.param(datetime(999, 12, 31, 23, 0, tzinfo=UTC), id="year-999"),
            pytest.param(datetime(1000, 1, 1, 1, 0, tzinfo=PLUS_FIVE), id="year-999-in-utc"),
            pytest.param(datetime(1, 1, 1, 1, 0, tzinfo=PLUS_FIVE), id="before-datetime-min"),
            pytest.param(datetime(9999, 12, 31, 23, 59, tzinfo=LIMA), id="after-datetime-max"),
            pytest.param(datetime.min.replace(tzinfo=UTC), id="datetime-min"),
        ],
    )
    def test_paid_at_must_fall_in_years_1000_to_9999_in_utc(self, paid_at: datetime) -> None:
        # Storage writes the UTC instant with a four-digit year that cannot start with 0.
        with pytest.raises(InvalidEntryError, match="paid_at"):
            make_payment(paid_at=paid_at)

    @pytest.mark.parametrize(
        "paid_at",
        [
            datetime(1000, 1, 1, tzinfo=UTC),
            datetime(1000, 1, 1, 0, 0, tzinfo=LIMA),
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(9999, 12, 31, 18, 59, 59, 999999, tzinfo=LIMA),
        ],
    )
    def test_paid_at_may_reach_both_ends_of_the_storable_years(self, paid_at: datetime) -> None:
        assert make_payment(paid_at=paid_at).paid_at == paid_at

    @pytest.mark.parametrize(
        "created_at",
        [datetime(999, 12, 31, 23, 59, tzinfo=UTC), datetime.min.replace(tzinfo=UTC)],
    )
    def test_created_at_must_fall_in_years_1000_to_9999(self, created_at: datetime) -> None:
        with pytest.raises(InvalidEntryError, match="created_at"):
            make_payment(created_at=created_at)

    @pytest.mark.parametrize("field", ["created_at", "paid_at"])
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("2026-10-29T15:00:00+00:00", id="text"),
            pytest.param(1761750000, id="number"),
            pytest.param(date(2026, 10, 29), id="date"),
        ],
    )
    def test_timestamps_must_be_datetimes(self, field: str, value: object) -> None:
        with pytest.raises(InvalidEntryError, match=field):
            make_payment(**{field: value})

    @pytest.mark.parametrize(
        "created_by",
        [0, -1, 2**63, True, 1.5, "7", "abc", None],
        ids=["zero", "negative", "above-int64", "bool", "float", "digit-text", "text", "none"],
    )
    def test_created_by_must_be_a_positive_64_bit_int(self, created_by: object) -> None:
        with pytest.raises(InvalidEntryError, match="created_by"):
            make_payment(created_by=created_by)

    @pytest.mark.parametrize("created_by", [1, 2**63 - 1])
    def test_created_by_may_reach_both_ends_of_the_range(self, created_by: int) -> None:
        assert make_payment(created_by=created_by).created_by == created_by

    @pytest.mark.parametrize("value", [0, -1, 2**63, True, 1.0, "1"])
    def test_ids_must_be_positive_64_bit_ints(self, value: object) -> None:
        with pytest.raises(InvalidEntryError, match="^id "):
            make_payment(id=value)
        with pytest.raises(InvalidEntryError, match="^corrects_id "):
            make_adjustment(-500, corrects_id=value)

    def test_ids_may_reach_the_largest_64_bit_int(self) -> None:
        assert make_payment(id=2**63 - 1).id == 2**63 - 1
        assert make_adjustment(-500, corrects_id=2**63 - 1).corrects_id == 2**63 - 1

    @pytest.mark.parametrize(
        "field", ["reason", "reference", "image_key", "extracted_json", "image_sha256"]
    )
    @pytest.mark.parametrize(
        "value", [pytest.param(123, id="int"), pytest.param(b"a" * 64, id="bytes")]
    )
    def test_text_fields_refuse_values_that_are_not_str(self, field: str, value: object) -> None:
        # A domain error, never a TypeError: every rule violation is a `DomainError`.
        with pytest.raises(InvalidEntryError, match=field):
            make_payment(**{field: value})

    @pytest.mark.parametrize("currency", [123, b"PEN", None])
    def test_currency_that_is_not_str_is_refused_as_a_currency(self, currency: object) -> None:
        with pytest.raises(InvalidCurrencyError):
            make_payment(currency=currency)

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

    def test_new_amount_may_reach_the_maximum(self) -> None:
        adjustment = correct(make_payment(25000), [], MAX_AMOUNT_MINOR, "big transfer")

        assert adjustment.amount_minor == MAX_AMOUNT_MINOR - 25000

    def test_rejects_new_amount_above_the_maximum(self) -> None:
        with pytest.raises(InvalidAmountError):
            correct(make_payment(25000), [], MAX_AMOUNT_MINOR + 1, "too big")

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_rejects_blank_reason(self, reason: str) -> None:
        with pytest.raises(MissingReasonError):
            correct(make_payment(25000), [], 30000, reason)

    @pytest.mark.parametrize("reason", [123, b"typo"])
    def test_rejects_a_reason_that_is_not_text(self, reason: Any) -> None:
        with pytest.raises(InvalidEntryError, match="reason"):
            correct(make_payment(25000), [], 30000, reason)

    @pytest.mark.parametrize("new_amount_minor", [True, 1.5, 30000.0, "30000"])
    def test_rejects_a_new_amount_that_is_not_integer_minor_units(
        self, new_amount_minor: Any
    ) -> None:
        with pytest.raises(InvalidAmountError):
            correct(make_payment(25000), [], new_amount_minor, "typo")

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
