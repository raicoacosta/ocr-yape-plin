import csv
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from payments_bot.adapters.extraction.claude_bedrock import BedrockSettings
from payments_bot.ports.amount_extractor import Extraction, ExtractionStatus, TokenUsage
from payments_bot.tools.eval_extractor import main
from payments_bot.tools.labels import LABEL_COLUMNS

OPUS = "anthropic.claude-opus-5-5"
HAIKU = "anthropic.claude-haiku-5-5"
ENV = {"AWS_REGION": "us-east-1"}
FIXED_NOW = datetime(2026, 10, 29, 15, 0, 0, tzinfo=UTC)

IMAGES = {
    "a.jpeg": b"\xff\xd8\xff receipt a",
    "b.jpeg": b"\xff\xd8\xff receipt b",
    "c.jpeg": b"\xff\xd8\xff receipt c",
    "d.jpeg": b"\xff\xd8\xff receipt d",
}
EXPECTED = {"a.jpeg": "250", "b.jpeg": "40,00", "c.jpeg": "100.00", "d.jpeg": ""}

type Answer = tuple[ExtractionStatus, int | None, str | None]

CORRECT_A: Answer = (ExtractionStatus.OK, 25000, "PEN")
CORRECT_B: Answer = (ExtractionStatus.OK, 4000, "PEN")
WRONG_C: Answer = (ExtractionStatus.OK, 1000, "PEN")


@dataclass
class FakeExtractor:
    model: str
    answers: dict[bytes, Answer]
    seen: list[bytes] = field(default_factory=list)

    def extract(self, image: bytes) -> Extraction:
        self.seen.append(image)
        status, amount_minor, currency = self.answers[image]
        return Extraction(
            status=status,
            model=self.model,
            amount_minor=amount_minor,
            currency=currency,
            usage=TokenUsage(input_tokens=1000, output_tokens=50),
        )


@dataclass
class FakeFactory:
    answers: dict[bytes, Answer]
    settings: list[BedrockSettings] = field(default_factory=list)
    extractors: list[FakeExtractor] = field(default_factory=list)

    def __call__(self, settings: BedrockSettings) -> FakeExtractor:
        self.settings.append(settings)
        extractor = FakeExtractor(settings.model, self.answers)
        self.extractors.append(extractor)
        return extractor


def answers(a: Answer = CORRECT_A, b: Answer = CORRECT_B, c: Answer = WRONG_C) -> FakeFactory:
    return FakeFactory({IMAGES["a.jpeg"]: a, IMAGES["b.jpeg"]: b, IMAGES["c.jpeg"]: c})


@dataclass(frozen=True)
class Workspace:
    images: Path
    labels: Path
    results: Path

    def args(self, *extra: str) -> list[str]:
        return [
            "--labels",
            str(self.labels),
            "--images",
            str(self.images),
            "--results-dir",
            str(self.results),
            *extra,
        ]

    def result_files(self) -> list[Path]:
        return sorted(self.results.glob("results-*.csv")) if self.results.exists() else []


