"""SQLite storage for the ledger.

Defense in depth: besides the domain rules, the schema itself refuses edits. UPDATE and
DELETE abort in triggers, rows cannot be overwritten through INSERT OR REPLACE,
adjustments must target an existing payment in the same currency, and no adjustment can
take its payment's effective amount below zero or above `MAX_AMOUNT_MINOR`.

Rows can never be deleted, so a row the domain refuses to load would break `history()` for
good. The CHECK constraints therefore mirror the domain: ISO 4217 currency, lowercase
SHA-256 hex, non-blank reasons, bounded amounts, positive ids and `created_by`, no NUL
character in any text, and timestamps in exactly the UTC text format this adapter writes
(years 1000 to 9999, which the domain enforces before any write). Text that is not valid
UTF-8 (raw bytes CAST to TEXT, which SQLite stores unchecked) is read with replacement
characters instead of failing to decode.

Transactions are explicit: the connection runs in autocommit mode, so a lone `append`
commits on its own and `atomic()` wraps a unit of work in BEGIN IMMEDIATE ... COMMIT. A
writer waits up to the busy timeout for another one; past it, the port's `LedgerBusyError`
is raised instead of SQLite's "database is locked".

The schema version lives in `PRAGMA user_version`; a database from a newer version, one
created before versioning, or another application's file is refused instead of being read
with the wrong assumptions, and is left exactly as it was: nothing is written to a file
before it is known to be this ledger, or empty.
"""

import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

from payments_bot.domain.ledger import Entry, EntryKind, EntrySource
from payments_bot.domain.money import MAX_AMOUNT_MINOR
from payments_bot.ports.ledger_repository import ImageAlreadyStoredError, LedgerBusyError

IN_MEMORY = ":memory:"
SCHEMA_VERSION = 3
"""Stored in `PRAGMA user_version`; bump it, and migrate from the previous one, on change.

Version 2 made the text checks NUL-safe and bounded the effective amount above; version 3
requires a positive `created_by`. No version 1 or 2 ledger was ever put to use, so there is
no migration from them: they are refused like any other.
"""
BUSY_TIMEOUT_SECONDS = 5.0
"""How long a writer waits for a concurrent unit of work to finish before giving up."""
_OPEN_RETRY_PAUSE_SECONDS = 0.02
"""Pause between attempts to prepare a database file another connection is preparing too."""
_BUSY_ERROR_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
_MAX_SQLITE_INTEGER = 2**63 - 1
_MAX_BUSY_TIMEOUT_MS = 2**31 - 1
"""SQLite keeps its busy timeout in a C int of milliseconds (about 24.8 days)."""
_SCHEMA_STATE = (
    "SELECT (SELECT user_version FROM pragma_user_version) AS version,"
    " (SELECT count(*) FROM sqlite_master) AS objects,"
    " EXISTS (SELECT 1 FROM sqlite_master WHERE name = 'ledger_entries') AS has_ledger"
)
"""Version and contents read in one statement, so concurrent openers see one snapshot."""


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


def _no_nul(column: str) -> str:
    """SQL that holds when `column` has no NUL character.

    GLOB and length() stop at the first NUL, so without this a valid prefix followed by a NUL
    and anything at all would pass the other checks. instr() and comparisons do not stop.
    """
    return f"instr({column}, char(0)) = 0"


def _utc_text(column: str) -> str:
    """SQL that holds when `column` is a real calendar instant in exactly the stored format."""
    seconds = f"substr({column}, 1, 19)"
    # strftime normalizes impossible dates (Feb 31 -> Mar 3) or returns NULL, so a value
    # that does not survive the round trip is not a real instant.
    return (
        f"{column} GLOB '{_UTC_TEXT_GLOB}' AND {_no_nul(column)} "
        f"AND strftime('%Y-%m-%dT%H:%M:%S', {seconds}) IS {seconds}"
    )


def _optional_text(column: str) -> str:
    """Named CHECK refusing a NUL character in a free-text column that may be NULL."""
    return f"CONSTRAINT {column}_no_nul CHECK ({column} IS NULL OR {_no_nul(column)})"


