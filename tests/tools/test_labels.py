import csv
from pathlib import Path

import pytest

from payments_bot.tools.labels import (
    LABEL_COLUMNS,
    Label,
    LabelError,
    format_minor,
    parse_expected_amount,
    read_labels,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.mark.parametrize(
    ("text", "minor"),
    [("250", 25000), ("250.00", 25000), ("250,00", 25000), (" 40.5 ", 4050), ("0,05", 5)],
)
def test_expected_amount_accepts_what_a_human_types(text: str, minor: int) -> None:
    assert parse_expected_amount(text) == minor


@pytest.mark.parametrize("text", ["abc", "1.250,00", "1,250.00", "-5", "S/ 250", "40.555", ","])
def test_expected_amount_rejects_ambiguous_or_malformed_text(text: str) -> None:
    with pytest.raises(LabelError):
        parse_expected_amount(text)


@pytest.mark.parametrize(("minor", "text"), [(25000, "250.00"), (4050, "40.50"), (5, "0.05")])
def test_format_minor_shows_two_decimals(minor: int, text: str) -> None:
    assert format_minor(minor) == text


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_text(text, encoding=encoding)
    return path


def test_read_labels_parses_rows_and_leaves_unlabeled_amounts_empty(tmp_path: Path) -> None:
    csv_path = write(
        tmp_path / "labels.csv",
        ",".join(LABEL_COLUMNS) + f"\na.jpeg,{HASH_A},250,PEN,yape\nb.jpeg,{HASH_B},,,\n",
        encoding="utf-8-sig",
    )

    labels = read_labels(csv_path)

    assert labels == [
        Label(
            file="a.jpeg",
            sha256=HASH_A,
            expected_amount_minor=25000,
            expected_currency="PEN",
            notes="yape",
        ),
        Label(file="b.jpeg", sha256=HASH_B, expected_amount_minor=None, expected_currency="PEN"),
    ]


def test_read_labels_accepts_semicolon_separated_files_from_spreadsheets(tmp_path: Path) -> None:
    csv_path = write(
        tmp_path / "labels.csv",
        ";".join(LABEL_COLUMNS) + "\n" + f"a.jpeg;{HASH_A};250,00;PEN;\n",
    )

    (label,) = read_labels(csv_path)

    assert label.expected_amount_minor == 25000


def test_read_labels_names_the_line_of_a_bad_amount(tmp_path: Path) -> None:
    csv_path = write(
        tmp_path / "labels.csv",
        ",".join(LABEL_COLUMNS) + f'\na.jpeg,{HASH_A},250,PEN,\nb.jpeg,{HASH_B},"1.250,00",PEN,\n',
    )

    with pytest.raises(LabelError, match="line 3"):
        read_labels(csv_path)


def test_read_labels_rejects_a_bad_currency(tmp_path: Path) -> None:
    csv_path = write(
        tmp_path / "labels.csv", ",".join(LABEL_COLUMNS) + "\n" + f"a.jpeg,{HASH_A},250,soles,\n"
    )

    with pytest.raises(LabelError, match="line 2"):
        read_labels(csv_path)


@pytest.mark.parametrize(
    "name", ["../x.jpeg", "..\\x.jpeg", "sub/x.jpeg", "/tmp/x.jpeg", "..", "C:\\x.jpeg"]
)
def test_read_labels_refuses_a_file_that_is_not_a_bare_name(tmp_path: Path, name: str) -> None:
    csv_path = tmp_path / "labels.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([LABEL_COLUMNS, [name, HASH_A, "250", "PEN", ""]])

    with pytest.raises(LabelError, match="bare file name"):
        read_labels(csv_path)


def test_read_labels_refuses_a_file_listed_twice(tmp_path: Path) -> None:
    csv_path = write(
        tmp_path / "labels.csv",
        ",".join(LABEL_COLUMNS) + f"\na.jpeg,{HASH_A},250,PEN,\na.jpeg,{HASH_A},40,PEN,\n",
    )

    with pytest.raises(LabelError, match=r"line 3: .*a\.jpeg.* line 2"):
        read_labels(csv_path)


def test_read_labels_explains_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    csv_path = write(
        tmp_path / "labels.csv",
        ",".join(LABEL_COLUMNS) + f"\npagó.jpeg,{HASH_A},250,PEN,\n",
        encoding="cp1252",
    )

    with pytest.raises(LabelError, match="UTF-8"):
        read_labels(csv_path)


def test_read_labels_requires_the_template_columns(tmp_path: Path) -> None:
    csv_path = write(tmp_path / "labels.csv", "file,amount\na.jpeg,250\n")

    with pytest.raises(LabelError, match="sha256"):
        read_labels(csv_path)
