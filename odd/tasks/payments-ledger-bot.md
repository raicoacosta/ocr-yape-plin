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
- [ ] T3 — Phase 2a: extractor port, amount parsing, Claude-on-Bedrock adapter (unit-tested with fakes), labels template + eval script. Route: delegated writer (2+ non-trivial files).
- [ ] T4 — Phase 2b: user confirms expected amounts for the 22 images and the eval runs against Bedrock. Blocked on: user authorization of AWS profile/region and Bedrock model access.

## Acceptance criteria

- T2: UPDATE/DELETE on ledger rows fail at the DB level; balance = sum of entries per currency; correct/void produce adjustments with the right delta; duplicate images rejected; all tests green.
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

### T2 — append-only ledger (done)

- Layout: `domain/{money,ledger,errors}.py`, `ports/ledger_repository.py` (Protocol, no update/delete), `application/{payments,corrections,queries,errors,clock}.py`, `adapters/persistence/sqlite_ledger.py`.
- SQLite defense in depth: `STRICT` table, CHECK constraints per kind, `BEFORE UPDATE`/`BEFORE DELETE` triggers abort with `ledger is append-only`, adjustment-target trigger (must be a payment in the same currency), and a no-overwrite trigger that blocks `INSERT OR REPLACE` (which otherwise deletes rows without firing delete triggers; confirmed by dropping the trigger in a scratch run: the row was silently rewritten).
- Test-first, three cycles, RED observed each time as collection errors (`ModuleNotFoundError`) before the module existed:
  - domain: RED 2 collection errors → GREEN 69 passed.
  - SQLite adapter: RED 1 collection error → GREEN 33 passed.
  - application: RED conftest `ModuleNotFoundError` → GREEN, full suite 123 passed.
- Final checks: `uv run pytest -q` → 123 passed; `uv run ruff check .` → All checks passed; `uv run ruff format --check .` → 30 files already formatted.

## Next step

T3 — extractor port, amount parsing, Claude-on-Bedrock adapter (fakes only, no remote calls), labels template + eval script.
