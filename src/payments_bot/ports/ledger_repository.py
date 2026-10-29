"""Storage port for the ledger.

The ledger is append-only: this port deliberately offers no update or delete operation.
Corrections are appended as adjustment entries.
"""

from contextlib import AbstractContextManager
from typing import Protocol

from payments_bot.domain.ledger import Entry


class ImageAlreadyStoredError(Exception):
    """`append` refused an entry because another entry already carries its image hash."""

    def __init__(self, existing_entry_id: int) -> None:
        super().__init__(f"image already stored as entry {existing_entry_id}")
        self.existing_entry_id = existing_entry_id


class LedgerRepository(Protocol):
    def atomic(self) -> AbstractContextManager[None]:
        """Unit of work: what the block reads and appends commits together, or not at all.

        Units of work are serialized against every other writer, so a decision computed from
        a read inside the block still holds when the resulting entry is appended. Blocks do
        not nest. Outside a block, each `append` commits on its own.
        """
        ...

    def append(self, entry: Entry) -> Entry:
        """Persist a new entry and return it with its assigned id.

        Raises `ImageAlreadyStoredError` when another entry already has the image hash.
        """
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
