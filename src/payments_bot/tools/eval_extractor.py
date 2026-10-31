"""Score the receipt extractor against hand-labeled images, per model.

    uv run python -m payments_bot.tools.eval_extractor [--model ID ...] [--yes]

This SENDS every labeled receipt image to Amazon Bedrock, once per model. Without --yes it only
prints what it would send, including the endpoint and auth mode the SDK will use. Every check
that needs no network (labels, image hashes, settings, and creating every model's results file)
runs first, so a typo never costs a request. Results go to data/eval/ (git-ignored) one row at
a time, so a run that stops halfway keeps every answer already paid for. A formula character
that would start a spreadsheet cell, also where a ";" or a line break starts one, is written
after an apostrophe, and a character no UTF-8 file can hold is written as a backslash escape.
"""

import argparse
import csv
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import TextIO

from payments_bot.adapters.extraction.claude_bedrock import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    EFFORT_LEVELS,
    BedrockEndpoint,
    BedrockSettings,
    ExtractorConfigError,
    build_bedrock_extractor,
    resolve_endpoint,
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

DEFAULT_RESULTS_DIR = Path("data/eval")
"""Git-ignored, like the labels and the receipts the results describe."""
RESULT_NAME_ATTEMPTS = 5

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
    "paid_on",
    "paid_at",
    "provider",
    "input_tokens",
    "output_tokens",
    "error",
    "raw_json",
)
NUMBER_COLUMNS = frozenset({"expected_amount", "got_amount", "input_tokens", "output_tokens"})
"""Written as plain numbers; every other result column is text."""
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
"""A spreadsheet runs a cell that starts with one of these as a formula (CSV injection)."""
CELL_BREAKS = (";", "\r", "\n")
"""Where a spreadsheet may start a new cell inside one text cell of this comma-separated file:
Excel in locales whose list separator is ";" (Spanish ones among them) splits every line on
";", and a line break inside a quoted cell can end the row there."""
_FORMULA_CELL_START = re.compile(
    "(?:^|(?<=[{breaks}]))(?=[{starts}])".format(
        breaks=re.escape("".join(CELL_BREAKS)),
        starts=re.escape("".join(FORMULA_TRIGGERS) + '"'),
    )
)
"""Every place a cell may start (the text's start or right after a cell break) followed by a
formula character, or by a double quote: a cell that starts with one is read as quoted, so its
value starts after it."""
ENCODING_ERRORS = "backslashreplace"
"""In results files and the printed table, a character no UTF-8 text can hold (a lone
surrogate) is written as "\\ud800" instead of stopping a run whose requests are paid for."""
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
        allow_abbrev=False,
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
        default=DEFAULT_RESULTS_DIR,
        help="where to write results-<model>-<UTC time>-<random>.csv (default: %(default)s)",
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
    new_suffix: Callable[[], str] = lambda: secrets.token_hex(3),
) -> int:
    """`env` stands for the process environment, which the Bedrock client also reads itself."""
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
        endpoint = resolve_endpoint(runs[0], env)
        if args.yes:
            prepare_results_dir(args.results_dir)
    except (LabelError, SetupError, ExtractorConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_plan(
        cases,
        skipped=len(labels) - len(cases),
        runs=runs,
        endpoint=endpoint,
        labels_path=args.labels,
    )
    if not args.yes:
        print("Dry run: nothing was sent. Re-run with --yes to send the images.")
        return 0

    timestamp = clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        results = open_results_files(runs, args.results_dir, timestamp, new_suffix)
    except SetupError as exc:
        print(f"error: {exc}; nothing was sent", file=sys.stderr)
        return 2
    with ExitStack() as stack:
        for _, handle in results:
            stack.enter_context(handle)
        scores = [
            evaluate(settings, cases, extractor_factory, path, handle)
            for settings, (path, handle) in zip(runs, results, strict=True)
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


def prepare_results_dir(directory: Path) -> None:
    """Create the results directory before anything is sent, so a bad path costs nothing."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError(f"cannot use {directory} for results: {exc.strerror}") from None
    if not os.access(directory, os.W_OK):
        raise SetupError(f"cannot write results to {directory}")


def print_plan(
    cases: Sequence[Case],
    *,
    skipped: int,
    runs: Sequence[BedrockSettings],
    endpoint: BedrockEndpoint,
    labels_path: Path,
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
    print(f"Bedrock {endpoint.describe()}.")


def evaluate(
    settings: BedrockSettings,
    cases: Sequence[Case],
    extractor_factory: ExtractorFactory,
    path: Path,
    handle: TextIO,
) -> Score:
    """Score one model, writing and flushing each result row to `handle` as soon as it is
    known. The caller owns (and closes) `handle`."""
    extractor = extractor_factory(settings)
    status_width = max(len(status.value) for status in ExtractionStatus)
    widths = (max(len(case.label.file) for case in cases), 10, 10, 5, status_width, 10)
    print(f"\n== {settings.model} ==")
    print(f"results (written row by row): {path}")
    print(_table_row(TABLE_COLUMNS, widths))
    outcomes = []
    writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
    for case in cases:
        outcome = Outcome(case.label, extractor.extract(case.image))
        outcomes.append(outcome)
        writer.writerow(_result_row(outcome))
        handle.flush()
        print(_printable(_table_row(_table_cells(outcome), widths)), flush=True)
    score = Score.of(settings.model, outcomes)
    print(score.describe())
    return score


def open_results_files(
    runs: Sequence[BedrockSettings],
    directory: Path,
    timestamp: str,
    new_suffix: Callable[[], str],
) -> list[tuple[Path, TextIO]]:
    """Create every model's results file, with its header row, before anything is sent: a
    name the file system refuses must not surface after earlier models' requests were paid
    for. If one cannot be created, `SetupError` is raised; whatever stops the setup (an
    interrupt included), the files created here are closed and removed before it propagates."""
    created: list[tuple[Path, TextIO]] = []
    try:
        for settings in runs:
            stem = directory / _results_stem(settings.model, timestamp)
            path, handle = create_results_file(stem, new_suffix)
            created.append((path, handle))
            try:
                csv.DictWriter(handle, fieldnames=RESULT_COLUMNS).writeheader()
                handle.flush()
            except OSError as exc:
                raise SetupError(f"cannot write results file {path}: {exc.strerror}") from None
    except BaseException:
        for path, handle in created:
            handle.close()
            path.unlink(missing_ok=True)
        raise
    return created


def create_results_file(stem: Path, new_suffix: Callable[[], str]) -> tuple[Path, TextIO]:
    """Open a new `<stem>-<suffix>.csv` exclusively: never overwrites an earlier run's file,
    even when two runs start in the same second. Raises `SetupError` instead of `OSError`."""
    for _ in range(RESULT_NAME_ATTEMPTS):
        path = stem.with_name(f"{stem.name}-{new_suffix()}.csv")
        try:
            return path, path.open("x", encoding=CSV_ENCODING, errors=ENCODING_ERRORS, newline="")
        except FileExistsError:
            continue
        except OSError as exc:
            raise SetupError(f"cannot create results file {path}: {exc.strerror}") from None
    raise SetupError(f"no unused results file name for {stem} after {RESULT_NAME_ATTEMPTS} tries")


def _results_stem(model: str, timestamp: str) -> str:
    return f"results-{re.sub(r'[^A-Za-z0-9._-]', '_', model)}-{timestamp}"


def _result_row(outcome: Outcome) -> dict[str, str]:
    label, extraction = outcome.label, outcome.extraction
    usage = extraction.usage
    row = {
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
        "paid_on": extraction.paid_on.isoformat() if extraction.paid_on else "",
        "paid_at": extraction.paid_at.isoformat() if extraction.paid_at else "",
        "provider": extraction.provider or "",
        "input_tokens": str(usage.input_tokens) if usage else "",
        "output_tokens": str(usage.output_tokens) if usage else "",
        "error": extraction.error or "",
        "raw_json": extraction.raw_json or "",
    }
    return {
        column: value if column in NUMBER_COLUMNS else _text_cell(value)
        for column, value in row.items()
    }


def _text_cell(text: str) -> str:
    """`text` as a spreadsheet shows it, never runs it, whether the spreadsheet splits lines
    on "," or on ";". An apostrophe goes before every formula character (or double quote) that
    starts the text or follows a ";" or a line break: "Yape;=1+1" is written "Yape;'=1+1", so
    neither the whole cell nor the ";"-separated piece "'=1+1" is a formula. The image can
    steer the model's text, and the results file opens in spreadsheet apps."""
    return _FORMULA_CELL_START.sub("'", text)


def _printable(text: str) -> str:
    """`text` with any character no UTF-8 text can hold written as a backslash escape."""
    return text.encode("utf-8", ENCODING_ERRORS).decode("utf-8")


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
