"""The hand-labeled CSV that the extractor evaluation is scored against.

One row per receipt image: its file name, its SHA-256 (so a swapped or edited image is caught
before it is scored) and the amount a person read on it.
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from payments_bot.domain.errors import InvalidAmountError, InvalidCurrencyError
from payments_bot.domain.money import (
    DEFAULT_CURRENCY,
    MINOR_UNITS_PER_MAJOR,
    parse_amount,
    validate_currency,
)

LABEL_COLUMNS = ("file", "sha256", "expected_amount", "expected_currency", "notes")
REQUIRED_COLUMNS = LABEL_COLUMNS[:4]
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
DEFAULT_IMAGES_DIR = Path("pagos")
DEFAULT_LABELS_PATH = Path("data/eval/labels.csv")
CSV_ENCODING = "utf-8-sig"
"""UTF-8 with a byte-order mark, so spreadsheet apps keep non-ASCII file names intact."""

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class LabelError(ValueError):
    """The labels file is missing or malformed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Label:
    file: str
    sha256: str
    expected_amount_minor: int | None
    """None until a person fills in the amount; such rows are not evaluated."""
    expected_currency: str = DEFAULT_CURRENCY
    notes: str = ""


def list_images(directory: Path) -> list[Path]:
    """Receipt images directly inside `directory`, sorted by name; hidden files are skipped."""
    images = (
        path
        for path in directory.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in IMAGE_SUFFIXES
    )
    return sorted(images, key=lambda path: path.name)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_expected_amount(text: str) -> int:
    """Minor units from an amount typed by a person: "250", "250.00" or "250,00"."""
    cleaned = text.strip()
    if cleaned.count(",") == 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return parse_amount(cleaned)
    except InvalidAmountError:
        raise LabelError(
            f"expected_amount {text!r} is not an amount like 250, 250.00 or 250,00"
        ) from None


def format_minor(amount_minor: int) -> str:
    """Minor units as a decimal with two places: 25000 -> "250.00"."""
    sign = "-" if amount_minor < 0 else ""
    major, minor = divmod(abs(amount_minor), MINOR_UNITS_PER_MAJOR)
    return f"{sign}{major}.{minor:02d}"


def read_labels(path: Path) -> list[Label]:
    """Read a labels file written by `make_labels`, also after a spreadsheet re-saved it."""
    if not path.is_file():
        raise LabelError(
            f"{path} not found; create it with python -m payments_bot.tools.make_labels"
        )
    text = path.read_text(encoding=CSV_ENCODING)
    header = text.partition("\n")[0]
    delimiter = ";" if header.count(";") > header.count(",") else ","
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
    if missing:
        raise LabelError(f"{path} lacks the column(s) {', '.join(missing)}")
    labels = []
    for row in reader:
        cells = {column: (row.get(column) or "").strip() for column in LABEL_COLUMNS}
        if not any(cells.values()):
            continue
        try:
            labels.append(_label(cells))
        except LabelError as exc:
            raise LabelError(f"{path}: line {reader.line_num}: {exc}") from None
    return labels


def _label(cells: dict[str, str]) -> Label:
    if not cells["file"] or Path(cells["file"]).name != cells["file"]:
        raise LabelError(f"file {cells['file']!r} must be a bare file name")
    sha256 = cells["sha256"].lower()
    if not _SHA256_HEX.fullmatch(sha256):
        raise LabelError(f"sha256 {cells['sha256']!r} is not a SHA-256 hex digest")
    amount = cells["expected_amount"]
    currency = cells["expected_currency"].upper() or DEFAULT_CURRENCY
    try:
        validate_currency(currency)
    except InvalidCurrencyError:
        raise LabelError(f"expected_currency {currency!r} is not an ISO 4217 code") from None
    return Label(
        file=cells["file"],
        sha256=sha256,
        expected_amount_minor=parse_expected_amount(amount) if amount else None,
        expected_currency=currency,
        notes=cells["notes"],
    )
