from collections.abc import Callable
from typing import Any

import pytest

from payments_bot.adapters.persistence.sqlite_ledger import SqliteLedgerRepository
from payments_bot.application.clock import Clock
from payments_bot.application.corrections import CorrectEntry, VoidEntry
from payments_bot.application.errors import EntryNotFoundError
from payments_bot.application.payments import PaymentRegistered
from payments_bot.application.queries import (
    MAX_ENTRY_ID,
    MAX_HISTORY_PAGE,
    GetBalance,
    GetEntryDetail,
    ListHistory,
)

Register = Callable[..., PaymentRegistered]


def test_get_balance_sums_per_currency(
    repository: SqliteLedgerRepository, register: Register
) -> None:
    register(amount_minor=25000)
    register(amount_minor=1000, currency="USD")

    assert GetBalance(repository).execute() == {"PEN": 25000, "USD": 1000}


def test_get_entry_detail_returns_payment_adjustments_and_effective_amount(
    repository: SqliteLedgerRepository, clock: Clock, register: Register
) -> None:
    payment = register(amount_minor=25000).entry
    correct = CorrectEntry(repository, clock)
    first = correct.execute(entry_id=payment.id, new_amount_minor=20000, reason="a", created_by=1)
    second = correct.execute(entry_id=payment.id, new_amount_minor=21000, reason="b", created_by=1)

    detail = GetEntryDetail(repository).execute(payment.id)

    assert detail.payment == payment
    assert detail.adjustments == (first.adjustment, second.adjustment)
    assert detail.effective_amount == 21000


def test_get_entry_detail_of_missing_entry_raises_not_found(
    repository: SqliteLedgerRepository,
) -> None:
    with pytest.raises(EntryNotFoundError):
        GetEntryDetail(repository).execute(999)


def test_list_history_is_newest_first_and_pages_before_the_last_seen_id(
    repository: SqliteLedgerRepository, register: Register
) -> None:
    ids = [register(amount_minor=amount).entry.id for amount in (100, 200, 300)]
    history = ListHistory(repository)

    first_page = history.execute(limit=2)
    register(amount_minor=50)
    second_page = history.execute(limit=2, before_id=first_page[-1].id)

    assert [entry.id for entry in first_page] == [ids[2], ids[1]]
    assert [entry.id for entry in second_page] == [ids[0]]


@pytest.mark.parametrize(
    ("limit", "before_id"),
    [
        (0, None),
        (-1, None),
        (MAX_HISTORY_PAGE + 1, None),
        (2**63, None),
        (10, 0),
        (10, -1),
        (10, MAX_ENTRY_ID + 1),
    ],
)
def test_list_history_rejects_invalid_paging(
    repository: SqliteLedgerRepository, limit: int, before_id: int | None
) -> None:
    with pytest.raises(ValueError):
        ListHistory(repository).execute(limit=limit, before_id=before_id)


@pytest.mark.parametrize(
    ("limit", "before_id"),
    [(True, None), (2.0, None), ("10", None), (10, 2.5), (10, True), (10, "5")],
)
def test_list_history_accepts_only_int_paging_values(
    repository: SqliteLedgerRepository, limit: Any, before_id: Any
) -> None:
    with pytest.raises(TypeError):
        ListHistory(repository).execute(limit=limit, before_id=before_id)


def test_list_history_accepts_the_largest_page_and_id(
    repository: SqliteLedgerRepository, register: Register
) -> None:
    ids = [register(amount_minor=amount).entry.id for amount in (100, 200)]

    page = ListHistory(repository).execute(limit=MAX_HISTORY_PAGE, before_id=MAX_ENTRY_ID)

    assert [entry.id for entry in page] == ids[::-1]


@pytest.mark.parametrize("entry_id", [0, -1, MAX_ENTRY_ID + 1])
def test_ids_no_entry_can_have_are_not_found(
    repository: SqliteLedgerRepository, clock: Clock, entry_id: int
) -> None:
    with pytest.raises(EntryNotFoundError):
        GetEntryDetail(repository).execute(entry_id)
    with pytest.raises(EntryNotFoundError):
        VoidEntry(repository, clock).execute(entry_id=entry_id, reason="x", created_by=1)


@pytest.mark.parametrize("entry_id", [True, 1.0, "1"])
def test_entry_ids_must_be_ints(
    repository: SqliteLedgerRepository, register: Register, entry_id: Any
) -> None:
    register()

    with pytest.raises(TypeError):
        GetEntryDetail(repository).execute(entry_id)


def test_a_history_page_holds_at_most_500_entries(
    repository: SqliteLedgerRepository, register: Register
) -> None:
    # The documented maximum, pinned as a literal rather than through the constant.
    register()

    assert len(ListHistory(repository).execute(limit=500)) == 1
    with pytest.raises(ValueError):
        ListHistory(repository).execute(limit=501)


class _NoHistory:
    """A repository that must never be asked for history."""

    def history(self, limit: int, before_id: int | None = None) -> list[Any]:
        raise AssertionError(f"invalid paging reached the repository: {limit}, {before_id}")


@pytest.mark.parametrize(
    ("limit", "before_id", "error"),
    [
        (501, None, ValueError),
        (True, None, TypeError),
        (10, 0, ValueError),
        (10, MAX_ENTRY_ID + 1, ValueError),
        (10, True, TypeError),
        (10, 2.0, TypeError),
    ],
)
def test_list_history_checks_paging_before_asking_the_repository(
    limit: Any, before_id: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        ListHistory(_NoHistory()).execute(limit=limit, before_id=before_id)
