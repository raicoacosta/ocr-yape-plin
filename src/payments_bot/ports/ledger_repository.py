"""Storage port for the ledger.

The ledger is append-only: this port deliberately offers no update or delete operation.
Corrections are appended as adjustment entries.
"""

from typing import Protocol

from payments_bot.domain.ledger import Entry


class LedgerRepository(Protocol):
    def append(self, entry: Entry) -> Entry:
        """Persist a new entry and return it with its assigned id."""
        ...

    def get(self, entry_id: int) -> Entry | None: ...

    def adjustments_for(self, payment_id: int) -> list[Entry]:
        """Adjustments that correct `payment_id`, oldest first."""
        ...

    def find_by_image_hash(self, image_sha256: str) -> Entry | None: ...

    def find_payments_by_reference(self, reference: str) -> list[Entry]:
        """Payments whose receipt carries this operation number, oldest first."""
        ...

    def balance(self) -> dict[str, int]:
        """Sum of every entry in minor units, per currency."""
        ...

    def history(self, limit: int, offset: int = 0) -> list[Entry]:
        """Entries newest first."""
        ...
