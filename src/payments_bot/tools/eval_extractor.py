"""Score the receipt extractor against hand-labeled images, per model.

    uv run python -m payments_bot.tools.eval_extractor [--model ID ...] [--yes]

This SENDS every labeled receipt image to Amazon Bedrock, once per model. Without --yes it only
prints what it would send. Every check that needs no network (labels, image hashes, settings)
runs first, so a typo never costs a request.
"""

import argparse
import csv
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path

from payments_bot.adapters.extraction.claude_bedrock import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    EFFORT_LEVELS,
    BedrockSettings,
    ExtractorConfigError,
    build_bedrock_extractor,
)
from payments_bot.application.clock import Clock, utc_now
from payments_bot.ports.amount_extractor import AmountExtractor, Extraction, ExtractionStatus
from payments_bot.tools.labels import (
    CSV_ENCODING,
    DEFAULT_IMAGES_DIR,
    DEFAULT_LABELS_PATH,
    Label,
    LabelError,
    format_minor,
    read_labels,
    sha256_hex,
)

type ExtractorFactory = Callable[[BedrockSettings], AmountExtractor]

RESULT_COLUMNS = (
    "file",
    "sha256",
    "expected_amount",
    "expected_currency",
    "got_amount",
    "got_currency",
    "match",
    "status",
    "confidence",
    "reference",
    "paid_at",
    "provider",
    "input_tokens",
    "output_tokens",
    "error",
    "raw_json",
)
TABLE_COLUMNS = ("file", "expected", "got", "match", "status", "confidence")


class SetupError(Exception):
    """The evaluation cannot start; nothing has been sent."""


@dataclass(frozen=True, slots=True)
class Case:
    label: Label
    image: bytes


@dataclass(frozen=True, slots=True)
class Outcome:
    label: Label
    extraction: Extraction

    @property
    def matched(self) -> bool:
        extraction = self.extraction
        return (
            extraction.status is ExtractionStatus.OK
            and extraction.amount_minor == self.label.expected_amount_minor
            and extraction.currency == self.label.expected_currency
        )


