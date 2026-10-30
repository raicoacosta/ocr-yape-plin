"""Write the labels template that the extractor evaluation is scored against.

    uv run python -m payments_bot.tools.make_labels [--images pagos] [--output data/eval/labels.csv]

Every image gets a row with its SHA-256, an empty expected_amount and PEN as currency. A person
then types the amount each receipt really shows. Nothing is sent anywhere.
"""

import argparse
import csv
import sys
from collections.abc import Sequence
from pathlib import Path

from payments_bot.domain.money import DEFAULT_CURRENCY
from payments_bot.tools.labels import (
    CSV_ENCODING,
    DEFAULT_IMAGES_DIR,
    DEFAULT_LABELS_PATH,
    LABEL_COLUMNS,
    list_images,
    sha256_hex,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m payments_bot.tools.make_labels",
        allow_abbrev=False,
        description=(
            "Write a labels CSV template (file, sha256, expected_amount, expected_currency, "
            "notes) listing every receipt image, for a person to fill in. Local only."
        ),
    )
    parser.add_argument(
        "--images",
        type=Path,
        default=DEFAULT_IMAGES_DIR,
        help="directory with the receipt images (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_LABELS_PATH,
        help="labels CSV to create (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing labels file; amounts typed into it are lost",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.images.is_dir():
        print(f"error: images directory not found: {args.images}", file=sys.stderr)
        return 1
    images = list_images(args.images)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("w" if args.force else "x", encoding=CSV_ENCODING, newline="") as out:
            writer = csv.writer(out)
            writer.writerow(LABEL_COLUMNS)
            for image in images:
                writer.writerow(
                    [image.name, sha256_hex(image.read_bytes()), "", DEFAULT_CURRENCY, ""]
                )
    except FileExistsError:
        print(
            f"error: {args.output} already exists; pass --force to overwrite it "
            "(amounts typed into it would be lost)",
            file=sys.stderr,
        )
        return 1
    print(f"Wrote {len(images)} images to {args.output}. Fill in expected_amount for each row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
