"""SQLite storage for the ledger.

Defense in depth: besides the domain rules, the schema itself refuses edits. UPDATE and
DELETE abort in triggers, rows cannot be overwritten through INSERT OR REPLACE, and
adjustments must target an existing payment in the same currency.
"""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from payments_bot.domain.ledger import Entry, EntryKind, EntrySource

IN_MEMORY = ":memory:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('payment', 'adjustment')),
    amount_minor   INTEGER NOT NULL,
    currency       TEXT    NOT NULL CHECK (length(currency) = 3),
    source         TEXT    NOT NULL CHECK (source IN ('ocr', 'manual', 'import')),
    created_by     INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    corrects_id    INTEGER REFERENCES ledger_entries (id),
    reason         TEXT,
    reference      TEXT,
    paid_at        TEXT,
    image_sha256   TEXT    UNIQUE,
    image_key      TEXT,
    extracted_json TEXT,
    CHECK (
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
        self._connection = sqlite3.connect(database)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if str(database) != IN_MEMORY:
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def append(self, entry: Entry) -> Entry:
        if entry.id is not None:
            raise ValueError(f"entry {entry.id} is already persisted")
        with self._connection:
            cursor = self._connection.execute(_INSERT, _to_row(entry))
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

    def history(self, limit: int, offset: int = 0) -> list[Entry]:
        return self._fetch_all(
            "SELECT * FROM ledger_entries ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )

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
    """ISO-8601 in UTC, so stored timestamps sort and compare as text."""
    return moment.astimezone(UTC).isoformat()
