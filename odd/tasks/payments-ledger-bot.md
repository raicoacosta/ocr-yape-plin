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
- [ ] T3 — Phase 2a: extractor port, amount parsing, Claude-on-Bedrock adapter (unit-tested with fakes), labels template + eval script. Route: delegated writer (2+ non-trivial files). Status: code, tests, README and all checks done; only `.env.example` pending (writer's write was denied by permission settings).
- [ ] T4 — Phase 2b: user confirms expected amounts for the 22 images and the eval runs against Bedrock. Blocked on: user authorization of AWS profile/region and Bedrock model access.

## Acceptance criteria

- T2: UPDATE/DELETE on ledger rows fail at the DB level; balance = sum of entries per currency; correct/void produce adjustments with the right delta; duplicate images rejected; all tests green.
- T2.1: concurrent corrections/voids/registrations serialize (no double-apply, duplicate image → `DuplicateImageError`); every row the DB accepts is readable by the domain; amounts capped at `MAX_AMOUNT_MINOR`; history pages by `before_id`; unknown schema versions refused.
- T3: extractor returns the amount in minor units or an explicit "unreadable" result; prompt and tests guard against operation number / security code / phone being taken as the amount; eval script reports per-image and overall accuracy.
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
- Commits: `ae1fbc2` (`fix(ledger): make corrections and registrations atomic`, 492+/37−) and the following `fix(ledger): harden schema constraints and paging` (≈442+/44− plus this document). Both above the ~400-line heuristic; over half of each is tests.

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
- Commits: `4786855` (port + Bedrock adapter), `3faafa8` (labels template + eval harness).
- Work units: (1) port + Bedrock adapter, (2) labels template + eval harness + README. Size is above the ~400-line heuristic (about 1160 and 1150 authored lines, excluding `uv.lock`; over half of it tests); each commit is one coherent behavior.
- Pending: `.env.example` listing `AWS_REGION=` with `AWS_PROFILE`, `EXTRACTOR_MODEL`, `EXTRACTOR_EFFORT` commented out (an empty `AWS_PROFILE` would reach the AWS SDK).

## Next step

Create `.env.example` and check T3. Then T4 — user fills data/eval/labels.csv and authorizes AWS profile/region; run eval.
