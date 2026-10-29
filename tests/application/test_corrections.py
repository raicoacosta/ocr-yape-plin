from collections.abc import Callable
from typing import Any

import pytest

from payments_bot.adapters.persistence.sqlite_ledger import SqliteLedgerRepository
from payments_bot.application.clock import Clock
from payments_bot.application.corrections import AdjustmentRecorded, CorrectEntry, VoidEntry
from payments_bot.application.errors import EntryNotFoundError
from payments_bot.application.payments import PaymentRegistered
from payments_bot.domain.errors import AlreadyVoidedError, NoChangeError, NotAPaymentError
from payments_bot.domain.ledger import EntryKind

Register = Callable[..., PaymentRegistered]
FIXER_ID = 3003


def correct(
    repository: SqliteLedgerRepository, clock: Clock, entry_id: int, new_amount_minor: int
) -> AdjustmentRecorded:
    return CorrectEntry(repository, clock).execute(
        entry_id=entry_id,
        new_amount_minor=new_amount_minor,
        reason="amount misread",
        created_by=FIXER_ID,
    )


class TestCorrectEntry:
    def test_appends_adjustment_without_touching_the_payment(
        self, repository: SqliteLedgerRepository, clock: Clock, register: Register
    ) -> None:
        payment = register(amount_minor=25000).entry
        register(amount_minor=4000)

        result = CorrectEntry(repository, clock).execute(
            entry_id=payment.id,
            new_amount_minor=30000,
            reason="amount misread",
            created_by=FIXER_ID,
        )

        assert result.adjustment.id is not None
        assert result.adjustment.kind is EntryKind.ADJUSTMENT
        assert result.adjustment.amount_minor == 5000
        assert result.adjustment.corrects_id == payment.id
        assert result.adjustment.created_by == FIXER_ID
        assert result.adjustment.created_at == clock()
        assert result.effective_amount == 30000
        assert result.balance == {"PEN": 34000}
        assert repository.get(payment.id) == payment

    def test_second_correction_is_relative_to_the_effective_amount(
        self, repository: SqliteLedgerRepository, clock: Clock, register: Register
    ) -> None:
        payment = register(amount_minor=25000).entry
        correct(repository, clock, payment.id, 20000)

        result = CorrectEntry(repository, clock).execute(
            entry_id=payment.id, new_amount_minor=22000, reason="second look", created_by=FIXER_ID
        )

        assert result.adjustment.amount_minor == 2000
        assert result.effective_amount == 22000
        assert result.balance == {"PEN": 22000}

    def test_unchanged_amount_writes_nothing(
        self, repository: SqliteLedgerRepository, clock: Clock, register: Register
    ) -> None:
        payment = register(amount_minor=25000).entry

        with pytest.raises(NoChangeError):
            correct(repository, clock, payment.id, 25000)

        assert repository.history(limit=10) == [payment]

    def test_missing_entry_raises_not_found(
        self, repository: SqliteLedgerRepository, clock: Clock
    ) -> None:
        with pytest.raises(EntryNotFoundError) as excinfo:
            correct(repository, clock, 999, 1000)

        assert excinfo.value.entry_id == 999

    def test_adjustment_cannot_be_corrected(
        self, repository: SqliteLedgerRepository, clock: Clock, register: Register
    ) -> None:
        payment = register(amount_minor=25000).entry
        fix = correct(repository, clock, payment.id, 20000)

        with pytest.raises(NotAPaymentError):
            correct(repository, clock, fix.adjustment.id, 1000)

        assert len(repository.history(limit=10)) == 2


class TestVoidEntry:
    def test_void_brings_the_payment_to_zero(
        self, repository: SqliteLedgerRepository, clock: Clock, register: Register
    ) -> None:
        payment = register(amount_minor=25000).entry
        register(amount_minor=4000)
        correct(repository, clock, payment.id, 20000)

        result = VoidEntry(repository, clock).execute(
            entry_id=payment.id, reason="not our payment", created_by=FIXER_ID
        )

        assert result.adjustment.amount_minor == -20000
        assert result.adjustment.reason == "not our payment"
        assert result.effective_amount == 0
        assert result.balance == {"PEN": 4000}
        assert repository.get(payment.id) == payment

    def test_voiding_twice_is_rejected(
        self, repository: SqliteLedgerRepository, clock: Clock, register: Register
    ) -> None:
        payment = register(amount_minor=25000).entry
        void = VoidEntry(repository, clock)
        void.execute(entry_id=payment.id, reason="duplicate", created_by=FIXER_ID)

        with pytest.raises(AlreadyVoidedError):
            void.execute(entry_id=payment.id, reason="again", created_by=FIXER_ID)

        assert len(repository.history(limit=10)) == 2

    def test_missing_entry_raises_not_found(
        self, repository: SqliteLedgerRepository, clock: Clock
    ) -> None:
        with pytest.raises(EntryNotFoundError):
            VoidEntry(repository, clock).execute(entry_id=999, reason="gone", created_by=FIXER_ID)


class _FailingBalance:
    """A repository whose balance query fails after the append succeeded."""

    def __init__(self, repository: SqliteLedgerRepository) -> None:
        self._repository = repository

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    def balance(self) -> dict[str, int]:
        raise RuntimeError("balance unavailable")


@pytest.mark.parametrize("use_case", ["correct", "void"])
def test_adjustment_is_undone_when_a_later_step_fails(
    repository: SqliteLedgerRepository, clock: Clock, register: Register, use_case: str
) -> None:
    payment = register(amount_minor=25000).entry
    failing = _FailingBalance(repository)

    with pytest.raises(RuntimeError, match="balance unavailable"):
        if use_case == "correct":
            CorrectEntry(failing, clock).execute(
                entry_id=payment.id, new_amount_minor=1, reason="x", created_by=FIXER_ID
            )
        else:
            VoidEntry(failing, clock).execute(entry_id=payment.id, reason="x", created_by=FIXER_ID)

    assert repository.history(limit=10) == [payment]
