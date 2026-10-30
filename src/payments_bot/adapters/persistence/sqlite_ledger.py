"""SQLite storage for the ledger.

Defense in depth: besides the domain rules, the schema itself refuses edits. UPDATE and
DELETE abort in triggers, rows cannot be overwritten through INSERT OR REPLACE,
adjustments must target an existing payment in the same currency, and no adjustment can
take its payment's effective amount below zero.

Rows can never be deleted, so a row the domain refuses to load would break `history()` for
good. The CHECK constraints therefore mirror the domain: ISO 4217 currency, lowercase
SHA-256 hex, non-blank reasons, bounded amounts, positive ids, and timestamps in exactly the
UTC text format this adapter writes.

Transactions are explicit: the connection runs in autocommit mode, so a lone `append`
commits on its own and `atomic()` wraps a unit of work in BEGIN IMMEDIATE ... COMMIT.

The schema version lives in `PRAGMA user_version`; a database from a newer version, or one
created before versioning, is refused instead of being read with the wrong assumptions.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from payments_bot.domain.ledger import Entry, EntryKind, EntrySource
from payments_bot.domain.money import MAX_AMOUNT_MINOR
from payments_bot.ports.ledger_repository import ImageAlreadyStoredError

IN_MEMORY = ":memory:"
SCHEMA_VERSION = 1
"""Stored in `PRAGMA user_version`; bump it, and migrate from the previous one, on change."""
BUSY_TIMEOUT_SECONDS = 5.0
"""How long a writer waits for a concurrent unit of work to finish before giving up."""


class UnsupportedSchemaError(RuntimeError):
    """The database file was written by a schema version this code cannot read safely."""


_UTC_TEXT_GLOB = (
    "[1-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]"
    "T[0-2][0-9]:[0-5][0-9]:[0-5][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00"
)
"""Shape of `_to_text` output, e.g. 2026-10-29T15:00:00.000000+00:00."""

_PYTHON_WHITESPACE = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004"
    "\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
"""Every character `str.strip()` removes, so the database and the domain agree on blank."""


def _utc_text(column: str) -> str:
    """SQL that holds when `column` is a real calendar instant in exactly the stored format."""
    seconds = f"substr({column}, 1, 19)"
    # strftime normalizes impossible dates (Feb 31 -> Mar 3) or returns NULL, so a value
    # that does not survive the round trip is not a real instant.
    return (
        f"{column} GLOB '{_UTC_TEXT_GLOB}' "
        f"AND strftime('%Y-%m-%dT%H:%M:%S', {seconds}) IS {seconds}"
    )


_BLANK_CHARACTERS = f"char({', '.join(str(ord(char)) for char in _PYTHON_WHITESPACE)})"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ledger_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT CONSTRAINT id_positive CHECK (id > 0),
    kind           TEXT    NOT NULL CHECK (kind IN ('payment', 'adjustment')),
    amount_minor   INTEGER NOT NULL CONSTRAINT amount_in_range
                   CHECK (amount_minor BETWEEN -{MAX_AMOUNT_MINOR} AND {MAX_AMOUNT_MINOR}),
    currency       TEXT    NOT NULL CONSTRAINT currency_iso_4217
                   CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    source         TEXT    NOT NULL CHECK (source IN ('ocr', 'manual', 'import')),
    created_by     INTEGER NOT NULL,
    created_at     TEXT    NOT NULL CONSTRAINT created_at_utc_text
                   CHECK ({_utc_text("created_at")}),
    corrects_id    INTEGER REFERENCES ledger_entries (id),
    reason         TEXT    CONSTRAINT reason_not_blank
                   CHECK (reason IS NULL OR trim(reason, {_BLANK_CHARACTERS}) <> ''),
    reference      TEXT,
    paid_at        TEXT    CONSTRAINT paid_at_utc_text
                   CHECK (paid_at IS NULL OR ({_utc_text("paid_at")})),
    image_sha256   TEXT    UNIQUE CONSTRAINT image_sha256_hex CHECK (
                       image_sha256 IS NULL
                       OR (length(image_sha256) = 64 AND image_sha256 NOT GLOB '*[^0-9a-f]*')
                   ),
    image_key      TEXT,
    extracted_json TEXT,
    CONSTRAINT entry_kind_shape CHECK (
        (kind = 'payment' AND corrects_id IS NULL AND amount_minor > 0)
        OR (
            kind = 'adjustment'
            AND corrects_id IS NOT NULL
            AND reason IS NOT NULL
            AND amount_minor <> 0
            AND source = 'manual'
            AND image_sha256 IS NULL
            AND image_key IS NULL
        )
    )
) STRICT;

CREATE INDEX IF NOT EXISTS ledger_entries_corrects_id ON ledger_entries (corrects_id);
CREATE INDEX IF NOT EXISTS ledger_entries_reference ON ledger_entries (reference);

CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

-- INSERT OR REPLACE deletes the conflicting row without firing delete triggers
-- (unless recursive_triggers is on), so refuse any insert that would collide.
CREATE TRIGGER IF NOT EXISTS ledger_entries_no_overwrite
BEFORE INSERT ON ledger_entries
WHEN EXISTS (SELECT 1 FROM ledger_entries WHERE id = NEW.id)
    OR EXISTS (SELECT 1 FROM ledger_entries WHERE image_sha256 = NEW.image_sha256)
BEGIN
    SELECT RAISE(ABORT, 'ledger entry already exists');
END;

CREATE TRIGGER IF NOT EXISTS ledger_entries_adjustment_target
BEFORE INSERT ON ledger_entries
WHEN NEW.kind = 'adjustment'
    AND NOT EXISTS (
        SELECT 1 FROM ledger_entries
        WHERE id = NEW.corrects_id AND kind = 'payment' AND currency = NEW.currency
    )
BEGIN
    SELECT RAISE(ABORT, 'adjustment must target a payment in the same currency');
END;

-- Whatever a caller computed from a stale read, an adjustment can never push its payment's
-- effective amount (payment plus every adjustment) below zero.
CREATE TRIGGER IF NOT EXISTS ledger_entries_effective_not_negative
BEFORE INSERT ON ledger_entries
WHEN NEW.kind = 'adjustment'
    AND NEW.amount_minor + (
        SELECT payment.amount_minor + coalesce(sum(adjustment.amount_minor), 0)
        FROM ledger_entries AS payment
        LEFT JOIN ledger_entries AS adjustment ON adjustment.corrects_id = payment.id
        WHERE payment.id = NEW.corrects_id AND payment.kind = 'payment'
    ) < 0
BEGIN
    SELECT RAISE(ABORT, 'adjustment would make the payment amount negative');
END;
"""

