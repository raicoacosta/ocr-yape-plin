from dataclasses import dataclass

from payments_bot.application.errors import EntryNotFoundError
from payments_bot.domain.ledger import Entry, effective_amount
from payments_bot.ports.ledger_repository import LedgerRepository


@dataclass(frozen=True, slots=True)
class EntryDetail:
    payment: Entry
    adjustments: tuple[Entry, ...]
    effective_amount: int


@dataclass(frozen=True, slots=True)
class GetBalance:
    repository: LedgerRepository

    def execute(self) -> dict[str, int]:
        return self.repository.balance()


@dataclass(frozen=True, slots=True)
class GetEntryDetail:
    repository: LedgerRepository

    def execute(self, entry_id: int) -> EntryDetail:
        payment = require_entry(self.repository, entry_id)
        adjustments = self.repository.adjustments_for(entry_id)
        return EntryDetail(payment, tuple(adjustments), effective_amount(payment, adjustments))


@dataclass(frozen=True, slots=True)
class ListHistory:
    repository: LedgerRepository

    def execute(self, limit: int = 10, before_id: int | None = None) -> list[Entry]:
        """Entries newest first; pass the last id of a page as `before_id` for the next one."""
        if limit < 1 or (before_id is not None and before_id < 1):
            raise ValueError(f"invalid page: limit={limit}, before_id={before_id}")
        return self.repository.history(limit, before_id)


def require_entry(repository: LedgerRepository, entry_id: int) -> Entry:
    entry = repository.get(entry_id)
    if entry is None:
        raise EntryNotFoundError(entry_id)
    return entry
