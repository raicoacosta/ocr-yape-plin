import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from payments_bot.adapters.persistence.sqlite_ledger import SqliteLedgerRepository
from payments_bot.domain.ledger import Entry, EntryKind, EntrySource, balance

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
LIMA = timezone(-timedelta(hours=5))
USER_ID = 1001
HASH_A = "a" * 64
HASH_B = "b" * 64


def payment(amount_minor: int = 25000, **overrides: Any) -> Entry:
    fields: dict[str, Any] = {
        "kind": EntryKind.PAYMENT,
        "amount_minor": amount_minor,
        "currency": "PEN",
        "source": EntrySource.OCR,
        "created_by": USER_ID,
        "created_at": NOW,
    }
    return Entry(**(fields | overrides))


def adjustment(amount_minor: int, corrects_id: int, **overrides: Any) -> Entry:
    fields: dict[str, Any] = {
        "kind": EntryKind.ADJUSTMENT,
        "amount_minor": amount_minor,
        "currency": "PEN",
        "source": EntrySource.MANUAL,
        "created_by": USER_ID,
        "created_at": NOW,
        "corrects_id": corrects_id,
        "reason": "wrong amount read",
    }
    return Entry(**(fields | overrides))


def insert_raw(connection: sqlite3.Connection, **columns: Any) -> None:
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    with connection:
        connection.execute(
            f"INSERT INTO ledger_entries ({names}) VALUES ({placeholders})",
            tuple(columns.values()),
        )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def repository(db_path: Path) -> Iterator[SqliteLedgerRepository]:
    repo = SqliteLedgerRepository(db_path)
    yield repo
    repo.close()


@pytest.fixture
def raw(db_path: Path, repository: SqliteLedgerRepository) -> Iterator[sqlite3.Connection]:
    """A second connection that bypasses the adapter: the database itself must refuse edits."""
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


class TestPersistence:
    def test_append_assigns_id_and_get_round_trips_every_field(
        self, repository: SqliteLedgerRepository
    ) -> None:
        entry = payment(
            reference="00123456",
            paid_at=datetime(2026, 10, 28, 15, 30, tzinfo=LIMA),
            image_sha256=HASH_A,
            image_key="receipts/a.jpg",
            extracted_json='{"amount": "250"}',
        )

        saved = repository.append(entry)

        assert isinstance(saved.id, int)
        assert repository.get(saved.id) == saved

    def test_get_returns_none_for_unknown_id(self, repository: SqliteLedgerRepository) -> None:
        assert repository.get(999) is None

    def test_append_refuses_an_already_persisted_entry(
        self, repository: SqliteLedgerRepository
    ) -> None:
        saved = repository.append(payment())

        with pytest.raises(ValueError, match="already persisted"):
            repository.append(saved)

    def test_schema_creation_is_idempotent(
        self, db_path: Path, repository: SqliteLedgerRepository
    ) -> None:
        saved = repository.append(payment())

        reopened = SqliteLedgerRepository(db_path)
        try:
            assert reopened.get(saved.id) == saved
        finally:
            reopened.close()

    def test_file_database_uses_write_ahead_log(self, raw: sqlite3.Connection) -> None:
        (mode,) = raw.execute("PRAGMA journal_mode").fetchone()

        assert mode == "wal"

    def test_in_memory_database_is_supported(self) -> None:
        repo = SqliteLedgerRepository(":memory:")
        try:
            saved = repo.append(payment())
            assert repo.get(saved.id) == saved
        finally:
            repo.close()


