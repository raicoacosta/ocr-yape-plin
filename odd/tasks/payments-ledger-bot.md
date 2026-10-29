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

- [ ] T1 — Project scaffold: uv project, pytest, ruff, git repo, `.gitignore`. Route: delegated writer (bundled with T2).
- [ ] T2 — Phase 1: domain model, SQLite append-only ledger, use cases, tests. Route: delegated writer (2+ non-trivial files).
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

(none yet)

## Next step

T1 + T2 via delegated writer.
