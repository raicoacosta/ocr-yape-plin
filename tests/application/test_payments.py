from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from payments_bot.adapters.persistence.sqlite_ledger import SqliteLedgerRepository
from payments_bot.application.clock import Clock
from payments_bot.application.errors import DuplicateImageError
from payments_bot.application.payments import PaymentRegistered
from payments_bot.domain.errors import InvalidEntryError
from payments_bot.domain.ledger import EntryKind, EntrySource

Register = Callable[..., PaymentRegistered]
HASH_A = "a" * 64


def test_register_saves_payment_and_returns_new_balance(
    repository: SqliteLedgerRepository, clock: Clock, register: Register
) -> None:
    register(amount_minor=25000)
    paid_at = datetime(2026, 10, 28, 20, 30, tzinfo=UTC)

    result = register(
        amount_minor=4000,
        source=EntrySource.MANUAL,
        created_by=2002,
        image_sha256=HASH_A,
        image_key="receipts/a.jpg",
        reference="00123456",
        paid_at=paid_at,
        extracted_json='{"amount": "40.00"}',
    )

    assert result.entry.id is not None
    assert result.entry.kind is EntryKind.PAYMENT
    assert result.entry.source is EntrySource.MANUAL
    assert result.entry.created_by == 2002
    assert result.entry.created_at == clock()
    assert result.entry.paid_at == paid_at
    assert repository.get(result.entry.id) == result.entry
    assert result.possible_duplicates == ()
    assert result.balance == {"PEN": 29000}


def test_duplicate_image_is_rejected_with_existing_id_and_nothing_written(
    repository: SqliteLedgerRepository, register: Register
) -> None:
    original = register(image_sha256=HASH_A)

    with pytest.raises(DuplicateImageError) as excinfo:
        register(amount_minor=4000, image_sha256=HASH_A)

    assert excinfo.value.existing_entry_id == original.entry.id
    assert repository.history(limit=10) == [original.entry]


def test_same_reference_is_registered_but_flagged_as_possible_duplicate(
    register: Register,
) -> None:
    first = register(reference="00123456")
    register(reference="00999999")
    second = register(reference="00123456")

    third = register(reference="00123456")

    assert second.possible_duplicates == (first.entry.id,)
    assert third.possible_duplicates == (first.entry.id, second.entry.id)
    assert third.balance == {"PEN": 100000}


def test_blank_reference_is_stored_as_missing_and_never_flagged(register: Register) -> None:
    register(reference="")

    result = register(reference="   ")

    assert result.entry.reference is None
    assert result.possible_duplicates == ()


def test_invalid_payment_writes_nothing(
    repository: SqliteLedgerRepository, register: Register
) -> None:
    with pytest.raises(InvalidEntryError):
        register(amount_minor=0)

    assert repository.history(limit=10) == []
