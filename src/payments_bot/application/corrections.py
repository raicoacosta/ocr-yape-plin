"""Corrections never edit history: each one appends an adjustment to the payment.

Each use case reads, decides and appends inside one unit of work, so two corrections of the
same payment can never both be computed from the same, soon stale, effective amount.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from payments_bot.application.clock import Clock, utc_now
from payments_bot.application.queries import require_entry
from payments_bot.domain.ledger import Entry, build_correction, build_void, effective_amount
from payments_bot.ports.ledger_repository import LedgerRepository


@dataclass(frozen=True, slots=True)
class AdjustmentRecorded:
    adjustment: Entry
    effective_amount: int
    """The corrected payment's amount after this adjustment."""
    balance: dict[str, int]


@dataclass(frozen=True, slots=True)
class CorrectEntry:
    repository: LedgerRepository
    clock: Clock = utc_now

    def execute(
        self, *, entry_id: int, new_amount_minor: int, reason: str, created_by: int
    ) -> AdjustmentRecorded:
        with self.repository.atomic():
            payment = require_entry(self.repository, entry_id)
            adjustments = self.repository.adjustments_for(entry_id)
            adjustment = build_correction(
                payment,
                adjustments,
                new_amount_minor=new_amount_minor,
                reason=reason,
                created_by=created_by,
                created_at=self.clock(),
            )
            return _record(self.repository, payment, adjustments, adjustment)


@dataclass(frozen=True, slots=True)
class VoidEntry:
    repository: LedgerRepository
    clock: Clock = utc_now

    def execute(self, *, entry_id: int, reason: str, created_by: int) -> AdjustmentRecorded:
        with self.repository.atomic():
            payment = require_entry(self.repository, entry_id)
            adjustments = self.repository.adjustments_for(entry_id)
            adjustment = build_void(
                payment,
                adjustments,
                reason=reason,
                created_by=created_by,
                created_at=self.clock(),
            )
            return _record(self.repository, payment, adjustments, adjustment)


def _record(
    repository: LedgerRepository,
    payment: Entry,
    previous: Sequence[Entry],
    adjustment: Entry,
) -> AdjustmentRecorded:
    saved = repository.append(adjustment)
    return AdjustmentRecorded(
        adjustment=saved,
        effective_amount=effective_amount(payment, [*previous, saved]),
        balance=repository.balance(),
    )