@dataclass(frozen=True, slots=True)
class Score:
    model: str
    matches: int
    total: int
    input_tokens: int
    output_tokens: int

    @classmethod
    def of(cls, model: str, outcomes: Sequence[Outcome]) -> "Score":
        usages = [o.extraction.usage for o in outcomes if o.extraction.usage is not None]
        return cls(
            model=model,
            matches=sum(outcome.matched for outcome in outcomes),
            total=len(outcomes),
            input_tokens=sum(usage.input_tokens for usage in usages),
            output_tokens=sum(usage.output_tokens for usage in usages),
        )

    def describe(self) -> str:
        return (
            f"accuracy {self.matches}/{self.total} ({self.matches / self.total:.1%}) · "
            f"tokens: input {self.input_tokens}, output {self.output_tokens}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m payments_bot.tools.eval_extractor",
        description=(
            "Score the receipt amount extractor against the hand-labeled images in the labels "
            "CSV. This SENDS each labeled receipt image to Amazon Bedrock, once per model; "
            "nothing is sent unless --yes is given."
        ),
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS_PATH,
        help="labels CSV from make_labels, filled in (default: %(default)s)",
    )
    parser.add_argument(
        "--images",
        type=Path,
        default=DEFAULT_IMAGES_DIR,
        help="directory with the receipt images (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        action="append",
        metavar="ID",
        help=f"Bedrock model id; repeat to compare models (default: $EXTRACTOR_MODEL or "
        f"{DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        help=f"output_config.effort (default: $EXTRACTOR_EFFORT or {DEFAULT_EFFORT})",
    )
    parser.add_argument("--region", help="AWS region of Amazon Bedrock (default: $AWS_REGION)")
    parser.add_argument(
        "--profile",
        help="AWS profile (default: $AWS_PROFILE, else the default AWS credential chain)",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        help="where to write results-<model>-<UTC time>.csv (default: the labels directory)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="really send the images to Amazon Bedrock (without it: print the plan and exit)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    extractor_factory: ExtractorFactory = build_bedrock_extractor,
    clock: Clock = utc_now,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        labels = read_labels(args.labels)
        cases = load_cases(labels, args.images)
        if not cases:
            print(
                f"error: no row in {args.labels} has an expected_amount yet; fill them in first",
                file=sys.stderr,
            )
            return 1
        runs = resolve_settings(args, env)
    except (LabelError, SetupError, ExtractorConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_plan(cases, skipped=len(labels) - len(cases), runs=runs, labels_path=args.labels)
    if not args.yes:
        print("Dry run: nothing was sent. Re-run with --yes to send the images.")
        return 0

    results_dir = args.results_dir or args.labels.parent
    timestamp = clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    scores = [
        evaluate(settings, cases, extractor_factory, results_dir, timestamp) for settings in runs
    ]
    print("\n== summary ==")
    width = max(len(score.model) for score in scores)
    for score in scores:
        print(f"{score.model.ljust(width)}  {score.describe()}")
    return 0


def load_cases(labels: Sequence[Label], images_dir: Path) -> list[Case]:
    """Labeled rows with their image bytes, refusing missing or changed images."""
    cases = []
    for label in labels:
        if label.expected_amount_minor is None:
            continue
        path = images_dir / label.file
        if not path.is_file():
            raise SetupError(f"{label.file}: image not found in {images_dir}")
        image = path.read_bytes()
        if sha256_hex(image) != label.sha256:
            raise SetupError(
                f"{label.file}: image changed since it was labeled (sha256 differs); "
                "check its label and update the hash"
            )
        cases.append(Case(label, image))
    return cases


def resolve_settings(args: argparse.Namespace, env: Mapping[str, str]) -> list[BedrockSettings]:
    """One settings object per model; flags override the environment."""
    flags = {
        "AWS_REGION": args.region,
        "AWS_PROFILE": args.profile,
        "EXTRACTOR_EFFORT": args.effort,
    }
    merged = {**env, **{name: value for name, value in flags.items() if value is not None}}
    base = BedrockSettings.from_env(merged)
    models = dict.fromkeys(args.model or [base.model])
    return [replace(base, model=model) for model in models]


def print_plan(
    cases: Sequence[Case], *, skipped: int, runs: Sequence[BedrockSettings], labels_path: Path
) -> None:
    first = runs[0]
    profile = f"AWS profile {first.profile}" if first.profile else "default AWS credential chain"
    models = ", ".join(settings.model for settings in runs)
    print(
        f"Labels: {labels_path}: {_count(len(cases), 'image')} to evaluate, "
        f"{_count(skipped, 'row')} skipped (no expected_amount)."
    )
    print(
        f"WARNING: this sends {_count(len(cases), 'receipt image')} to Amazon Bedrock in region "
        f"{first.region} ({profile}), once per model: {models} (effort {first.effort}). "
        f"That is {_count(len(cases) * len(runs), 'request')}. Receipts contain personal data."
    )


def evaluate(
    settings: BedrockSettings,
    cases: Sequence[Case],
    extractor_factory: ExtractorFactory,
    results_dir: Path,
    timestamp: str,
) -> Score:
    extractor = extractor_factory(settings)
    widths = (max(len(case.label.file) for case in cases), 10, 10, 5, 13, 10)
    print(f"\n== {settings.model} ==")
    print(_table_row(TABLE_COLUMNS, widths))
    outcomes = []
    for case in cases:
        outcome = Outcome(case.label, extractor.extract(case.image))
        outcomes.append(outcome)
        print(_table_row(_table_cells(outcome), widths), flush=True)
    score = Score.of(settings.model, outcomes)
    path = write_results(results_dir, settings.model, timestamp, outcomes)
    print(score.describe())
    print(f"results: {path}")
    return score


def write_results(
    results_dir: Path, model: str, timestamp: str, outcomes: Sequence[Outcome]
) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"results-{re.sub(r'[^A-Za-z0-9._-]', '_', model)}-{timestamp}.csv"
    with path.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(_result_row(outcome) for outcome in outcomes)
    return path


def _result_row(outcome: Outcome) -> dict[str, str]:
    label, extraction = outcome.label, outcome.extraction
    usage = extraction.usage
    return {
        "file": label.file,
        "sha256": label.sha256,
        "expected_amount": _amount(label.expected_amount_minor),
        "expected_currency": label.expected_currency,
        "got_amount": _amount(extraction.amount_minor),
        "got_currency": extraction.currency or "",
        "match": "yes" if outcome.matched else "no",
        "status": extraction.status.value,
        "confidence": extraction.confidence.value if extraction.confidence else "",
        "reference": extraction.reference or "",
        "paid_at": extraction.paid_at.isoformat() if extraction.paid_at else "",
        "provider": extraction.provider or "",
        "input_tokens": str(usage.input_tokens) if usage else "",
        "output_tokens": str(usage.output_tokens) if usage else "",
        "error": extraction.error or "",
        "raw_json": extraction.raw_json or "",
    }


def _table_cells(outcome: Outcome) -> tuple[str, ...]:
    extraction = outcome.extraction
    got = _amount(extraction.amount_minor) or "-"
    if extraction.currency and extraction.currency != outcome.label.expected_currency:
        got = f"{got} {extraction.currency}"
    cells = (
        outcome.label.file,
        _amount(outcome.label.expected_amount_minor),
        got,
        "yes" if outcome.matched else "NO",
        extraction.status.value,
        extraction.confidence.value if extraction.confidence else "-",
    )
    return (*cells, extraction.error) if extraction.error else cells


def _table_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = [cell.ljust(width) for cell, width in zip(cells, widths, strict=False)]
    return "  ".join([*padded, *cells[len(widths) :]]).rstrip()


def _amount(amount_minor: int | None) -> str:
    return "" if amount_minor is None else format_minor(amount_minor)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


if __name__ == "__main__":
    raise SystemExit(main())