_COLUMNS = (
    "kind",
    "amount_minor",
    "currency",
    "source",
    "created_by",
    "created_at",
    "corrects_id",
    "reason",
    "reference",
    "paid_at",
    "image_sha256",
    "image_key",
    "extracted_json",
)
_INSERT = (
    f"INSERT INTO ledger_entries ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)


class SqliteLedgerRepository:
    """`LedgerRepository` backed by SQLite. It exposes no update or delete operation."""

    def __init__(self, database: str | Path) -> None:
        # autocommit=True: the driver never opens or commits transactions behind our back
        # (unlike isolation_level=None, executescript() then also respects an open one).
        self._connection = sqlite3.connect(database, autocommit=True, timeout=BUSY_TIMEOUT_SECONDS)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            if str(database) != IN_MEMORY:
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._ensure_schema()
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        if self._connection.in_transaction:
            raise RuntimeError("atomic blocks cannot be nested")
        # IMMEDIATE takes the write lock before the first read, so two units of work never
        # decide from the same state: the second one waits (busy timeout) for the first.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def append(self, entry: Entry) -> Entry:
        if entry.id is not None:
            raise ValueError(f"entry {entry.id} is already persisted")
        try:
            cursor = self._connection.execute(_INSERT, _to_row(entry))
        except sqlite3.IntegrityError as error:
            owner = self._image_owner(entry)
            if owner is not None:
                raise ImageAlreadyStoredError(owner) from error
            raise
        return replace(entry, id=cursor.lastrowid)

    def get(self, entry_id: int) -> Entry | None:
        return self._fetch_one("SELECT * FROM ledger_entries WHERE id = ?", (entry_id,))

    def adjustments_for(self, payment_id: int) -> list[Entry]:
        return self._fetch_all(
            "SELECT * FROM ledger_entries WHERE corrects_id = ? ORDER BY id", (payment_id,)
        )

    def find_by_image_hash(self, image_sha256: str) -> Entry | None:
        return self._fetch_one(
            "SELECT * FROM ledger_entries WHERE image_sha256 = ?", (image_sha256,)
        )

    def find_payments_by_reference(self, reference: str) -> list[Entry]:
        return self._fetch_all(
            "SELECT * FROM ledger_entries WHERE kind = 'payment' AND reference = ? ORDER BY id",
            (reference,),
        )

    def balance(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT currency, SUM(amount_minor) AS total FROM ledger_entries "
            "GROUP BY currency ORDER BY currency"
        )
        return {row["currency"]: row["total"] for row in rows}

    def history(self, limit: int, before_id: int | None = None) -> list[Entry]:
        if limit < 1:
            raise ValueError(f"history limit must be at least 1: {limit}")
        if before_id is None:
            return self._fetch_all(
                "SELECT * FROM ledger_entries ORDER BY id DESC LIMIT ?", (limit,)
            )
        return self._fetch_all(
            "SELECT * FROM ledger_entries WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before_id, limit),
        )

    def _ensure_schema(self) -> None:
        """Create the schema in a new database; refuse one this code cannot read safely."""
        if self._schema_version() == SCHEMA_VERSION:
            return
        with self.atomic():
            version = self._schema_version()  # again, now that no one else can write
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"ledger database has schema version {version}; "
                    f"this code supports up to {SCHEMA_VERSION}"
                )
            # Version 0 is a brand-new file. A future version N migrates from N - 1 here.
            if self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'ledger_entries'"
            ).fetchone():
                raise UnsupportedSchemaError(
                    "ledger database has no schema version: it predates versioning "
                    "and cannot be migrated"
                )
            self._connection.executescript(_SCHEMA)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _schema_version(self) -> int:
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def _image_owner(self, entry: Entry) -> int | None:
        """Id of the stored entry that already carries `entry`'s image hash, if any."""
        if entry.image_sha256 is None:
            return None
        row = self._connection.execute(
            "SELECT id FROM ledger_entries WHERE image_sha256 = ?", (entry.image_sha256,)
        ).fetchone()
        return row["id"] if row is not None else None

    def _fetch_one(self, query: str, params: tuple[object, ...]) -> Entry | None:
        row = self._connection.execute(query, params).fetchone()
        return _to_entry(row) if row is not None else None

    def _fetch_all(self, query: str, params: tuple[object, ...]) -> list[Entry]:
        return [_to_entry(row) for row in self._connection.execute(query, params)]


def _to_row(entry: Entry) -> tuple[object, ...]:
    return (
        entry.kind.value,
        entry.amount_minor,
        entry.currency,
        entry.source.value,
        entry.created_by,
        _to_text(entry.created_at),
        entry.corrects_id,
        entry.reason,
        entry.reference,
        _to_text(entry.paid_at) if entry.paid_at is not None else None,
        entry.image_sha256,
        entry.image_key,
        entry.extracted_json,
    )


def _to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        kind=EntryKind(row["kind"]),
        amount_minor=row["amount_minor"],
        currency=row["currency"],
        source=EntrySource(row["source"]),
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        corrects_id=row["corrects_id"],
        reason=row["reason"],
        reference=row["reference"],
        paid_at=datetime.fromisoformat(row["paid_at"]) if row["paid_at"] is not None else None,
        image_sha256=row["image_sha256"],
        image_key=row["image_key"],
        extracted_json=row["extracted_json"],
    )


def _to_text(moment: datetime) -> str:
    """Fixed-width ISO-8601 in UTC, so stored timestamps sort and compare as text."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")
