import math
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from payments_bot.adapters.persistence import sqlite_ledger
from payments_bot.adapters.persistence.sqlite_ledger import (
    SCHEMA_VERSION,
    SqliteLedgerRepository,
    UnsupportedSchemaError,
)
from payments_bot.domain.ledger import Entry, EntryKind, EntrySource, balance
from payments_bot.domain.money import MAX_AMOUNT_MINOR
from payments_bot.ports.ledger_repository import ImageAlreadyStoredError, LedgerBusyError

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
LIMA = timezone(-timedelta(hours=5))
USER_ID = 1001
HASH_A = "a" * 64
HASH_B = "b" * 64
TS = "2026-10-29T15:00:00.000000+00:00"
"""`NOW` exactly as the adapter stores it: UTC, microseconds, `+00:00`."""
PYTHON_WHITESPACE = [chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace()]
LEDGER_TABLE = "CREATE TABLE ledger_entries (id INTEGER PRIMARY KEY)"
OTHER_APP_TABLE = "CREATE TABLE notes (body TEXT)"
LONG_AGO_NS = 1_000_000_000 * 1_000_000_000
"""An mtime (2001-09-09) no write made during a test can keep."""
BUSY_CODES = [
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_BUSY_RECOVERY,
    sqlite3.SQLITE_BUSY_SNAPSHOT,
    sqlite3.SQLITE_BUSY_TIMEOUT,
    sqlite3.SQLITE_LOCKED_SHAREDCACHE,
    sqlite3.SQLITE_LOCKED_VTAB,
]
"""SQLite's busy and locked result codes, primary and extended."""


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


def raw_payment(**overrides: Any) -> dict[str, Any]:
    """Columns of a valid payment row, as a raw INSERT would write them."""
    row = {
        "kind": "payment",
        "amount_minor": 100,
        "currency": "PEN",
        "source": "ocr",
        "created_by": USER_ID,
        "created_at": TS,
    }
    return row | overrides


def raw_adjustment(corrects_id: int, **overrides: Any) -> dict[str, Any]:
    """Columns of a valid adjustment row, as a raw INSERT would write them."""
    row = raw_payment(kind="adjustment", source="manual", corrects_id=corrects_id, reason="fix")
    return row | {"amount_minor": -100} | overrides


def insert_raw(connection: sqlite3.Connection, **columns: Any) -> None:
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    with connection:
        connection.execute(
            f"INSERT INTO ledger_entries ({names}) VALUES ({placeholders})",
            tuple(columns.values()),
        )


def hold_write_lock(path: Path) -> sqlite3.Connection:
    """A connection that holds the database's write lock until it rolls back or closes."""
    holder = sqlite3.connect(path, autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def rollback_journal_file(path: Path, statements: list[str]) -> Path:
    """A database file in SQLite's default rollback-journal mode, last modified long ago."""
    connection = sqlite3.connect(path, autocommit=True)
    for statement in statements:
        connection.execute(statement)
    connection.close()
    os.utime(path, ns=(LONG_AGO_NS, LONG_AGO_NS))
    return path


def file_state(path: Path) -> tuple[bytes, int, list[str]]:
    """Everything an open could change: the bytes, the mtime and the files beside it."""
    return path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir())


def open_in_thread(path: Path, busy_timeout: float) -> tuple[threading.Thread, dict[str, Any]]:
    """Open `path` in a daemon thread, so a test can bound how long the open may take."""
    result: dict[str, Any] = {}

    def run() -> None:
        started = time.monotonic()
        try:
            SqliteLedgerRepository(path, busy_timeout=busy_timeout).close()
            result["outcome"] = "opened"
        except Exception as error:  # noqa: BLE001 - the error is the outcome under test
            result["outcome"] = error
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


class CraftedErrorConnection(sqlite3.Connection):
    """A real connection whose statements starting with `fail_on` raise a crafted error.

    SQLite only reports some codes (SQLITE_LOCKED, extended busy codes, a busy COMMIT) under
    conditions a test cannot set up reliably, so the error is made up with the code it has.
    """

    fail_on: str | None = None
    error_code: int | None = None

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if self.fail_on is not None and sql.lstrip().upper().startswith(self.fail_on):
            error = sqlite3.OperationalError(f"crafted error, code {self.error_code}")
            if self.error_code is not None:
                error.sqlite_errorcode = self.error_code
            raise error
        return super().execute(sql, parameters)


