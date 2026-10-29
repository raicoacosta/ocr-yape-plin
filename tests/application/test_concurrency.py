"""Two writers, each with its own connection to the same file, run the real use cases at once.

`PausingRepository` holds each racer right after a chosen read until the other racer reaches
the same point, so without a unit of work both would decide from the same state. With one,
the second racer is blocked behind the first, the pause times out, and the writes serialize.
The assertions only use invariants that hold for either ordering.
"""

import contextlib
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from payments_bot.adapters.persistence.sqlite_ledger import SqliteLedgerRepository
from payments_bot.application.corrections import CorrectEntry, VoidEntry
from payments_bot.application.errors import DuplicateImageError
from payments_bot.application.payments import RegisterPayment
from payments_bot.application.queries import GetEntryDetail
from payments_bot.domain.errors import AlreadyVoidedError, NoChangeError
from payments_bot.domain.ledger import EntryKind, EntrySource, balance

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
HASH_A = "a" * 64
RACE_WINDOW_SECONDS = 0.3
"""How long a racer waits for the other one after its read; far below the busy timeout."""

Job = Callable[[Any], object]


def clock() -> datetime:
    return NOW


class PausingRepository:
    """Delegates to a repository, pausing after `pause_on` until the other racer gets there."""

    def __init__(
        self, repository: SqliteLedgerRepository, pause_on: str, barrier: threading.Barrier
    ) -> None:
        self._repository = repository
        self._pause_on = pause_on
        self._barrier = barrier

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._repository, name)
        if name != self._pause_on:
            return attribute

        def paused(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            # A broken barrier means the other racer is blocked behind this unit of work.
            with contextlib.suppress(threading.BrokenBarrierError):
                self._barrier.wait(timeout=RACE_WINDOW_SECONDS)
            return result

        return paused


def race(db_path: Path, pause_on: str, *jobs: Job) -> list[object]:
    """Run each job in its own thread and repository; return each result or raised error."""
    barrier = threading.Barrier(len(jobs))
    outcomes: list[object] = [None] * len(jobs)

    def run(index: int, job: Job) -> None:
        repository = SqliteLedgerRepository(db_path)
        try:
            outcomes[index] = job(PausingRepository(repository, pause_on, barrier))
        except Exception as error:  # noqa: BLE001 - the error is the outcome under test
            outcomes[index] = error
        finally:
            repository.close()

    threads = [threading.Thread(target=run, args=item) for item in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    return outcomes


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def ledger(db_path: Path) -> Iterator[SqliteLedgerRepository]:
    repository = SqliteLedgerRepository(db_path)
    yield repository
    repository.close()


@pytest.fixture
def payment_id(ledger: SqliteLedgerRepository) -> int:
    register = RegisterPayment(ledger, clock)
    payment = register.execute(amount_minor=25000, source=EntrySource.OCR, created_by=1).entry
    register.execute(amount_minor=4000, source=EntrySource.OCR, created_by=1)
    return payment.id


def void(entry_id: int) -> Job:
    return lambda repository: VoidEntry(repository, clock).execute(
        entry_id=entry_id, reason="duplicate tap", created_by=1
    )


def correct(entry_id: int, new_amount_minor: int) -> Job:
    return lambda repository: CorrectEntry(repository, clock).execute(
        entry_id=entry_id, new_amount_minor=new_amount_minor, reason="misread", created_by=1
    )


def register(**fields: Any) -> Job:
    return lambda repository: RegisterPayment(repository, clock).execute(
        amount_minor=25000, source=EntrySource.OCR, created_by=1, **fields
    )


def assert_balance_matches_rows(ledger: SqliteLedgerRepository) -> None:
    assert ledger.balance() == balance(ledger.history(limit=1000))


def test_double_void_writes_one_adjustment(
    db_path: Path, ledger: SqliteLedgerRepository, payment_id: int
) -> None:
    outcomes = race(db_path, "adjustments_for", void(payment_id), void(payment_id))

    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], AlreadyVoidedError)
    detail = GetEntryDetail(ledger).execute(payment_id)
    assert [adjustment.amount_minor for adjustment in detail.adjustments] == [-25000]
    assert detail.effective_amount == 0
    assert ledger.balance() == {"PEN": 4000}
    assert_balance_matches_rows(ledger)


def test_double_correction_lands_exactly_on_the_requested_amount(
    db_path: Path, ledger: SqliteLedgerRepository, payment_id: int
) -> None:
    outcomes = race(
        db_path, "adjustments_for", correct(payment_id, 30000), correct(payment_id, 30000)
    )

    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], NoChangeError)
    detail = GetEntryDetail(ledger).execute(payment_id)
    assert detail.effective_amount == 30000
    assert len(detail.adjustments) == 1
    assert ledger.balance() == {"PEN": 34000}
    assert_balance_matches_rows(ledger)


def test_correction_racing_a_void_ends_in_one_of_the_two_intended_states(
    db_path: Path, ledger: SqliteLedgerRepository, payment_id: int
) -> None:
    outcomes = race(db_path, "adjustments_for", correct(payment_id, 30000), void(payment_id))

    assert not any(isinstance(outcome, Exception) for outcome in outcomes)
    detail = GetEntryDetail(ledger).execute(payment_id)
    assert detail.effective_amount in {0, 30000}
    reported = {outcome.effective_amount for outcome in outcomes}
    assert reported == {0, 30000}
    assert_balance_matches_rows(ledger)


def test_same_image_registered_twice_at_once_keeps_one_row(
    db_path: Path, ledger: SqliteLedgerRepository
) -> None:
    outcomes = race(
        db_path, "find_by_image_hash", register(image_sha256=HASH_A), register(image_sha256=HASH_A)
    )

    winners = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], DuplicateImageError)
    assert losers[0].existing_entry_id == winners[0].entry.id
    assert ledger.history(limit=10) == [winners[0].entry]


def test_same_reference_registered_twice_at_once_is_flagged_by_the_second(
    db_path: Path, ledger: SqliteLedgerRepository
) -> None:
    outcomes = race(
        db_path,
        "find_payments_by_reference",
        register(reference="OP-1"),
        register(reference="OP-1"),
    )

    assert not any(isinstance(outcome, Exception) for outcome in outcomes)
    first, second = sorted(outcomes, key=lambda outcome: outcome.entry.id)
    assert first.possible_duplicates == ()
    assert second.possible_duplicates == (first.entry.id,)
    assert [entry.kind for entry in ledger.history(limit=10)] == [EntryKind.PAYMENT] * 2