def write_labels(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(LABEL_COLUMNS)
        writer.writerows(rows)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    images = tmp_path / "pagos"
    images.mkdir()
    rows = []
    for name, data in IMAGES.items():
        (images / name).write_bytes(data)
        rows.append([name, hashlib.sha256(data).hexdigest(), EXPECTED[name], "PEN", ""])
    labels = tmp_path / "labels.csv"
    write_labels(labels, rows)
    return Workspace(images=images, labels=labels, results=tmp_path / "results")


def run(
    workspace: Workspace,
    *extra: str,
    factory: FakeFactory | None = None,
    env: dict[str, str] = ENV,
) -> tuple[int, FakeFactory]:
    factory = factory or answers()
    exit_code = main(
        workspace.args(*extra), env=env, extractor_factory=factory, clock=lambda: FIXED_NOW
    )
    return exit_code, factory


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# --- the --yes gate -------------------------------------------------------------------


def test_without_yes_it_prints_the_plan_and_sends_nothing(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, factory = run(workspace)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "WARNING" in out
    assert "Amazon Bedrock" in out
    assert "us-east-1" in out
    assert OPUS in out
    assert "3 images" in out
    assert "--yes" in out
    assert factory.settings == []
    assert workspace.result_files() == []


def test_warning_comes_before_the_first_call(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    printed_before_call: list[str] = []
    factory = answers()

    def spying_factory(settings: BedrockSettings) -> FakeExtractor:
        printed_before_call.append(capsys.readouterr().out)
        return factory(settings)

    main(
        workspace.args("--yes"),
        env=ENV,
        extractor_factory=spying_factory,
        clock=lambda: FIXED_NOW,
    )

    assert "WARNING" in printed_before_call[0]
    assert "Amazon Bedrock" in printed_before_call[0]


# --- evaluation -----------------------------------------------------------------------


def test_reports_rows_accuracy_and_tokens_for_labeled_images_only(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, factory = run(workspace, "--yes")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 row skipped (no expected_amount)" in out
    assert "2/3" in out
    assert "66.7%" in out
    assert "input 3000" in out
    assert "output 150" in out
    for name in ("a.jpeg", "b.jpeg", "c.jpeg"):
        assert name in out
    assert "d.jpeg" not in out
    (extractor,) = factory.extractors
    assert extractor.seen == [IMAGES["a.jpeg"], IMAGES["b.jpeg"], IMAGES["c.jpeg"]]


def test_results_csv_records_every_evaluated_image(workspace: Workspace) -> None:
    run(workspace, "--yes")

    (results,) = workspace.result_files()
    assert results.name == f"results-{OPUS}-20261029T150000Z.csv"
    rows = read_rows(results)
    assert [row["file"] for row in rows] == ["a.jpeg", "b.jpeg", "c.jpeg"]
    assert [row["expected_amount"] for row in rows] == ["250.00", "40.00", "100.00"]
    assert [row["got_amount"] for row in rows] == ["250.00", "40.00", "10.00"]
    assert [row["match"] for row in rows] == ["yes", "yes", "no"]
    assert [row["status"] for row in rows] == ["ok", "ok", "ok"]
    assert [row["input_tokens"] for row in rows] == ["1000", "1000", "1000"]


def test_results_csv_keeps_a_date_only_receipt_apart_from_a_timed_one(
    workspace: Workspace,
) -> None:
    lima = ZoneInfo("America/Lima")
    dated = {
        IMAGES["a.jpeg"]: (date(2026, 1, 15), datetime(2026, 1, 15, 9, 30, tzinfo=lima)),
        IMAGES["b.jpeg"]: (date(2026, 1, 16), None),
        IMAGES["c.jpeg"]: (None, None),
    }

    @dataclass
    class DatedExtractor:
        model: str

        def extract(self, image: bytes) -> Extraction:
            paid_on, paid_at = dated[image]
            return Extraction(
                status=ExtractionStatus.NEEDS_CONFIRMATION,
                model=self.model,
                amount_minor=25000,
                currency="PEN",
                paid_on=paid_on,
                paid_at=paid_at,
            )

    main(
        workspace.args("--yes"),
        env=ENV,
        extractor_factory=lambda settings: DatedExtractor(settings.model),
        clock=lambda: FIXED_NOW,
    )

    (results,) = workspace.result_files()
    rows = read_rows(results)
    assert [row["paid_on"] for row in rows] == ["2026-01-15", "2026-01-16", ""]
    assert [row["paid_at"] for row in rows] == ["2026-01-15T09:30:00-05:00", "", ""]
    assert [row["status"] for row in rows] == ["needs_confirmation"] * 3
    assert [row["match"] for row in rows] == ["no"] * 3


def test_several_models_are_evaluated_side_by_side(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, factory = run(workspace, "--yes", "--model", OPUS, "--model", HAIKU)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert [settings.model for settings in factory.settings] == [OPUS, HAIKU]
    assert [path.name for path in workspace.result_files()] == [
        f"results-{HAIKU}-20261029T150000Z.csv",
        f"results-{OPUS}-20261029T150000Z.csv",
    ]
    assert out.count("2/3 (66.7%)") >= 2


def test_non_ok_extraction_never_matches(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    run(workspace, "--yes", factory=answers(a=(ExtractionStatus.UNREADABLE, None, None)))

    out = capsys.readouterr().out
    assert "1/3" in out
    assert "unreadable" in out


def test_currency_must_match_as_well(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    run(workspace, "--yes", factory=answers(a=(ExtractionStatus.OK, 25000, "USD")))

    assert "1/3" in capsys.readouterr().out


def test_region_profile_and_effort_flags_override_the_environment(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    _, factory = run(
        workspace, "--yes", "--region", "sa-east-1", "--profile", "payments", "--effort", "high"
    )

    assert factory.settings[0] == BedrockSettings(
        region="sa-east-1", model=OPUS, effort="high", profile="payments"
    )
    out = capsys.readouterr().out
    assert "sa-east-1" in out
    assert "payments" in out


# --- refusing to start ----------------------------------------------------------------


def test_missing_region_stops_before_any_call(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, factory = run(workspace, "--yes", env={})

    assert exit_code == 2
    assert "AWS_REGION" in capsys.readouterr().err
    assert factory.settings == []


def test_changed_image_stops_before_any_call(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace.images / "b.jpeg").write_bytes(b"\xff\xd8\xff replaced")

    exit_code, factory = run(workspace, "--yes")

    assert exit_code == 2
    assert "b.jpeg" in capsys.readouterr().err
    assert factory.settings == []


def test_missing_image_stops_before_any_call(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace.images / "c.jpeg").unlink()

    exit_code, factory = run(workspace, "--yes")

    assert exit_code == 2
    assert "c.jpeg" in capsys.readouterr().err
    assert factory.settings == []


def test_malformed_expected_amount_stops_before_any_call(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_labels(workspace.labels, [["a.jpeg", "a" * 64, "doscientos", "PEN", ""]])

    exit_code, factory = run(workspace, "--yes")

    assert exit_code == 2
    assert "line 2" in capsys.readouterr().err
    assert factory.settings == []


def test_nothing_labeled_yet_is_reported(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_labels(workspace.labels, [["d.jpeg", "d" * 64, "", "PEN", ""]])

    exit_code, factory = run(workspace, "--yes")

    assert exit_code == 1
    assert "expected_amount" in capsys.readouterr().err
    assert factory.settings == []


def test_help_mentions_bedrock_and_the_yes_gate(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    out = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "--yes" in out
    assert "Bedrock" in out
