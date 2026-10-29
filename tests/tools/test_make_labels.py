import csv
import hashlib
from pathlib import Path

import pytest

from payments_bot.tools.labels import LABEL_COLUMNS
from payments_bot.tools.make_labels import main


@pytest.fixture
def images(tmp_path: Path) -> Path:
    directory = tmp_path / "pagos"
    directory.mkdir()
    (directory / "b receipt.jpeg").write_bytes(b"\xff\xd8\xff second")
    (directory / "a.PNG").write_bytes(b"\x89PNG\r\n\x1a\n first")
    (directory / "notes.txt").write_text("not an image")
    (directory / ".hidden.jpg").write_bytes(b"\xff\xd8\xff hidden")
    (directory / "nested").mkdir()
    (directory / "nested" / "c.jpg").write_bytes(b"\xff\xd8\xff nested")
    return directory


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_template_lists_each_image_with_its_hash_and_empty_expectations(
    images: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "data" / "eval" / "labels.csv"

    exit_code = main(["--images", str(images), "--output", str(output)])

    assert exit_code == 0
    with output.open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.reader(handle)) == list(LABEL_COLUMNS)
    assert read_rows(output) == [
        {
            "file": "a.PNG",
            "sha256": hashlib.sha256(b"\x89PNG\r\n\x1a\n first").hexdigest(),
            "expected_amount": "",
            "expected_currency": "PEN",
            "notes": "",
        },
        {
            "file": "b receipt.jpeg",
            "sha256": hashlib.sha256(b"\xff\xd8\xff second").hexdigest(),
            "expected_amount": "",
            "expected_currency": "PEN",
            "notes": "",
        },
    ]
    assert "2 images" in capsys.readouterr().out


def test_existing_labels_are_never_overwritten_without_force(
    images: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "labels.csv"
    output.write_text("my typed labels\n")

    exit_code = main(["--images", str(images), "--output", str(output)])

    assert exit_code == 1
    assert output.read_text() == "my typed labels\n"
    assert "--force" in capsys.readouterr().err


def test_force_overwrites_existing_labels(images: Path, tmp_path: Path) -> None:
    output = tmp_path / "labels.csv"
    output.write_text("stale\n")

    exit_code = main(["--images", str(images), "--output", str(output), "--force"])

    assert exit_code == 0
    assert len(read_rows(output)) == 2


def test_missing_images_directory_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "labels.csv"

    exit_code = main(["--images", str(tmp_path / "nope"), "--output", str(output)])

    assert exit_code == 1
    assert not output.exists()
    assert "nope" in capsys.readouterr().err


def test_help_describes_the_tool(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "--force" in capsys.readouterr().out