_BLANK_CHARACTERS = f"char({', '.join(str(ord(char)) for char in _PYTHON_WHITESPACE)})"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ledger_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT CONSTRAINT id_positive CHECK (id > 0),
    kind           TEXT    NOT NULL CHECK (kind IN ('payment', 'adjustment')),
    amount_minor   INTEGER NOT NULL CONSTRAINT amount_in_range
                   CHECK (amount_minor BETWEEN -{MAX_AMOUNT_MINOR} AND {MAX_AMOUNT_MINOR}),
    currency       TEXT    NOT NULL CONSTRAINT currency_iso_4217
                   CHECK (currency GLOB '[A-Z][A-Z][A-Z]' AND {_no_nul("currency")}),
    source         TEXT    NOT NULL CHECK (source IN ('ocr', 'manual', 'import')),
    created_by     INTEGER NOT NULL CONSTRAINT created_by_positive CHECK (created_by > 0),
    created_at     TEXT    NOT NULL CONSTRAINT created_at_utc_text
                   CHECK ({_utc_text("created_at")}),
    corrects_id    INTEGER REFERENCES ledger_entries (id),
    reason         TEXT    CONSTRAINT reason_not_blank
                   CHECK (reason IS NULL OR trim(reason, {_BLANK_CHARACTERS}) <> '')
                   {_optional_text("reason")},
    reference      TEXT    {_optional_text("reference")},
    paid_at        TEXT    CONSTRAINT paid_at_utc_text
                   CHECK (paid_at IS NULL OR ({_utc_text("paid_at")})),
    image_sha256   TEXT    UNIQUE CONSTRAINT image_sha256_hex CHECK (
                       image_sha256 IS NULL
                       OR (
                           length(image_sha256) = 64
                           AND image_sha256 NOT GLOB '*[^0-9a-f]*'
                           AND {_no_nul("image_sha256")}
                       )
                   ),
    image_key      TEXT    {_optional_text("image_key")},
    extracted_json TEXT    {_optional_text("extracted_json")},
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

