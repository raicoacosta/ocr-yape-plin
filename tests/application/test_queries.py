from collections.abc import Callable

import pytest

from payments_bot.adapters.persistence.sqlite_ledger import SqliteLedgerRepository
from payments_bot.application.clock import Clock
from payments_bot.application.corrections import CorrectEntry
from payments_bot.application.errors import EntryNotFoundError
from payments_bot.application.payments import PaymentRegistered
from payments_bot.application.queries import GetBalance, GetEntryDetail, ListHistory

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


def test_list_history_is_newest_first_and_pages(
    repository: SqliteLedgerRepository, register: Register
) -> None:
    ids = [register(amount_minor=amount).entry.id for amount in (100, 200, 300)]
    history = ListHistory(repository)

    assert [entry.id for entry in history.execute(limit=2)] == [ids[2], ids[1]]
    assert [entry.id for entry in history.execute(limit=2, offset=2)] == [ids[0]]


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (-1, 0), (10, -1)])
def test_list_history_rejects_invalid_paging(
    repository: SqliteLedgerRepository, limit: int, offset: int
) -> None:
    with pytest.raises(ValueError):
        ListHistory(repository).execute(limit=limit, offset=offset)
