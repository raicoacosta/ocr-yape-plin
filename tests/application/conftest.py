from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from payments_bot.adapters.persistence.sqlite_ledger import SqliteLedgerRepository
from payments_bot.application.clock import Clock
from payments_bot.application.payments import PaymentRegistered, RegisterPayment
from payments_bot.domain.ledger import EntrySource

FIXED_NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
DEFAULT_USER_ID = 1001


@pytest.fixture
def repository() -> Iterator[SqliteLedgerRepository]:
    repo = SqliteLedgerRepository(":memory:")
    yield repo
    repo.close()


@pytest.fixture
def clock() -> Clock:
    return lambda: FIXED_NOW


@pytest.fixture
def register(repository: SqliteLedgerRepository, clock: Clock) -> Callable[..., PaymentRegistered]:
    """Register a payment with sensible defaults; keyword overrides replace them."""
    use_case = RegisterPayment(repository, clock)

    def _register(**overrides: Any) -> PaymentRegistered:
        fields: dict[str, Any] = {
            "amount_minor": 25000,
            "source": EntrySource.OCR,
            "created_by": DEFAULT_USER_ID,
        }
        return use_case.execute(**(fields | overrides))

    return _register
