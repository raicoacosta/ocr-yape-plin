# Feature: payments-ledger-bot

Locator: `odd/tasks/payments-ledger-bot.md` · Engram mirror: `odd/payments-ledger-bot/tasks`

## Objective

Telegram bot that reads payment receipts (screenshots/photos), extracts the amount, keeps a running total, and supports manual corrections as append-only adjustments so the history is never edited.

## Problem / Why

Payment receipts (Yape, Banco de la Nación — PEN, `S/`) arrive as WhatsApp images. Summing them by hand is error-prone and there is no audit trail of corrections.

Observed receipt traits (from `pagos/`, 22 JPEG, 47–187 KB):
- Amount shown with `S/` prefix, sometimes without decimals (`S/ 250`), sometimes with (`S/ 40.00`).
- Same image also contains an operation number, phone digits, dates and (Yape) a 3-digit security code — none of these is the amount.

## Scope (authorized now)

- Phase 1: domain + append-only ledger (SQLite) + application use cases.
- Phase 2: amount extractor (Claude vision via Amazon Bedrock) + evaluation harness against the real images in `pagos/`.

## Out of scope (later phases, need separate go-ahead)

Telegram bot (phase 3), bulk import of `pagos/` (4), EC2 deployment (5), operations/alerts (6).

## Constraints

- Amounts stored as integer minor units (céntimos); never float. Currency ISO 4217 (default `PEN`).
- Ledger is append-only, enforced in the database (triggers) and in the domain.
- Corrections are adjustment entries referencing the original payment, with a mandatory reason.
- Duplicate images rejected by SHA-256; same operation number flagged as a possible duplicate.
- Private data (`pagos/`, `data/`, `*.db`) never committed.
- Remote calls (Bedrock) require explicit user authorization of AWS profile/region before running.
- Code, comments and identifiers in English.

## Tasks

