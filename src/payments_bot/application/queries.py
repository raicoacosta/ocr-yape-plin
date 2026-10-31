from dataclasses import dataclass

from payments_bot.application.errors import EntryNotFoundError
from payments_bot.domain.ledger import MAX_ENTRY_ID, Entry, effective_amount
from payments_bot.ports.ledger_repository import LedgerRepository

MAX_HISTORY_PAGE = 500
"""Most entries `ListHistory` returns at once; a larger page is refused, not truncated."""


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
        """Entries newest first; pass the last id of a page as `before_id` for the next one.

        `limit` is an int from 1 to `MAX_HISTORY_PAGE` and `before_id` one from 1 to
        `MAX_ENTRY_ID`. Anything else raises `TypeError` (not an int, or a bool) or
        `ValueError` (out of range).
        """
        _require_int_between("limit", limit, 1, MAX_HISTORY_PAGE)
        if before_id is not None:
            _require_int_between("before_id", before_id, 1, MAX_ENTRY_ID)
        return self.repository.history(limit, before_id)


def require_entry(repository: LedgerRepository, entry_id: int) -> Entry:
    """The entry with this id; an id no entry can have is simply not found."""
    _require_int("entry_id", entry_id)
    entry = repository.get(entry_id) if 1 <= entry_id <= MAX_ENTRY_ID else None
    if entry is None:
        raise EntryNotFoundError(entry_id)
    return entry


def _require_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")


def _require_int_between(name: str, value: int, low: int, high: int) -> None:
    _require_int(name, value)
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}: {value}")