-- Whatever a caller computed from a stale read, an adjustment can never take its payment's
-- effective amount (payment plus every adjustment) below zero, nor above the maximum: a
-- payment beyond it could no longer be voided by one adjustment within amount_in_range.
-- LEFT JOIN: a payment's first adjustment has no earlier adjustment to join with.
CREATE TRIGGER IF NOT EXISTS ledger_entries_effective_in_range
BEFORE INSERT ON ledger_entries
WHEN NEW.kind = 'adjustment'
BEGIN
    SELECT CASE
        WHEN effective < 0
            THEN RAISE(ABORT, 'adjustment would make the payment amount negative')
        WHEN effective > {MAX_AMOUNT_MINOR}
            THEN RAISE(ABORT, 'adjustment would make the payment amount exceed the maximum')
    END
    FROM (
        SELECT NEW.amount_minor + (
            SELECT payment.amount_minor + coalesce(sum(adjustment.amount_minor), 0)
            FROM ledger_entries AS payment
            LEFT JOIN ledger_entries AS adjustment ON adjustment.corrects_id = payment.id
            WHERE payment.id = NEW.corrects_id AND payment.kind = 'payment'
        ) AS effective
    );
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

    def __init__(self, database: str | Path, *, busy_timeout: float = BUSY_TIMEOUT_SECONDS) -> None:
        if not math.isfinite(busy_timeout) or busy_timeout < 0:
            raise ValueError(
                f"busy_timeout must be a finite number of seconds >= 0: {busy_timeout}"
            )
        self._busy_timeout = busy_timeout
        # autocommit=True: the driver never opens or commits transactions behind our back
        # (unlike isolation_level=None, executescript() then also respects an open one).
        # The busy timeout is set by _prepare, per attempt, and then for good.
        self._connection = sqlite3.connect(database, autocommit=True, timeout=0)
        self._connection.row_factory = sqlite3.Row
        self._connection.text_factory = _decode_text
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._prepare(wal=str(database) != IN_MEMORY)
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
        with self._busy_as_ledger_error():
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            with self._busy_as_ledger_error():
                self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def append(self, entry: Entry) -> Entry:
        if entry.id is not None:
            raise ValueError(f"entry {entry.id} is already persisted")
        try:
            with self._busy_as_ledger_error():
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
        rows = self._rows(
            "SELECT currency, SUM(amount_minor) AS total FROM ledger_entries "
            "GROUP BY currency ORDER BY currency"
        )
        return {row["currency"]: row["total"] for row in rows}

    def history(self, limit: int, before_id: int | None = None) -> list[Entry]:
        _require_positive_integer("limit", limit)
        if before_id is None:
            return self._fetch_all(
                "SELECT * FROM ledger_entries ORDER BY id DESC LIMIT ?", (limit,)
            )
        _require_positive_integer("before_id", before_id)
        return self._fetch_all(
            "SELECT * FROM ledger_entries WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before_id, limit),
        )

    def _prepare(self, *, wal: bool) -> None:
        """Check the schema, switch to WAL and create the schema, waiting out other openers.

        Switching the journal mode does not wait on the busy timeout: while another
        connection holds a lock, SQLite answers "database is locked" at once (it would risk a
        deadlock otherwise), so processes opening a new file together used to fail. Retry
        the whole preparation until the busy timeout has passed, then give up. An attempt
        can also wait inside SQLite's busy handler, so each one may only wait for the time
        left: the whole open takes about the busy timeout, never twice as long.
        """
        deadline = monotonic() + self._busy_timeout
        while True:
            self._set_busy_timeout(min(self._busy_timeout, max(0.0, deadline - monotonic())))
            try:
                with self._busy_as_ledger_error():
                    self._open_schema(wal=wal)
                break
            except LedgerBusyError:
                if monotonic() >= deadline:
                    raise
                sleep(_OPEN_RETRY_PAUSE_SECONDS)
        self._set_busy_timeout(self._busy_timeout)

    def _set_busy_timeout(self, seconds: float) -> None:
        milliseconds = min(int(seconds * 1000), _MAX_BUSY_TIMEOUT_MS)
        self._connection.execute(f"PRAGMA busy_timeout = {milliseconds}")

    @contextmanager
    def _busy_as_ledger_error(self) -> Iterator[None]:
        """Raise the port's `LedgerBusyError` for SQLite's busy and locked errors."""
        try:
            yield
        except sqlite3.OperationalError as error:
            if not _is_busy(error):
                raise
            raise LedgerBusyError(
                f"ledger database is locked by another connection "
                f"(busy timeout {self._busy_timeout} s): {error}"
            ) from error

    def _open_schema(self, *, wal: bool) -> None:
        """Use this ledger, or create it in an empty file; refuse anything else untouched.

        The file is checked before anything is written: switching to WAL rewrites its header,
        so a refused file would otherwise be left changed for good.
        """
        is_new = self._is_empty_or_refuse()
        if wal:
            self._connection.execute("PRAGMA journal_mode = WAL")
        if not is_new:
            return
        with self.atomic():
            # Again, now that no one else can write: another opener may have created it.
            if not self._is_empty_or_refuse():
                return
            self._connection.executescript(_SCHEMA)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _is_empty_or_refuse(self) -> bool:
        """False for this schema version, True for an empty file; refuse anything else."""
        version, objects, has_ledger = self._rows(_SCHEMA_STATE)[0]
        if version == SCHEMA_VERSION:
            return False
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"ledger database has schema version {version}; "
                f"this code supports up to {SCHEMA_VERSION}"
            )
        # A future version N migrates from N - 1 here; no older version migrates today.
        if version != 0:
            raise UnsupportedSchemaError(
                f"ledger database has schema version {version}; "
                f"this code needs {SCHEMA_VERSION} and cannot migrate from it"
            )
        if has_ledger:
            raise UnsupportedSchemaError(
                "ledger database has no schema version: it predates versioning "
                "and cannot be migrated"
            )
        if objects:
            raise UnsupportedSchemaError(
                "database is not a ledger: it is not empty and has no ledger schema version"
            )
        return True

    def _image_owner(self, entry: Entry) -> int | None:
        """Id of the stored entry that already carries `entry`'s image hash, if any."""
        if entry.image_sha256 is None:
            return None
        rows = self._rows(
            "SELECT id FROM ledger_entries WHERE image_sha256 = ?", (entry.image_sha256,)
        )
        return rows[0]["id"] if rows else None

    def _fetch_one(self, query: str, params: tuple[object, ...]) -> Entry | None:
        rows = self._rows(query, params)
        return _to_entry(rows[0]) if rows else None

    def _fetch_all(self, query: str, params: tuple[object, ...]) -> list[Entry]:
        return [_to_entry(row) for row in self._rows(query, params)]

    def _rows(self, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._busy_as_ledger_error():
            return self._connection.execute(query, params).fetchall()


def _is_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in _BUSY_ERROR_CODES


def _require_positive_integer(name: str, value: object) -> None:
    """Refuse what SQLite cannot bind as a positive INTEGER, rather than overflow or coerce."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if not 1 <= value <= _MAX_SQLITE_INTEGER:
        raise ValueError(f"{name} must be between 1 and {_MAX_SQLITE_INTEGER}: {value}")


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


def _decode_text(data: bytes) -> str:
    """Decode stored TEXT; bytes that are not UTF-8 become U+FFFD instead of failing the read."""
    return data.decode("utf-8", errors="replace")


def _to_text(moment: datetime) -> str:
    """Fixed-width ISO-8601 in UTC, so stored timestamps sort and compare as text."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")