- [x] T1 — Project scaffold: uv project, pytest, ruff, git repo, `.gitignore`. Route: delegated writer (bundled with T2).
- [x] T2 — Phase 1: domain model, SQLite append-only ledger, use cases, tests. Route: delegated writer (2+ non-trivial files).
- [x] T2.1 — Ledger hardening from independent verification (atomic corrections/registrations, stricter DB constraints, amount cap, keyset paging, schema version). Route: delegated writer.
- [x] T2.2 — Ledger gaps left by the independent verification of T2.1: NUL-safe text checks, effective amount bounded above, mutant-killing tests, schema version 2, concurrent first open, busy/locked and paging errors mapped at the port. Route: delegated writer (trigger: 2+ non-trivial files across adapter, port, domain, application and tests).
- [ ] T3 — Phase 2a: extractor port, amount parsing, Claude-on-Bedrock adapter (unit-tested with fakes), labels template + eval script. Route: delegated writer (2+ non-trivial files). Status: code, tests, README and all checks done; only `.env.example` pending (writer's write was denied by permission settings).
- [x] T3.1 — Fixes from the independent verification of T3 (`19d0c47`, `cfac151`): `needs_confirmation` status, amount/reference/security-code guards, Lima wall-clock time, incremental eval results, endpoint/auth banner, labels hardening, synthetic fixtures. Route: delegated writer (trigger: 2+ non-trivial files across port, adapter, prompt, tools and tests).
- [x] T3.2 — Gaps from the independent verification of T3.1 (`e8c843f`, `981f85a`): `extract()` never raises on malformed answers, repeated JSON keys refused, operation-number/security-code guards see every digit (other scripts converted, cap after leading zeros), plausible receipt dates with a fixed UTC−5 offset, URL credentials never printed, eval results files created before any request, CSV formula injection neutralized, labels case/Unicode duplicates and zero amounts refused, mutant-killing tests. Route: delegated writer (trigger: 2+ non-trivial files across adapter, port, tools and tests).
- [ ] T4 — Phase 2b: user confirms expected amounts for the 22 images and the eval runs against Bedrock. Blocked on: user authorization of AWS profile/region and Bedrock model access.

## Acceptance criteria

- T2: UPDATE/DELETE on ledger rows fail at the DB level; balance = sum of entries per currency; correct/void produce adjustments with the right delta; duplicate images rejected; all tests green.
- T2.1: concurrent corrections/voids/registrations serialize (no double-apply, duplicate image → `DuplicateImageError`); every row the DB accepts is readable by the domain; amounts capped at `MAX_AMOUNT_MINOR`; history pages by `before_id`; unknown schema versions refused.
- T2.2: no text value with a NUL character is accepted by the database (every text column) or the domain (free-text fields, plus characters UTF-8 cannot encode); a stored non-UTF-8 text never breaks a read; no adjustment takes a payment's effective amount below zero or above `MAX_AMOUNT_MINOR`, its first adjustment included; schema version 2, version 1 refused without migration; several connections opening a new file at once all succeed, and a lock held past the busy timeout raises `LedgerBusyError` (never raw `sqlite3.OperationalError`) from the constructor and from use cases; `ListHistory` accepts only `int` (not `bool`) `limit` in 1..`MAX_HISTORY_PAGE` and `before_id` in 1..2**63-1, never raising `OverflowError`; AUTOINCREMENT exhaustion and `PRAGMA ignore_check_constraints` documented as outside the threat model.
- T3: extractor returns the amount in minor units or an explicit "unreadable" result; prompt and tests guard against operation number / security code / phone being taken as the amount; eval script reports per-image and overall accuracy.
- T3.1: only a high-confidence amount that passes every guard is `ok`; medium/low confidence or an amount repeating the security code is `needs_confirmation` (candidate kept, never auto-recordable); zero, above `MAX_AMOUNT_MINOR`, or repeating the operation number's digits (formatted, shifted decimal point) is `unreadable`; printed numbers never reach `int()`; `parse_amount` refuses over-long input instead of rounding; currency is `PEN`/`USD`/null; receipt time is Lima wall-clock time and a date-only receipt invents no time; eval results default to `data/eval/`, are written row by row with collision-free names, and the banner shows endpoint and auth mode without secrets; labels refuse non-UTF-8 files, duplicate files and non-bare names; abbreviated flags and blank `--model` are refused; 529 is retryable; no real receipt data in code, prompt, tests or docs.
- T3.2: no model answer makes `extract()` raise (list/dict confidence, nesting deeper than the parser's limit, integer literals over 4300 digits all give `failed`); a key repeated in the answer gives `failed`; an amount spelling the operation number or security code is caught however many leading zeros precede it and whatever digit script it uses, and a digit of another script is converted, never dropped; the reference keeps up to 32 digits; receipt dates before 2016-01-01 or after tomorrow in Lima give `paid_on`/`paid_at` None without changing the status, and receipt times carry a fixed −05:00 offset; the endpoint banner never prints a URL's user, password, query or fragment, whatever the URL's shape, and an empty base URL is reported as the SDK resolves it; every model's results file is created before the first request and a file that cannot be created ends the run with a clean `error:` line, exit 2 and no new file; result text cells starting with `=`, `+`, `-`, `@`, tab or CR get a leading apostrophe while number columns stay numbers; labels refuse names that differ only in case or Unicode form and an `expected_amount` of zero; 500/502/503/504/529 are retryable through the real Mantle status mapping.
- T4: accuracy reported on the 22 images; model choice (Opus 5.5 vs Haiku 5.5) decided with data.

## Checks

`uv run pytest -q`, `uv run ruff check .`

## Delivery

Strategy: `ask-on-risk`. Forecast ≈ 950 authored lines across T1–T3 (> 400). No git remote exists yet, so PR slicing is deferred until the user decides to push; commits stay as work units on `feat/payments-ledger-bot`.

## Progress / Evidence

### T1 — scaffold (done)

- uv project, src layout (`src/payments_bot`), hatchling backend, `requires-python >=3.12`, `.python-version` 3.13; dev deps `pytest`, `ruff` via `uv add --dev`; ruff `E,F,I,UP,B,SIM`, line-length 100; pytest `testpaths = ["tests"]`.
- `uv run pytest -q`: 1 passed (import smoke test). `uv run ruff check .`: All checks passed.
- RED/GREEN: no meaningful RED for a scaffold; the smoke test was written alongside the package.
- Commit: `451bff2`.

### T2 — append-only ledger (done)

- Layout: `domain/{money,ledger,errors}.py`, `ports/ledger_repository.py` (Protocol, no update/delete), `application/{payments,corrections,queries,errors,clock}.py`, `adapters/persistence/sqlite_ledger.py`.
- SQLite defense in depth: `STRICT` table, CHECK constraints per kind, `BEFORE UPDATE`/`BEFORE DELETE` triggers abort with `ledger is append-only`, adjustment-target trigger (must be a payment in the same currency), and a no-overwrite trigger that blocks `INSERT OR REPLACE` (which otherwise deletes rows without firing delete triggers; confirmed by dropping the trigger in a scratch run: the row was silently rewritten).
- Test-first, three cycles, RED observed each time as collection errors (`ModuleNotFoundError`) before the module existed:
  - domain: RED 2 collection errors → GREEN 69 passed.
  - SQLite adapter: RED 1 collection error → GREEN 33 passed.
  - application: RED conftest `ModuleNotFoundError` → GREEN, full suite 123 passed.
- Final checks: `uv run pytest -q` → 123 passed; `uv run ruff check .` → All checks passed; `uv run ruff format --check .` → 30 files already formatted.
- Commit: `9875869`. Independent verification attacked it: 1 major + 4 minor + 2 nits, all addressed in T2.1.

### T2.1 — ledger hardening from independent verification (done)

Findings (verifier reproduction scripts: concurrency, invalid rows, SQL paths, arithmetic, schema mutation) and fixes:

- MAJOR — corrections were read-compute-append without a transaction; two writers from the same state double-applied (double void left a 25000 payment at -25000, balance negative; double "correct to 30000" applied +5000 twice). Fix: unit of work on the port, `LedgerRepository.atomic()`; SQLite runs `BEGIN IMMEDIATE` … `COMMIT`/`ROLLBACK` on an `autocommit=True` connection with a 5 s busy timeout, so the second writer waits instead of reading stale state. `CorrectEntry`, `VoidEntry`, `RegisterPayment` do reads, domain decision, append and resulting balance inside one block (a failure after the append rolls it back). Blocks do not nest (explicit `RuntimeError`). Defense in depth: trigger `ledger_entries_effective_not_negative` refuses any adjustment taking payment + adjustments + NEW below zero.
- MINOR — DB CHECKs weaker than the domain; one bad row broke `history()` forever. Fix: named constraints `currency_iso_4217` (`GLOB '[A-Z][A-Z][A-Z]'`), `image_sha256_hex` (64 lowercase hex), `reason_not_blank` (trimmed with every character `str.strip()` removes, not only spaces), `created_at_utc_text` / `paid_at_utc_text` (exact `YYYY-MM-DDTHH:MM:SS.ffffff+00:00` plus a `strftime` round trip that rejects impossible dates), `id_positive` (an explicit id -1 used to block every later append), `entry_kind_shape`. The adapter now always writes timestamps with microseconds (fixed width). Domain: a payment reason, when given, cannot be blank.
- MINOR — no amount bound: SUM overflow broke `balance()` permanently, and use cases raised after their write committed. Fix: `MAX_AMOUNT_MINOR = 10**11` (S/ 1,000,000,000.00) enforced at entry validation and in `build_correction` (`InvalidAmountError`, never `OverflowError`), plus `amount_in_range` CHECK (`BETWEEN -max AND max`, which cannot overflow the way `abs()` of INT64_MIN does). Not enforced in `parse_amount`: its callers (extractor, labels tool, outside this task's surface) would report an over-cap amount as "not a canonical decimal".
- MINOR — concurrent duplicate image surfaced as raw `sqlite3.IntegrityError`. Fix: `append` maps an image-hash conflict to port-level `ImageAlreadyStoredError(existing_id)`, which `RegisterPayment` turns into `DuplicateImageError(existing_id)`; image and same-reference checks share the atomic block with the insert.
- NIT — offset paging drifted when entries arrived between pages. Fix: keyset paging `history(limit, before_id=None)` and `ListHistory.execute(limit, before_id)`; adapter refuses `limit < 1`.
- NIT — missing test for an adjustment carrying only `image_key`. Added (refused by `entry_kind_shape`).
- Schema version: `PRAGMA user_version = 1` set on creation inside the same `BEGIN IMMEDIATE`; a higher version, or an existing unversioned `ledger_entries` table, raises `UnsupportedSchemaError` instead of being stamped.

Test-first, two work units, RED observed before each implementation (interface-only stubs added first so RED is behavioral, not an import error):

- Atomicity: RED 15 failed / 53 passed (adapter + application) — both racers succeeded, effective 5000 after correct-vs-void, raw `IntegrityError` for the duplicate image, missing `possible_duplicates`, writes kept after a failing balance. GREEN 268 passed. Concurrency tests repeated 15× in a loop: 0 failures (~2 s per run).
- Constraints/cap/paging/version: RED 78 failed / 268 passed. GREEN 346 passed.
- Concurrency tests hold each racer after its read on a barrier with a 0.3 s window; with the unit of work the other racer is blocked, the barrier breaks and the writes serialize. Assertions use invariants valid for either ordering.

Verifier scripts re-run (copies adapted: barrier tolerates serialization, raw timestamps in the stored format, keyset paging):

- concurrency: double void → one adjustment, loser `AlreadyVoidedError`; double correct → effective 30000, loser `NoChangeError`; correct vs void → effective 30000 (void first, then correct), balance = sum of rows; same image → one row, loser `DuplicateImageError`; same reference → second flagged `(1,)`. Out of scope, unchanged: one repository used from two threads raises `ProgrammingError` (sqlite3 `check_same_thread`).
- invalid rows: every previously accepted bad row now refused (blank reason, negative effective, lowercase/digit currency, uppercase/garbage hash, garbage/non-UTC `created_at`); `2**63` raises `InvalidAmountError`. Lossless `1.0` / `'100'` amounts are still coerced to INTEGER by `STRICT` (readable, not a defect).
- SQL paths: all rewrite/remove paths still refused; explicit id -1 refused by `id_positive` and a later append succeeds. DDL (`ALTER TABLE … DROP COLUMN`) remains out of the threat model.
- arithmetic: keyset pages concatenate to the full history; no overlap when an entry arrives between pages; `history(-1)` raises `ValueError`; oversized amounts refused before any write.
- schema mutation: 33 mutations (19 carried over from the verifier, `drop_currency_len` retired with the column check it targeted, plus 14 for the new guards): 31 killed; 2 survivors are equivalent mutants (`drop_unique_hash`: the no-overwrite trigger already refuses a repeated hash; `drop_kind_enum`: `entry_kind_shape` already restricts `kind`).
- Final checks: `uv run pytest -q` → 346 passed; `uv run ruff check .` → All checks passed; `uv run ruff format --check .` → 51 files already formatted.
- Commits: `f82a37a` (`fix(ledger): make corrections and registrations atomic`, 492+/37−) and `a82478c` (`fix(ledger): harden schema constraints and paging`, ≈442+/44− plus this document). Both above the ~400-line heuristic; over half of each is tests.
- Independent verification (isolated export of `f82a37a` + `a82478c`): 346 passed, concurrency tests stable, 32/35 mutants killed; of the 7 original findings 5 fixed and 2 partial. Every remaining gap moved to T2.2.

### T2.2 — ledger gaps from the independent verification of T2.1 (done)

The verifier ran T2.1 on an isolated export (346 passed, concurrency tests stable, 32/35 mutants killed) and left these gaps; numbering follows its report.

1. MINOR (raw SQL) — NUL bypass: SQLite `GLOB` and `length()` stop at the first NUL, so `currency = 'PEN\x00x'`, a valid hash or timestamp followed by `'\x00junk'` passed the CHECKs and then made `history()` raise forever. Fix: `instr(col, char(0)) = 0` (which does not stop at NUL) inside `currency_iso_4217`, `image_sha256_hex`, `created_at_utc_text`, `paid_at_utc_text`, and new named constraints `reason_no_nul`, `reference_no_nul`, `image_key_no_nul`, `extracted_json_no_nul`. `kind` and `source` use `IN`, whose comparison does not stop at NUL either (tests added; they already passed). The domain mirrors it, so a domain-valid entry never reaches the database only to fail as raw `IntegrityError` or `UnicodeEncodeError`: `reason`, `reference`, `image_key` and `extracted_json` refuse NUL and characters UTF-8 cannot encode (lone surrogates), with `InvalidEntryError`. Same class, found while fixing this: `CAST(x'ff41' AS TEXT)` stores bytes that are not UTF-8 unchecked, and every later read failed with "Could not decode to UTF-8"; no SQL function can validate UTF-8 inside a CHECK, so the adapter decodes stored TEXT with `errors="replace"` (U+FFFD) instead of failing.
2. MINOR (pre-existing) — concurrent first open: `PRAGMA journal_mode = WAL` does not wait on the busy timeout (SQLite answers "database is locked" at once while another connection holds a lock, to avoid a deadlock), so two processes opening a new file together made one fail (verifier: 1 of 2, 7 of 8). Fix: the constructor retries the WAL switch and schema creation as one step (`_prepare`), pausing 20 ms between attempts, until `busy_timeout` has passed, then raises `LedgerBusyError`. `busy_timeout` is now a keyword argument (default `BUSY_TIMEOUT_SECONDS`).
3. MINOR (raw SQL) — upper bound: the effective-amount trigger only refused below zero, so `+MAX_AMOUNT_MINOR` on a `MAX_AMOUNT_MINOR` payment left 2×max, which no in-range adjustment can void. Fix: trigger `ledger_entries_effective_in_range` computes the effective amount once and refuses below zero (same message as before) or above `MAX_AMOUNT_MINOR` (`… exceed the maximum`). A payment held at the maximum can still be voided.
4. Surviving mutants: tests for a payment's *first* adjustment overshooting below zero and above the maximum (kill `LEFT JOIN` → `JOIN`); a raw payment row of `-MAX_AMOUNT_MINOR - 1` reports `amount_in_range` (kills `BETWEEN -max - 1`: CHECKs run in declaration order, otherwise `entry_kind_shape` reports it); an adjustment of `-MAX_AMOUNT_MINOR - 1` is refused and `-MAX_AMOUNT_MINOR` voids a maximum payment.
5. NITS — error mapping: SQLite's busy/locked errors (`sqlite_errorcode` `SQLITE_BUSY`/`SQLITE_LOCKED`) become the port's new `LedgerBusyError` in every adapter operation (`atomic` BEGIN/COMMIT, `append`, every read), so use cases never surface raw `sqlite3.OperationalError`; reads still answer while a writer holds the lock (WAL). `ListHistory` accepts only `int` (a `bool` is refused) `limit` in 1..`MAX_HISTORY_PAGE` (500) and `before_id` in 1..`MAX_ENTRY_ID` (2**63 - 1): `TypeError` for the wrong type, `ValueError` out of range, never `OverflowError`. The adapter's `history()` applies the same type check and the int64 range. Same class, extended: `require_entry` (used by `GetEntryDetail`, `CorrectEntry`, `VoidEntry`) refuses a non-`int` id with `TypeError` and reports an id outside 1..2**63 - 1 as `EntryNotFoundError` instead of `OverflowError`.
6. Schema version 2 (the CHECK and trigger changes alter the schema). No ledger database was ever used (the bot has never run), so there is no migration: a version 1 file is refused with `UnsupportedSchemaError` ("needs 2 and cannot migrate from it") and left untouched. Newer and unversioned files are refused as before.
7. Threat model (documented, not implemented). In scope: any DML a second connection can run, with any values (raw `INSERT`, `UPDATE`, `DELETE`, `INSERT OR REPLACE`). Outside it, like DDL (`ALTER TABLE`, `DROP TRIGGER`): writes to SQLite's internal `sqlite_` tables and per-connection switches. A raw row with `id = 2**63 - 1`, or `UPDATE sqlite_sequence SET seq = 2**63 - 1`, exhausts AUTOINCREMENT and every later append fails with "database or disk is full" (checked in a scratch run); triggers cannot be attached to `sqlite_` tables, so capping explicit ids would still leave `sqlite_sequence` open. `PRAGMA ignore_check_constraints = ON` on the writing connection disables every CHECK (a garbage timestamp then breaks `history()`; triggers still refuse UPDATE/DELETE). Both need the same file access as DDL; the defense is file permissions and backups, not the schema.

Test-first (work unit 1: items 1, 3, 4, 6):

- RED 23 failed / 194 passed (adapter + domain): NUL rows accepted, non-UTF-8 read failing, upper bound missing, version still 1, domain accepting NUL and lone surrogates. Already passing before (characterization, mutant killers): first overshoot below zero, `huge-negative` payment row, `kind`/`source` with NUL, adjustment lower bound. GREEN: suite 489 passed.
- Targeted mutation run on a copy (scratch script): 14/14 killed — `LEFT JOIN`→`JOIN`, `BETWEEN -max - 1`, NUL check removed everywhere / from currency / hash / timestamps / free-text columns, upper bound `+ 1` and removed, lower bound removed, version 1 accepted, text decoding strict, domain NUL check and UTF-8 check removed.
- Commit: `8949486` (`fix(ledger): refuse NUL text and out-of-range effective amounts in the schema`, 231+/27−).

Test-first (work unit 2: items 2 and 5; interface-only stubs first — `LedgerBusyError`, the `busy_timeout` keyword, `MAX_HISTORY_PAGE`/`MAX_ENTRY_ID` — so RED is behavioral):

- RED 24 failed / 160 passed (adapter + application): raw `database is locked` from the first open (deterministic test, and attempt 2 of the 8-thread race), from `append`/`atomic` and from the three write use cases; `history()`/`ListHistory` accepting `True`, `2.0`, `2.5`, raising `OverflowError` for 2**63 and allowing a 501-entry page; `GetEntryDetail(2**63)` raising `OverflowError`. GREEN: suite 519 passed.
- Concurrency tests: the retry test is deterministic (the holder's lock is released from the patched pause, so it also proves a retry happened); the timeout tests use `busy_timeout` 0.1–0.2 s with the lock held throughout; the race test opens 20 new files from 8 threads released by a barrier. `TestBusyDatabase` + `tests/application/test_concurrency.py` repeated 15×: 15/15 runs passed (10 tests each). The verifier's process-based `open_race.py` re-run: 2 and 8 fresh openers, 3 trials each, all opened (was 1/2 and 7/8 failing).
- Targeted mutation run: 14/14 killed (open retry removed, busy mapping removed, `append`/`BEGIN` unmapped, adapter type/`bool`/range/`before_id` checks removed, application type/range/page-max/entry-range/entry-type checks removed). Retry-forever (deadline ignored) hangs the timeout test, caught only by the scratch script's 120 s limit; no pytest timeout plugin is installed.
- Final checks: `uv run --frozen pytest -q` → 519 passed; `uv run --frozen ruff check .` → All checks passed; `uv run --frozen ruff format --check .` → 51 files already formatted.
- Commits: `8949486` (unit 1) and `f55fd4f` (`fix(ledger): wait out locks on first open and map busy and paging errors`, unit 2, this document). Not changed: `created_by` beyond int64 still raises `OverflowError` on append (Telegram ids are far below it; the phase 3 bot owns that input).
- Independent verification pending.

### T3 — extractor + eval harness (checks green; `.env.example` pending)

- Dependency `anthropic[bedrock]>=2.3.0` (resolved anthropic 2.3.0, boto3/botocore 1.43.122). Real client `anthropic.AnthropicBedrockMantle(aws_region=..., aws_profile=...)`, injected so tests use fakes; the `anthropic[bedrock]` extra and `anthropic.`-prefixed model ids come from the claude-api skill (`shared/model-migration.md`, "Amazon Bedrock model IDs"). Defaults: `anthropic.claude-opus-5-5`, effort `medium`; env `AWS_REGION` (required), `AWS_PROFILE`, `EXTRACTOR_MODEL`, `EXTRACTOR_EFFORT`.
- Layout: `ports/amount_extractor.py` (`AmountExtractor`, `Extraction`, statuses ok/unreadable/not_a_receipt/refused/failed), `adapters/extraction/{image_format,receipt_prompt,claude_bedrock}.py`, `tools/{labels,make_labels,eval_extractor}.py`.
- Request: base64 image block (media type from magic bytes; JPEG/PNG/WebP/GIF, anything else → `failed` with no call), frozen system prompt naming the amount and its traps, `max_tokens` 16000, explicit `output_config.effort`; no `thinking`, sampling params, `tool_choice` or prefill.
- Structured output: `output_config.format` with a closed JSON schema (`platform-availability.md`: "Structured outputs / strict tool use | Yes | Yes | Yes" for Bedrock). The amount is re-validated with `parse_amount`; non-canonical amount, missing/invalid currency, or an amount equal to the operation number → `unreadable`.
- `stop_reason` is checked first: `refusal` → `refused` (with `stop_details.category`); any other non-`end_turn` → `failed`. SDK errors map to `failed` through a most-specific-first typed chain. AWS credential resolution errors (outside the SDK's typed errors) propagate on purpose. Refusal-fallback middleware not wired (see open questions in the T3 handoff).
- Eval: verifies labels, image SHA-256 and settings before any call; prints a Bedrock warning (region, profile, models, request count) and exits 0 unless `--yes`; accepts `250`, `250.00`, `250,00`; reads comma- or semicolon-separated CSV (spreadsheet re-saves); writes `data/eval/results-<model>-<UTC>.csv`.
- Test-first, two cycles, RED observed as collection errors before the modules existed:
  - port + adapter: RED 3 collection errors (`ModuleNotFoundError: payments_bot.ports.amount_extractor`, `...adapters.extraction.claude_bedrock`) → GREEN 90 passed; suite without tools 214 passed.
  - tools: RED 3 collection errors (`ModuleNotFoundError: payments_bot.tools.labels`, `...tools.eval_extractor`) → GREEN 39 passed.
- Final checks: `uv run pytest -q` → 253 passed; `uv run ruff check .` → All checks passed; `uv run ruff format --check .` → 49 files already formatted; `make_labels --help` and `eval_extractor --help` → exit 0. No remote call was made; all tests are offline.
- Commits: `19d0c47` (port + Bedrock adapter), `cfac151` (labels template + eval harness).
- Work units: (1) port + Bedrock adapter, (2) labels template + eval harness + README. Size is above the ~400-line heuristic (about 1160 and 1150 authored lines, excluding `uv.lock`; over half of it tests); each commit is one coherent behavior.
- Pending: `.env.example` listing `AWS_REGION=` with `AWS_PROFILE`, `EXTRACTOR_MODEL`, `EXTRACTOR_EFFORT` commented out (an empty `AWS_PROFILE` would reach the AWS SDK).

### T3.1 — fixes from the independent verification of T3 (done)

Extraction contract (for the phase 3 bot):

- `ok` is the only status that may be recorded without asking (`Extraction.auto_recordable`). It requires `confidence == high` and every guard passing.
- `needs_confirmation` carries the candidate `amount_minor` and `currency` but must never be recorded automatically: the bot shows it and records only what the user confirms or types. Causes: medium or low confidence, or an amount whose digits repeat the receipt's security code (a 3-digit code coincides with a real amount often enough that refusing would block legitimate payments).
- `unreadable` (no candidate shown) for an amount that is zero, above `MAX_AMOUNT_MINOR`, or repeats the operation number's digits. Chosen over `needs_confirmation` because an operation-number collision is a misread, not a coincidence, and showing the number invites the user to confirm a wrong amount; typing the amount is the safer ask.
- Product default: the recorded amount is the **transferred amount** (Yape "Monto", Banco de la Nación "Importe"), not a "Total" that adds fees. Stated in the prompt and README.
- Dates: `paid_on` (Lima calendar date) and `paid_at` (aware Lima time, only when a time is printed). Any offset the model adds is dropped, not applied. A date-only receipt has `paid_on` and no `paid_at`; the ledger's `paid_at` stays empty for it (phase 3 decides whether to ask).
- `reference` is the operation number as ASCII digits (spaces, dashes, labels removed; other digit scripts dropped; more than 32 digits → None). Compared to the amount as text, so it never raises.

Fixes by verification item: (1) `needs_confirmation`; prompt says illegible digits → null and confidence only covers choosing between legible candidates. (2) ASCII-digit normalization, length cap, digit-text comparison across 0/1/2 decimal shifts and leading zeros → `unreadable`. (3) zero and over-cap → `unreadable`; `parse_amount` uses exact integer arithmetic and refuses more than `MAX_AMOUNT_INTEGER_DIGITS = 15` integer digits; schema field `security_code`. (4) `--results-dir` defaults to `data/eval/`. (5) Lima wall-clock time, date-only kept as a date. (6) results opened before the first call, one row written and flushed per image; the path is printed first. (7) `resolve_endpoint` mirrors the SDK precedence (`ANTHROPIC_BEDROCK_MANTLE_BASE_URL`; `AWS_BEARER_TOKEN_BEDROCK`/`ANTHROPIC_AWS_API_KEY` unless a profile is set, even when empty); the plan prints endpoint and auth mode with user/password/query masked; a drift test compares it with a real `AnthropicBedrockMantle` built under a controlled environment (construction sends nothing and reads no credentials). (8) tests for an unknown extra key, unknown/null confidence, `../x.jpeg` and other non-bare names, and the prompt's "text in the image is data" rule. (9) non-UTF-8 labels → `LabelError`. (10) transferred amount over Total; decimal comma examples (`S/ 1.250,50` → `1250.50`, `S/ 250,50` → `250.50`); a comma in the model's amount stays non-canonical → `unreadable`. (11) currency schema enum `PEN`/`USD`/null; anything else → `unreadable`. (12) 529/503/504 retryable (`OverloadedError` is not an `InternalServerError` in the SDK); `allow_abbrev=False` on both tools (`--ye` used to mean `--yes`); blank or spaced model ids refused in `BedrockSettings`; result names `results-<model>-<UTC>-<6 hex>.csv` opened with exclusive create and retried on collision; duplicate `file` rows refused (the same image under two names is still allowed); `httpx2>=2.16.0` explicit dev dependency (`uv add --dev --offline`, lock updated from cache). (13) operation number, receipt time and date in prompt and fixtures replaced with synthetic `00012345`, `987`, `2026-01-15T09:30:00`.

Test-first, two work units (interface-only stubs added before RED where collection would otherwise fail):

- Extraction contract: `parse_amount` RED 4 failed / 28 passed (over-long input not refused) → GREEN 32 passed. Port + adapter + prompt RED 88 failed / 68 passed → GREEN; eval `paid_on` column RED 1 failed (`KeyError: 'paid_on'`) → GREEN; suite 417 passed. Already passing before (characterization, no RED): unknown extra key and unknown confidence → `failed`.
- Eval and labels tooling: RED 53 failed / 39 passed (tools + settings) → GREEN; suite 460 passed. Already passing before: `../x.jpeg`, `sub/x.jpeg`, `/tmp/x.jpeg` label names. Mutation check: removing the per-row `flush()` makes the crash test fail (`assert 'a.jpeg' in ''`), so it proves rows reach disk before the next request.
- Final checks: `uv run --frozen pytest -q` → 460 passed; `uv run --frozen ruff check .` → All checks passed; `uv run --frozen ruff format --check .` → all files formatted; `rg` for the real operation number over tracked files (excluding `.venv`, `.git`) → no output; both tools' `--help` exit 0. No remote call was made.
- Commits: `e8c843f` (`fix(extraction): ask for confirmation instead of trusting doubtful amounts`, 600+/113−) and `981f85a` (`fix(eval): keep paid results and show the real endpoint before sending`; tools, labels, README, this document). Above the ~400-line heuristic; over half is tests.
- Not changed (outside this task): refusal-fallback middleware stays a phase 3 decision. The real operation number and receipt time that remained in the history of `19d0c47`/`e8c843f` were scrubbed by the history rewrite below.
- Verified independently (isolated export of `e8c843f` + `981f85a`, 460 passed, also with sockets blocked): all 13 fixes hold and no path leads to a wrong auto-recordable amount. 2 minor findings and 6 nits moved to T3.2.

### T3.2 — gaps from the independent verification of T3.1 (done)

The verifier ran T3.1 on an isolated export (460 passed, also with sockets blocked; all 13 fixes hold; no path to a wrong auto-recordable amount) and left these gaps; numbering follows the hand-off.

1. MINOR — `extract()` could raise, breaking the port contract: a list or dict `confidence` raised `TypeError: unhashable type`; nesting deeper than the parser's limit raised `RecursionError` and an integer literal over 4300 digits raised `ValueError` (only `JSONDecodeError` was caught). Fix: `confidence` is type-checked before the membership test; `json.loads` failures of any `ValueError` or `RecursionError` become `_SchemaMismatchError` → `failed` (raw text kept).
2. MINOR — CSV formula injection in the eval results (`provider`, `error`, `raw_json` carry text the image can steer; `file` comes from the labels; the UTF-8 BOM makes spreadsheet apps open the file). Fix: every text cell that starts with `=`, `+`, `-`, `@`, tab or CR is written with a leading `'`; the number columns (`expected_amount`, `got_amount`, `input_tokens`, `output_tokens`) are written unchanged.
3. N1 — operation-number guard bypass: the 32-digit cap applied before leading zeros were stripped (28 zeros + `12345` gave reference None and S/ 123.45 came back `ok`), and digits of other scripts were dropped (`1２345` → `1345`, or the guard skipped). Fix: `_ascii_digits` converts every character that stands for one digit (`unicodedata.digit`: fullwidth, Arabic-Indic, Persian, superscript) and drops the rest; the guards (`_spells`) compare the amount with every significant digit, with no length cap; the cap now only limits the stored `reference` (up to 32 digits, leading zeros kept). Docstrings fixed. `unicodedata.digit` instead of `decimal` on purpose: `decimal` still drops superscripts and would corrupt `1²345` the same way.
4. N2 — repeated JSON keys, last one won (`"amount":"250", …, "amount":"999"` → `ok` 999.00). Fix: `object_pairs_hook` refuses a repeated key → `failed` naming the key.
5. N3 — the banner could print a secret: `urlsplit` finds no user part without `scheme://`, and a `/`, `?` or `#` inside a password ends the user part early. Fix: `_mask_url_secrets` uses no URL parser: an empty URL stays empty ("(empty)"), a URL that is not `http(s)://` is not shown at all, anything before the last `@` is masked, the query is masked and the fragment dropped.
6. N4 — eval errors after money was spent: a results-file `SetupError` (name collision) or `OSError` (`ENAMETOOLONG` from a 300-character model id) escaped `main` as a traceback, and with several models after earlier models' requests. Fix: `open_results_files` creates every model's file, with its header row, before the first request; `create_results_file` turns any `OSError` but `FileExistsError` into `SetupError`; on failure the files created in this run are closed and removed; `main` prints `error: …; nothing was sent` and exits 2. Files are closed through an `ExitStack`, also when an extractor raises.
7. N5 — labels: `A.jpeg`/`a.jpeg` and NFC/NFD spellings were accepted as distinct rows although macOS opens one file for both. Fix: duplicates are keyed on NFC(casefold(NFD(name))) and the error names the first spelling; `expected_amount` 0 → `LabelError`. Consequence: on a case-sensitive Linux file system two images whose names differ only in case cannot both be labeled (rename one).
8. N6 — `paid_at` accepted years 0001 and 9999 (0001 even got the LMT offset −05:08:12). Fix: `EARLIEST_RECEIPT_DATE = 2016-01-01` up to tomorrow in Lima from an injectable `clock` (constructor keyword, default `datetime.now(UTC)`); out of range → `paid_on`/`paid_at` None, status unchanged. `RECEIPT_TIMEZONE` is a fixed `timezone(-5h, "PET")` (Peru has no DST since 1994), so no time zone database is needed.
9. Surviving mutants / test gaps: references of 9 and 32 digits are kept and 33 dropped (pins `MAX_PRINTED_NUMBER_DIGITS`); an empty `ANTHROPIC_BEDROCK_MANTLE_BASE_URL` resolves to an empty endpoint "from" that variable, in the precedence table and in the drift test against a real `AnthropicBedrockMantle`; the prompt test asserts the whole confidence-section sentence ending "the amount is null."; new tests drive a real `AnthropicBedrockMantle` (`skip_auth`, `max_retries=0`, `httpx2.MockTransport`, no socket, no credentials) so 500/502/503/504/529 are proven retryable and 400/409/413/422 not, through the SDK's own status mapping.

Contract update for the phase 3 bot (supersedes the T3.1 `reference` bullet): `reference` converts digits of other scripts instead of dropping them; the guards see every digit, so a long or zero-padded operation number can no longer hide a collision; `paid_on`/`paid_at` are None for an implausible date.

Test-first, two work units (an interface-only `clock` keyword was added before RED so the failures were behavioral, not `TypeError`):

- Extraction (items 1, 3, 4, 5, 8, 9): RED 29 failed / 200 passed (extraction tests) — list/dict confidence and both parser limits raising, repeated key read as 999, zero-padded / fullwidth / mixed / Arabic-Indic operation numbers not caught, other-script references dropped, fullwidth security code not caught, out-of-range dates kept, zone-database offset, five URL shapes leaking the password. Already passing before (characterization, mutant killers): 9-/32-/33-digit references, `½`, empty base URL (table and drift), whole prompt sentence, numeric confidence, the Mantle 5xx/4xx mapping, scheme-relative/query/fragment/`ftp` URLs, in-range dates. GREEN: extraction 230 passed; suite 574 passed. Targeted mutation run (scratch script, in place with restore): 21/21 killed after adding one case (the `http(s)`-only rule first survived; a scheme-less URL with `://` in its query now pins it).
- Eval and labels (items 2, 6, 7): RED 18 failed / 64 passed (tool tests) — formula cells written verbatim (6 triggers and the file name), results files created per model after the extractor was built, `SetupError` and `OSError` escaping `main`, case/NFC/NFD duplicates accepted, zero accepted. Already passing before: plain text unchanged. GREEN: tools 82 passed. Targeted mutation run: 10/10 killed (escape removed, tab/CR/minus triggers removed, no cleanup, `OSError` not converted, `SetupError` not caught, case or normalization ignored, zero accepted).
- Final checks: `uv run --frozen pytest -q` → 593 passed; `uv run --frozen ruff check .` → All checks passed; `uv run --frozen ruff format --check .` → 51 files already formatted; `rg` for the real operation number over the tree (excluding `.venv`, `.git`) → no output; both tools' `--help` exit 0. No remote call was made.
- Commits: `a148c0d` (`fix(extraction): keep every malformed answer a failed result and guard all printed digits`, 331+/50−) and `31e5731` (`fix(eval): create results files before sending and keep spreadsheet formulas inert`; tools, labels, README, this document).
- Independent verification pending.

## History rewrite (privacy scrub, user-approved, before the first push)

- Why: a real operation number and receipt time from the user's own receipt had been copied into prompt examples and test fixtures (T3, removed in T3.1) and were still readable in the local history. Nothing had been pushed.
- How: `git filter-repo --force --replace-text` replaced those values with synthetic ones (`00012345`, `2026-01-15T09:30:00` and its variants) in every blob; commit messages had no occurrence. A pre-rewrite bundle was kept outside the repository as a temporary safety net.
- Check: `git cat-file --batch-all-objects --batch` (all objects, reachable or not) → 0 matches for the scrubbed values.
- Commit ids from `19d0c47` onward changed; every id in this document was updated (`451bff2` and `9875869` are unchanged). Remote `origin` = `https://github.com/raicoacosta/ocr-yape-plin.git` (re-added after the rewrite; push still pending).

## Next step

Create `.env.example` and check T3. Then T4 — user fills data/eval/labels.csv and authorizes AWS profile/region; run eval.
