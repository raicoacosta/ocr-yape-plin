from dataclasses import dataclass
from datetime import datetime

from payments_bot.application.clock import Clock, utc_now
from payments_bot.application.errors import DuplicateImageError
from payments_bot.domain.ledger import Entry, EntryKind, EntrySource
from payments_bot.domain.money import DEFAULT_CURRENCY
from payments_bot.ports.ledger_repository import ImageAlreadyStoredError, LedgerRepository


@dataclass(frozen=True, slots=True)
class PaymentRegistered:
    entry: Entry
    possible_duplicates: tuple[int, ...]
    """Ids of earlier payments with the same receipt reference (operation number)."""
    balance: dict[str, int]


@dataclass(frozen=True, slots=True)
class RegisterPayment:
    repository: LedgerRepository
    clock: Clock = utc_now

    def execute(
        self,
        *,
        amount_minor: int,
        source: EntrySource,
        created_by: int,
        currency: str = DEFAULT_CURRENCY,
        image_sha256: str | None = None,
        image_key: str | None = None,
        reference: str | None = None,
        paid_at: datetime | None = None,
        extracted_json: str | None = None,
    ) -> PaymentRegistered:
        """Append a payment. A repeated image is refused; a repeated reference is flagged.

        The checks, the append and the resulting balance share one unit of work, so a
        concurrent registration of the same receipt is either refused or flagged.
        """
        entry = Entry(
            kind=EntryKind.PAYMENT,
            amount_minor=amount_minor,
            currency=currency,
            source=source,
            created_by=created_by,
            created_at=self.clock(),
            reference=_blank_to_none(reference),
            paid_at=paid_at,
            image_sha256=image_sha256,
            image_key=image_key,
            extracted_json=extracted_json,
        )
        with self.repository.atomic():
            self._refuse_known_image(entry)
            possible_duplicates = self._same_reference(entry)
            try:
                saved = self.repository.append(entry)
            except ImageAlreadyStoredError as error:
                raise DuplicateImageError(error.existing_entry_id) from error
            return PaymentRegistered(saved, possible_duplicates, self.repository.balance())

    def _refuse_known_image(self, entry: Entry) -> None:
        if entry.image_sha256 is None:
            return
        existing = self.repository.find_by_image_hash(entry.image_sha256)
        if existing is not None and existing.id is not None:
            raise DuplicateImageError(existing.id)

    def _same_reference(self, entry: Entry) -> tuple[int, ...]:
        if entry.reference is None:
            return ()
        payments = self.repository.find_payments_by_reference(entry.reference)
        return tuple(payment.id for payment in payments if payment.id is not None)


def _blank_to_none(text: str | None) -> str | None:
    if text is None or not text.strip():
        return None
    return text.strip()
