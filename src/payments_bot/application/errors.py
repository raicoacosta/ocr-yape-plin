"""Errors raised by use cases (domain rule violations raise `DomainError` instead)."""


class DuplicateImageError(Exception):
    """The receipt image was already registered; nothing was written."""

    def __init__(self, existing_entry_id: int) -> None:
        super().__init__(f"image already registered as entry {existing_entry_id}")
        self.existing_entry_id = existing_entry_id


class EntryNotFoundError(Exception):
    def __init__(self, entry_id: int) -> None:
        super().__init__(f"ledger entry {entry_id} does not exist")
        self.entry_id = entry_id