class TestAppendOnlyEnforcement:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE ledger_entries SET amount_minor = 1 WHERE id = 1",
            "UPDATE ledger_entries SET reason = 'edited'",
        ],
    )
    def test_update_is_refused(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection, statement: str
    ) -> None:
        saved = repository.append(payment())

        with pytest.raises(sqlite3.IntegrityError, match="ledger is append-only"), raw:
            raw.execute(statement)

        assert repository.get(1) == saved

    @pytest.mark.parametrize(
        "statement", ["DELETE FROM ledger_entries WHERE id = 1", "DELETE FROM ledger_entries"]
    )
    def test_delete_is_refused(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection, statement: str
    ) -> None:
        repository.append(payment())

        with pytest.raises(sqlite3.IntegrityError, match="ledger is append-only"), raw:
            raw.execute(statement)

        assert repository.get(1) is not None

    def test_insert_or_replace_cannot_overwrite_by_id(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        repository.append(payment(25000))

        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT OR REPLACE INTO ledger_entries"
                " (id, kind, amount_minor, currency, source, created_by, created_at)"
                " VALUES (1, 'payment', 1, 'PEN', 'ocr', 1, ?)",
                (NOW.isoformat(),),
            )

        assert repository.balance() == {"PEN": 25000}

    def test_insert_or_replace_cannot_overwrite_by_image_hash(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        repository.append(payment(25000, image_sha256=HASH_A))

        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT OR REPLACE INTO ledger_entries"
                " (kind, amount_minor, currency, source, created_by, created_at, image_sha256)"
                " VALUES ('payment', 1, 'PEN', 'ocr', 1, ?, ?)",
                (NOW.isoformat(), HASH_A),
            )

        assert repository.balance() == {"PEN": 25000}

    def test_duplicate_image_hash_is_rejected(self, repository: SqliteLedgerRepository) -> None:
        repository.append(payment(25000, image_sha256=HASH_A))

        with pytest.raises(sqlite3.IntegrityError):
            repository.append(payment(4000, image_sha256=HASH_A))

        assert repository.balance() == {"PEN": 25000}

    def test_adjustment_cannot_target_another_adjustment(
        self, repository: SqliteLedgerRepository
    ) -> None:
        original = repository.append(payment())
        first_fix = repository.append(adjustment(-5000, corrects_id=original.id))

        with pytest.raises(sqlite3.IntegrityError, match="adjustment must target"):
            repository.append(adjustment(-100, corrects_id=first_fix.id))

    def test_adjustment_cannot_target_a_missing_entry(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError, match="adjustment must target"):
            insert_raw(
                raw,
                kind="adjustment",
                amount_minor=-100,
                currency="PEN",
                source="manual",
                created_by=USER_ID,
                created_at=NOW.isoformat(),
                corrects_id=999,
                reason="no such payment",
            )

    def test_adjustment_must_use_the_payment_currency(
        self, repository: SqliteLedgerRepository
    ) -> None:
        original = repository.append(payment(currency="PEN"))

        with pytest.raises(sqlite3.IntegrityError, match="adjustment must target"):
            repository.append(adjustment(-100, corrects_id=original.id, currency="USD"))

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"amount_minor": 0}, id="payment-zero"),
            pytest.param({"amount_minor": -100}, id="payment-negative"),
            pytest.param({"amount_minor": 1.5}, id="payment-fractional"),
            pytest.param({"corrects_id": 1}, id="payment-with-target"),
            pytest.param({"kind": "refund"}, id="unknown-kind"),
            pytest.param({"source": "email"}, id="unknown-source"),
            pytest.param({"currency": "PE"}, id="short-currency"),
            pytest.param(
                {"kind": "adjustment", "source": "manual", "corrects_id": 1, "reason": None},
                id="adjustment-without-reason",
            ),
            pytest.param(
                {
                    "kind": "adjustment",
                    "source": "manual",
                    "corrects_id": 1,
                    "reason": "x",
                    "amount_minor": 0,
                },
                id="adjustment-zero",
            ),
            pytest.param(
                {"kind": "adjustment", "source": "ocr", "corrects_id": 1, "reason": "x"},
                id="adjustment-not-manual",
            ),
            pytest.param(
                {
                    "kind": "adjustment",
                    "source": "manual",
                    "corrects_id": 1,
                    "reason": "x",
                    "image_sha256": HASH_B,
                },
                id="adjustment-with-image",
            ),
        ],
    )
    def test_check_constraints_reject_inconsistent_rows(
        self,
        repository: SqliteLedgerRepository,
        raw: sqlite3.Connection,
        overrides: dict[str, Any],
    ) -> None:
        repository.append(payment())
        row = {
            "kind": "payment",
            "amount_minor": 100,
            "currency": "PEN",
            "source": "ocr",
            "created_by": USER_ID,
            "created_at": NOW.isoformat(),
        }

        with pytest.raises(sqlite3.IntegrityError):
            insert_raw(raw, **(row | overrides))


class TestQueries:
    def test_balance_is_the_sum_of_entries_per_currency(
        self, repository: SqliteLedgerRepository
    ) -> None:
        first = repository.append(payment(25000))
        repository.append(payment(4000))
        repository.append(payment(1000, currency="USD"))
        repository.append(adjustment(-5000, corrects_id=first.id))

        assert repository.balance() == {"PEN": 24000, "USD": 1000}
        assert repository.balance() == balance(repository.history(limit=100))

    def test_balance_of_an_empty_ledger_is_empty(self, repository: SqliteLedgerRepository) -> None:
        assert repository.balance() == {}

    def test_history_is_newest_first_and_pages(self, repository: SqliteLedgerRepository) -> None:
        ids = [repository.append(payment(amount)).id for amount in (100, 200, 300)]

        assert [entry.id for entry in repository.history(limit=2)] == [ids[2], ids[1]]
        assert [entry.id for entry in repository.history(limit=2, offset=2)] == [ids[0]]

    def test_adjustments_for_returns_only_that_payments_adjustments(
        self, repository: SqliteLedgerRepository
    ) -> None:
        first = repository.append(payment(25000))
        second = repository.append(payment(4000))
        fix_a = repository.append(adjustment(-5000, corrects_id=first.id))
        repository.append(adjustment(100, corrects_id=second.id))
        fix_b = repository.append(adjustment(1000, corrects_id=first.id))

        assert repository.adjustments_for(first.id) == [fix_a, fix_b]

    def test_find_by_image_hash(self, repository: SqliteLedgerRepository) -> None:
        saved = repository.append(payment(image_sha256=HASH_A))

        assert repository.find_by_image_hash(HASH_A) == saved
        assert repository.find_by_image_hash(HASH_B) is None

    def test_find_payments_by_reference(self, repository: SqliteLedgerRepository) -> None:
        first = repository.append(payment(25000, reference="00123456"))
        repository.append(payment(4000, reference="00999999"))
        second = repository.append(payment(25000, reference="00123456"))

        assert repository.find_payments_by_reference("00123456") == [first, second]
        assert repository.find_payments_by_reference("missing") == []