def open_at_once(path: Path, openers: int) -> list[str]:
    """Open `path` from `openers` threads released together; return each outcome."""
    barrier = threading.Barrier(openers)
    outcomes = ["not run"] * openers

    def run(index: int) -> None:
        barrier.wait(timeout=10)
        try:
            SqliteLedgerRepository(path).close()
            outcomes[index] = "opened"
        except Exception as error:  # noqa: BLE001 - the error is the outcome under test
            outcomes[index] = f"{type(error).__name__}: {error}"

    threads = [threading.Thread(target=run, args=(index,)) for index in range(openers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    return outcomes


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

    def test_timestamps_are_stored_in_one_fixed_utc_format(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        repository.append(payment(paid_at=datetime(2026, 10, 28, 15, 30, tzinfo=LIMA)))

        stored = raw.execute("SELECT created_at, paid_at FROM ledger_entries").fetchone()

        assert stored == (TS, "2026-10-28T20:30:00.000000+00:00")

    @pytest.mark.parametrize(
        ("moment", "stored_text"),
        [
            (datetime(1000, 1, 1, tzinfo=UTC), "1000-01-01T00:00:00.000000+00:00"),
            (
                datetime(9999, 12, 31, 18, 59, 59, 999999, tzinfo=LIMA),
                "9999-12-31T23:59:59.999999+00:00",
            ),
        ],
    )
    def test_every_moment_the_domain_accepts_is_stored_and_read_back(
        self,
        repository: SqliteLedgerRepository,
        raw: sqlite3.Connection,
        moment: datetime,
        stored_text: str,
    ) -> None:
        saved = repository.append(
            payment(created_at=moment.astimezone(UTC), paid_at=moment, created_by=2**63 - 1)
        )

        assert raw.execute("SELECT created_at, paid_at FROM ledger_entries").fetchone() == (
            stored_text,
            stored_text,
        )
        assert repository.get(saved.id) == saved

    def test_raw_rows_in_the_stored_format_are_accepted_and_readable(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        insert_raw(raw, **raw_payment(paid_at=TS, image_sha256=HASH_A, reason=" note "))
        insert_raw(raw, **raw_adjustment(1, reason="  misread  ", amount_minor=-100))

        history = repository.history(limit=10)

        assert [entry.kind for entry in history] == [EntryKind.ADJUSTMENT, EntryKind.PAYMENT]
        assert repository.balance() == {"PEN": 0}

    def test_text_that_is_not_utf8_is_read_with_replacement_characters(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        # CAST(... AS TEXT) stores raw bytes as TEXT without validating them; such a row
        # must not make every later read fail to decode.
        with raw:
            raw.execute(
                "INSERT INTO ledger_entries"
                " (kind, amount_minor, currency, source, created_by, created_at, reason)"
                " VALUES ('payment', 100, 'PEN', 'ocr', 1, ?, CAST(x'ff41' AS TEXT))",
                (TS,),
            )

        (entry,) = repository.history(limit=10)

        assert entry.reason == "�A"

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
                (TS,),
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
                (TS, HASH_A),
            )

        assert repository.balance() == {"PEN": 25000}

    def test_duplicate_image_hash_is_rejected_with_the_existing_id(
        self, repository: SqliteLedgerRepository
    ) -> None:
        original = repository.append(payment(25000, image_sha256=HASH_A))

        with pytest.raises(ImageAlreadyStoredError) as excinfo:
            repository.append(payment(4000, image_sha256=HASH_A))

        assert excinfo.value.existing_entry_id == original.id
        assert repository.balance() == {"PEN": 25000}

    def test_adjustment_cannot_make_the_payment_negative(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        original = repository.append(payment(25000))
        repository.append(adjustment(-20000, corrects_id=original.id))

        with pytest.raises(sqlite3.IntegrityError, match="payment amount negative"):
            repository.append(adjustment(-5001, corrects_id=original.id))
        with pytest.raises(sqlite3.IntegrityError, match="payment amount negative"):
            insert_raw(
                raw,
                kind="adjustment",
                amount_minor=-30000,
                currency="PEN",
                source="manual",
                created_by=USER_ID,
                created_at=TS,
                corrects_id=original.id,
                reason="overshoot",
            )

        repository.append(adjustment(-5000, corrects_id=original.id))
        assert repository.balance() == {"PEN": 0}

    def test_first_adjustment_cannot_overshoot_below_zero(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        original = repository.append(payment(25000))

        with pytest.raises(sqlite3.IntegrityError, match="payment amount negative"):
            repository.append(adjustment(-25001, corrects_id=original.id))
        with pytest.raises(sqlite3.IntegrityError, match="payment amount negative"):
            insert_raw(raw, **raw_adjustment(original.id, amount_minor=-30000))

        assert repository.adjustments_for(original.id) == []

    def test_adjustment_cannot_take_the_payment_above_the_maximum(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        original = repository.append(payment(25000))
        repository.append(adjustment(MAX_AMOUNT_MINOR - 25000, corrects_id=original.id))

        with pytest.raises(sqlite3.IntegrityError, match="payment amount exceed the maximum"):
            repository.append(adjustment(1, corrects_id=original.id))
        with pytest.raises(sqlite3.IntegrityError, match="payment amount exceed the maximum"):
            insert_raw(raw, **raw_adjustment(original.id, amount_minor=MAX_AMOUNT_MINOR))

        # A payment held at the maximum can still be voided by one in-range adjustment.
        repository.append(adjustment(-MAX_AMOUNT_MINOR, corrects_id=original.id))
        assert repository.balance() == {"PEN": 0}

    def test_first_adjustment_cannot_overshoot_above_the_maximum(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        biggest = repository.append(payment(MAX_AMOUNT_MINOR))
        smallest = repository.append(payment(1))

        with pytest.raises(sqlite3.IntegrityError, match="payment amount exceed the maximum"):
            insert_raw(raw, **raw_adjustment(biggest.id, amount_minor=MAX_AMOUNT_MINOR))
        with pytest.raises(sqlite3.IntegrityError, match="payment amount exceed the maximum"):
            repository.append(adjustment(MAX_AMOUNT_MINOR, corrects_id=smallest.id))

        assert repository.balance() == {"PEN": MAX_AMOUNT_MINOR + 1}

    def test_adjustment_amount_is_bounded_below_by_minus_the_maximum(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        biggest = repository.append(payment(MAX_AMOUNT_MINOR))

        with pytest.raises(sqlite3.IntegrityError):
            insert_raw(raw, **raw_adjustment(biggest.id, amount_minor=-MAX_AMOUNT_MINOR - 1))
        insert_raw(raw, **raw_adjustment(biggest.id, amount_minor=-MAX_AMOUNT_MINOR))

        assert repository.balance() == {"PEN": 0}

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
                created_at=TS,
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
            "created_at": TS,
        }

        with pytest.raises(sqlite3.IntegrityError):
            insert_raw(raw, **(row | overrides))


class TestRowsTheDomainCouldNotReadAreRefused:
    """Rows cannot be deleted, so any row the domain would refuse to load breaks history."""

    @pytest.mark.parametrize(
        ("overrides", "constraint"),
        [
            pytest.param({"id": 0}, "id_positive", id="id-zero"),
            pytest.param({"id": -1}, "id_positive", id="id-negative"),
            pytest.param({"amount_minor": MAX_AMOUNT_MINOR + 1}, "amount_in_range", id="huge"),
            pytest.param(
                {"amount_minor": -MAX_AMOUNT_MINOR - 1}, "amount_in_range", id="huge-negative"
            ),
            pytest.param({"currency": "pen"}, "currency_iso_4217", id="currency-lowercase"),
            pytest.param({"currency": "123"}, "currency_iso_4217", id="currency-digits"),
            pytest.param({"image_sha256": HASH_A.upper()}, "image_sha256_hex", id="hash-upper"),
            pytest.param({"image_sha256": "not-a-hash"}, "image_sha256_hex", id="hash-garbage"),
            pytest.param({"image_sha256": "a" * 63}, "image_sha256_hex", id="hash-short"),
            pytest.param({"image_sha256": "a" * 65}, "image_sha256_hex", id="hash-long"),
            # GLOB and length() stop at the first NUL, so a valid prefix must not pass for
            # the whole value.
            pytest.param({"kind": "payment\x00x"}, "kind IN", id="kind-nul"),
            pytest.param({"source": "ocr\x00x"}, "source IN", id="source-nul"),
            pytest.param({"currency": "PEN\x00x"}, "currency_iso_4217", id="currency-nul"),
            pytest.param(
                {"image_sha256": HASH_A + "\x00ZZ"}, "image_sha256_hex", id="hash-nul-suffix"
            ),
            pytest.param(
                {"created_at": TS + "\x00junk"}, "created_at_utc_text", id="ts-nul-suffix"
            ),
            pytest.param({"paid_at": TS + "\x00junk"}, "paid_at_utc_text", id="paid-nul-suffix"),
            pytest.param({"reason": "note\x00"}, "reason_no_nul", id="reason-nul"),
            pytest.param({"reference": "00012345\x00"}, "reference_no_nul", id="reference-nul"),
            pytest.param({"image_key": "a.jpg\x00"}, "image_key_no_nul", id="image-key-nul"),
            pytest.param({"extracted_json": "{}\x00"}, "extracted_json_no_nul", id="json-nul"),
            pytest.param({"created_at": "yesterday"}, "created_at_utc_text", id="ts-garbage"),
            pytest.param(
                {"created_at": "2026-10-29T15:00:00+00:00"},
                "created_at_utc_text",
                id="ts-without-microseconds",
            ),
            pytest.param(
                {"created_at": "2026-10-29T10:00:00.000000-05:00"},
                "created_at_utc_text",
                id="ts-not-utc",
            ),
            pytest.param(
                {"created_at": "2026-10-29 15:00:00.000000+00:00"},
                "created_at_utc_text",
                id="ts-space-separator",
            ),
            pytest.param(
                {"created_at": "2026-02-31T15:00:00.000000+00:00"},
                "created_at_utc_text",
                id="ts-impossible-day",
            ),
            pytest.param(
                {"created_at": "2026-13-01T15:00:00.000000+00:00"},
                "created_at_utc_text",
                id="ts-impossible-month",
            ),
            pytest.param(
                {"created_at": "2026-10-29T24:00:00.000000+00:00"},
                "created_at_utc_text",
                id="ts-hour-24",
            ),
            pytest.param({"paid_at": "2026-10-28"}, "paid_at_utc_text", id="paid-date-only"),
            pytest.param(
                {"paid_at": "2026-10-28T15:30:00.000000-05:00"},
                "paid_at_utc_text",
                id="paid-not-utc",
            ),
            pytest.param({"reason": ""}, "reason_not_blank", id="payment-reason-empty"),
            pytest.param({"reason": " \t\u3000\n"}, "reason_not_blank", id="payment-reason-blank"),
            pytest.param({"created_by": 0}, "created_by_positive", id="created-by-zero"),
            pytest.param({"created_by": -1}, "created_by_positive", id="created-by-negative"),
        ],
    )
    def test_payment_row(
        self,
        repository: SqliteLedgerRepository,
        raw: sqlite3.Connection,
        overrides: dict[str, Any],
        constraint: str,
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError, match=constraint):
            insert_raw(raw, **raw_payment(**overrides))

        assert repository.history(limit=10) == []

    @pytest.mark.parametrize(
        ("overrides", "constraint"),
        [
            pytest.param({"reason": ""}, "reason_not_blank", id="reason-empty"),
            pytest.param({"reason": "   "}, "reason_not_blank", id="reason-blank"),
            pytest.param({"reason": "fix\x00"}, "reason_no_nul", id="reason-nul"),
            pytest.param({"reason": None}, "entry_kind_shape", id="reason-missing"),
            pytest.param({"image_key": "receipts/x.jpg"}, "entry_kind_shape", id="image-key-only"),
            pytest.param({"image_sha256": HASH_B}, "entry_kind_shape", id="image-hash"),
        ],
    )
    def test_adjustment_row(
        self,
        repository: SqliteLedgerRepository,
        raw: sqlite3.Connection,
        overrides: dict[str, Any],
        constraint: str,
    ) -> None:
        original = repository.append(payment())

        with pytest.raises(sqlite3.IntegrityError, match=constraint):
            insert_raw(raw, **raw_adjustment(original.id, **overrides))

        assert repository.history(limit=10) == [original]

    @pytest.mark.parametrize(
        "blank", PYTHON_WHITESPACE, ids=[f"U+{ord(char):04X}" for char in PYTHON_WHITESPACE]
    )
    def test_reason_made_of_any_python_whitespace_is_refused(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection, blank: str
    ) -> None:
        original = repository.append(payment())

        with pytest.raises(sqlite3.IntegrityError, match="reason_not_blank"):
            insert_raw(raw, **raw_adjustment(original.id, reason=blank * 2))

    def test_payment_may_reach_the_maximum_amount(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        insert_raw(raw, **raw_payment(amount_minor=MAX_AMOUNT_MINOR))
        insert_raw(raw, **raw_payment(amount_minor=MAX_AMOUNT_MINOR))

        assert repository.balance() == {"PEN": 2 * MAX_AMOUNT_MINOR}

    def test_a_refused_negative_id_does_not_block_later_appends(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError, match="id_positive"):
            insert_raw(raw, **raw_payment(id=-1))

        saved = repository.append(payment())

        assert saved.id == 1


class TestSchemaVersion:
    def test_new_database_records_the_schema_version(self, raw: sqlite3.Connection) -> None:
        assert SCHEMA_VERSION == 3
        assert raw.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)

    @pytest.mark.parametrize("version", [1, 2])
    def test_database_from_an_older_version_is_refused_and_left_untouched(
        self, tmp_path: Path, version: int
    ) -> None:
        older = tmp_path / "older.db"
        connection = sqlite3.connect(older)
        connection.execute("CREATE TABLE ledger_entries (id INTEGER PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.close()

        with pytest.raises(UnsupportedSchemaError, match=f"version {version};"):
            SqliteLedgerRepository(older)

        connection = sqlite3.connect(older)
        assert connection.execute("PRAGMA user_version").fetchone() == (version,)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("ledger_entries",)
        ]
        connection.close()

    def test_database_from_a_newer_version_is_refused(
        self, db_path: Path, raw: sqlite3.Connection
    ) -> None:
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

        with pytest.raises(UnsupportedSchemaError, match=f"version {SCHEMA_VERSION + 1}"):
            SqliteLedgerRepository(db_path)

    def test_unversioned_ledger_is_refused_instead_of_being_stamped(self, tmp_path: Path) -> None:
        legacy = tmp_path / "legacy.db"
        connection = sqlite3.connect(legacy)
        connection.execute("CREATE TABLE ledger_entries (id INTEGER PRIMARY KEY)")
        connection.close()

        with pytest.raises(UnsupportedSchemaError, match="no schema version"):
            SqliteLedgerRepository(legacy)

        connection = sqlite3.connect(legacy)
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        connection.close()

    @pytest.mark.parametrize(
        ("statements", "message"),
        [
            pytest.param([LEDGER_TABLE, "PRAGMA user_version = 1"], "version 1;", id="version-1"),
            pytest.param([LEDGER_TABLE, "PRAGMA user_version = 2"], "version 2;", id="version-2"),
            pytest.param([LEDGER_TABLE, "PRAGMA user_version = 4"], "version 4;", id="newer"),
            pytest.param([LEDGER_TABLE], "no schema version", id="unversioned-ledger"),
            pytest.param([OTHER_APP_TABLE], "not a ledger", id="other-app-unversioned"),
            pytest.param(
                [OTHER_APP_TABLE, "PRAGMA user_version = 7"], "version 7;", id="other-app-versioned"
            ),
        ],
    )
    def test_refused_rollback_journal_file_is_left_byte_for_byte_untouched(
        self, tmp_path: Path, statements: list[str], message: str
    ) -> None:
        # Switching to WAL rewrites the file header, so it must wait until the file is known
        # to be this ledger, or empty.
        refused = rollback_journal_file(tmp_path / "refused.db", statements)
        before = file_state(refused)

        with pytest.raises(UnsupportedSchemaError, match=message):
            SqliteLedgerRepository(refused)

        assert file_state(refused) == before

    def test_file_another_program_fills_while_opening_is_refused(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Between the first look (empty) and the write lock, another program creates its own
        # table: the look is repeated under the lock, so the ledger never moves in with it.
        real_connect = sqlite3.connect

        class InterleavedConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql == "BEGIN IMMEDIATE":
                    other = real_connect(db_path, autocommit=True)
                    other.execute(OTHER_APP_TABLE)
                    other.close()
                return super().execute(sql, parameters)

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, factory=InterleavedConnection, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            with pytest.raises(UnsupportedSchemaError, match="not a ledger"):
                SqliteLedgerRepository(db_path)

        connection = sqlite3.connect(db_path)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("notes",)]
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        connection.close()

    def test_current_ledger_in_rollback_journal_mode_is_switched_to_wal(
        self, db_path: Path
    ) -> None:
        SqliteLedgerRepository(db_path).close()
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.close()

        SqliteLedgerRepository(db_path).close()

        connection = sqlite3.connect(db_path)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        connection.close()

    def test_empty_existing_file_becomes_a_new_ledger(self, db_path: Path) -> None:
        db_path.touch()

        repository = SqliteLedgerRepository(db_path)
        try:
            assert repository.append(payment()).id == 1
        finally:
            repository.close()


class TestBusyDatabase:
    """Another connection holds the write lock: wait for it, then fail with a port error."""

    def test_first_open_retries_while_another_connection_locks_the_new_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Switching to WAL does not wait on the busy timeout: SQLite answers "database is
        # locked" at once to avoid a deadlock, so the opener must retry by itself.
        new_file = tmp_path / "new.db"
        holder = hold_write_lock(new_file)
        pauses: list[float] = []

        def release_on_first_pause(seconds: float) -> None:
            pauses.append(seconds)
            if holder.in_transaction:
                holder.execute("ROLLBACK")

        monkeypatch.setattr(sqlite_ledger, "sleep", release_on_first_pause)
        try:
            repository = SqliteLedgerRepository(new_file)
        finally:
            holder.close()

        try:
            assert pauses
            assert repository.append(payment()).id == 1
        finally:
            repository.close()
        check = sqlite3.connect(new_file)
        assert check.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert check.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        check.close()

    def test_first_open_gives_up_with_ledger_busy_after_the_busy_timeout(
        self, tmp_path: Path
    ) -> None:
        new_file = tmp_path / "new.db"
        holder = hold_write_lock(new_file)
        try:
            # In a thread, so an open that never gives up fails the test instead of hanging it.
            thread, result = open_in_thread(new_file, busy_timeout=0.2)
            thread.join(timeout=5)
            gave_up = not thread.is_alive()
        finally:
            holder.close()
            thread.join(timeout=5)

        assert gave_up, "the open kept retrying long past its busy timeout"
        assert isinstance(result["outcome"], LedgerBusyError)
        assert 0.2 <= result["elapsed"] < 2.0
        repository = SqliteLedgerRepository(new_file)
        try:
            assert repository.append(payment()).id == 1
        finally:
            repository.close()

    def test_zero_busy_timeout_gives_up_at_once(self, tmp_path: Path) -> None:
        new_file = tmp_path / "new.db"
        holder = hold_write_lock(new_file)
        try:
            started = time.monotonic()
            with pytest.raises(LedgerBusyError):
                SqliteLedgerRepository(new_file, busy_timeout=0)
            elapsed = time.monotonic() - started
        finally:
            holder.close()

        assert elapsed < 1.0

    @pytest.mark.parametrize("busy_timeout", [math.nan, math.inf, -math.inf, -0.001])
    def test_busy_timeout_must_be_finite_and_not_negative(
        self, db_path: Path, busy_timeout: float
    ) -> None:
        # NaN used to make the open retry forever: no time is ever past a NaN deadline.
        with pytest.raises(ValueError, match="busy_timeout"):
            SqliteLedgerRepository(db_path, busy_timeout=busy_timeout)

        assert not db_path.exists()

    def test_last_open_attempt_waits_only_for_the_time_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The first attempt fails at once (the WAL switch does not wait). By the second one,
        # 90% of the budget is gone and the holder blocks even reads, so the attempt waits
        # inside SQLite's busy handler: for the time left, not for a whole busy timeout.
        new_file = tmp_path / "new.db"
        holder = hold_write_lock(new_file)
        busy_timeout = 2.0
        skipped = [0.0]
        real_monotonic = time.monotonic

        def spend_most_of_the_budget(seconds: float) -> None:
            if skipped[0] == 0.0:
                holder.execute("ROLLBACK")
                holder.execute("BEGIN EXCLUSIVE")
            skipped[0] += 0.9 * busy_timeout

        monkeypatch.setattr(sqlite_ledger, "monotonic", lambda: real_monotonic() + skipped[0])
        monkeypatch.setattr(sqlite_ledger, "sleep", spend_most_of_the_budget)
        started = real_monotonic()
        try:
            with pytest.raises(LedgerBusyError):
                SqliteLedgerRepository(new_file, busy_timeout=busy_timeout)
            elapsed = real_monotonic() - started
        finally:
            holder.close()

        assert skipped[0] > 0.0
        assert elapsed < busy_timeout / 2

    def test_after_an_open_with_retries_writes_wait_the_whole_busy_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        new_file = tmp_path / "new.db"
        holder = hold_write_lock(new_file)
        busy_timeout = 0.5
        skipped = [0.0]
        real_monotonic = time.monotonic

        def spend_most_of_the_budget_then_release(seconds: float) -> None:
            skipped[0] += 0.9 * busy_timeout
            if holder.in_transaction:
                holder.execute("ROLLBACK")

        monkeypatch.setattr(sqlite_ledger, "monotonic", lambda: real_monotonic() + skipped[0])
        monkeypatch.setattr(sqlite_ledger, "sleep", spend_most_of_the_budget_then_release)
        repository = SqliteLedgerRepository(new_file, busy_timeout=busy_timeout)
        try:
            holder.execute("BEGIN IMMEDIATE")
            started = real_monotonic()
            with pytest.raises(LedgerBusyError):
                repository.append(payment())
            waited = real_monotonic() - started
        finally:
            holder.close()
            repository.close()

        assert skipped[0] > 0.0
        assert waited >= 0.8 * busy_timeout

    def test_many_connections_opening_one_new_file_at_once_all_succeed(
        self, tmp_path: Path
    ) -> None:
        for attempt in range(20):
            new_file = tmp_path / f"new-{attempt}.db"

            outcomes = open_at_once(new_file, openers=8)

            assert outcomes == ["opened"] * 8, f"attempt {attempt}: {outcomes}"
            check = sqlite3.connect(new_file)
            assert check.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
            check.close()

    def test_writes_blocked_past_the_busy_timeout_raise_ledger_busy(
        self, db_path: Path, repository: SqliteLedgerRepository
    ) -> None:
        saved = repository.append(payment())
        impatient = SqliteLedgerRepository(db_path, busy_timeout=0.1)
        holder = hold_write_lock(db_path)
        try:
            with pytest.raises(LedgerBusyError):
                impatient.append(payment(4000))
            with pytest.raises(LedgerBusyError), impatient.atomic():
                pass
            assert impatient.history(limit=10) == [saved]
        finally:
            holder.close()
            impatient.close()


READS: list[Callable[[SqliteLedgerRepository], object]] = [
    lambda repository: repository.get(1),
    lambda repository: repository.adjustments_for(1),
    lambda repository: repository.find_by_image_hash(HASH_A),
    lambda repository: repository.find_payments_by_reference("00012345"),
    lambda repository: repository.balance(),
    lambda repository: repository.history(limit=10),
    lambda repository: repository.history(limit=10, before_id=5),
]
READ_IDS = [
    "get",
    "adjustments_for",
    "find_by_image_hash",
    "find_payments_by_reference",
    "balance",
    "history",
    "history-before-id",
]


@pytest.fixture
def crafted(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SqliteLedgerRepository, CraftedErrorConnection]]:
    """A repository whose connection can be told to fail statements with a crafted error."""
    connections: list[CraftedErrorConnection] = []
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, factory=CraftedErrorConnection, **kwargs)
        connections.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        repository = SqliteLedgerRepository(db_path)
    yield repository, connections[0]
    repository.close()


class TestSqliteErrorMapping:
    """Busy and locked codes, primary or extended, become `LedgerBusyError`; others do not."""

    @pytest.mark.parametrize("read", READS, ids=READ_IDS)
    def test_busy_reads_raise_ledger_busy(
        self,
        crafted: tuple[SqliteLedgerRepository, CraftedErrorConnection],
        read: Callable[[SqliteLedgerRepository], object],
    ) -> None:
        repository, connection = crafted
        connection.fail_on, connection.error_code = "SELECT", sqlite3.SQLITE_BUSY_SNAPSHOT

        with pytest.raises(LedgerBusyError):
            read(repository)

    @pytest.mark.parametrize("code", BUSY_CODES)
    def test_every_busy_and_locked_code_raises_ledger_busy(
        self, crafted: tuple[SqliteLedgerRepository, CraftedErrorConnection], code: int
    ) -> None:
        repository, connection = crafted
        connection.fail_on, connection.error_code = "SELECT", code

        with pytest.raises(LedgerBusyError, match=f"code {code}"):
            repository.get(1)

    @pytest.mark.parametrize(
        "code",
        [sqlite3.SQLITE_IOERR, sqlite3.SQLITE_IOERR_READ, sqlite3.SQLITE_CANTOPEN, None],
        ids=["ioerr", "ioerr-read", "cantopen", "no-code"],
    )
    def test_other_errors_stay_sqlite_errors(
        self, crafted: tuple[SqliteLedgerRepository, CraftedErrorConnection], code: int | None
    ) -> None:
        repository, connection = crafted
        connection.fail_on, connection.error_code = "SELECT", code

        with pytest.raises(sqlite3.OperationalError) as excinfo:
            repository.get(1)

        assert not isinstance(excinfo.value, LedgerBusyError)

    @pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT])
    def test_busy_commit_raises_ledger_busy_and_keeps_nothing(
        self, crafted: tuple[SqliteLedgerRepository, CraftedErrorConnection], code: int
    ) -> None:
        repository, connection = crafted
        connection.fail_on, connection.error_code = "COMMIT", code

        with pytest.raises(LedgerBusyError), repository.atomic():
            repository.append(payment())

        connection.fail_on = None
        assert repository.history(limit=10) == []
        assert repository.append(payment()).id == 1


class TestUnitOfWork:
    def test_block_commits_when_it_completes(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        with repository.atomic():
            repository.append(payment(25000))
            repository.append(payment(4000))

        assert raw.execute("SELECT count(*) FROM ledger_entries").fetchone() == (2,)

    def test_block_writes_nothing_when_it_raises(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        with pytest.raises(RuntimeError, match="boom"), repository.atomic():
            repository.append(payment(25000))
            raise RuntimeError("boom")

        assert repository.history(limit=10) == []
        assert raw.execute("SELECT count(*) FROM ledger_entries").fetchone() == (0,)

    def test_block_holds_the_write_lock_from_its_first_read(
        self, db_path: Path, repository: SqliteLedgerRepository
    ) -> None:
        impatient = sqlite3.connect(db_path, timeout=0, autocommit=True)
        try:
            with repository.atomic():
                repository.balance()
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    impatient.execute("BEGIN IMMEDIATE")
            impatient.execute("BEGIN IMMEDIATE")
            impatient.execute("ROLLBACK")
        finally:
            impatient.close()

    def test_blocks_do_not_nest(self, repository: SqliteLedgerRepository) -> None:
        with (
            repository.atomic(),
            pytest.raises(RuntimeError, match="nested"),
            repository.atomic(),
        ):
            pass

    def test_appends_outside_a_block_commit_on_their_own(
        self, repository: SqliteLedgerRepository, raw: sqlite3.Connection
    ) -> None:
        repository.append(payment(25000))

        assert raw.execute("SELECT count(*) FROM ledger_entries").fetchone() == (1,)


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

    def test_history_is_newest_first_and_pages_by_id(
        self, repository: SqliteLedgerRepository
    ) -> None:
        ids = [repository.append(payment(amount)).id for amount in (100, 200, 300)]

        first_page = repository.history(limit=2)
        second_page = repository.history(limit=2, before_id=first_page[-1].id)

        assert [entry.id for entry in first_page] == [ids[2], ids[1]]
        assert [entry.id for entry in second_page] == [ids[0]]

    def test_history_pages_do_not_drift_when_entries_arrive_between_them(
        self, repository: SqliteLedgerRepository
    ) -> None:
        ids = [repository.append(payment(amount)).id for amount in (100, 200, 300, 400)]
        first_page = repository.history(limit=2)

        repository.append(payment(50))
        second_page = repository.history(limit=2, before_id=first_page[-1].id)

        assert [entry.id for entry in second_page] == [ids[1], ids[0]]

    @pytest.mark.parametrize("limit", [0, -1])
    def test_history_refuses_a_non_positive_limit(
        self, repository: SqliteLedgerRepository, limit: int
    ) -> None:
        repository.append(payment())

        with pytest.raises(ValueError, match="limit"):
            repository.history(limit=limit)

    @pytest.mark.parametrize(
        ("limit", "before_id", "name"),
        [
            (2**63, None, "limit"),
            (10, 0, "before_id"),
            (10, -1, "before_id"),
            (10, 2**63, "before_id"),
        ],
    )
    def test_history_refuses_paging_values_sqlite_cannot_store(
        self, repository: SqliteLedgerRepository, limit: int, before_id: int, name: str
    ) -> None:
        with pytest.raises(ValueError, match=name):
            repository.history(limit=limit, before_id=before_id)

    @pytest.mark.parametrize(
        ("limit", "before_id", "name"),
        [
            (True, None, "limit"),
            (2.0, None, "limit"),
            (10, 2.5, "before_id"),
            (10, True, "before_id"),
        ],
    )
    def test_history_refuses_paging_values_that_are_not_ints(
        self, repository: SqliteLedgerRepository, limit: Any, before_id: Any, name: str
    ) -> None:
        with pytest.raises(TypeError, match=name):
            repository.history(limit=limit, before_id=before_id)

    def test_history_accepts_the_largest_id_as_before_id(
        self, repository: SqliteLedgerRepository
    ) -> None:
        saved = repository.append(payment())

        assert repository.history(limit=2**63 - 1, before_id=2**63 - 1) == [saved]

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
