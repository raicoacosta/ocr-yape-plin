import pytest

from payments_bot.domain.errors import InvalidAmountError, InvalidCurrencyError
from payments_bot.domain.money import DEFAULT_CURRENCY, parse_amount, validate_currency


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("250", 25000),
        ("40.00", 4000),
        ("40.5", 4050),
        ("1234.56", 123456),
        ("0.01", 1),
        ("0", 0),
    ],
)
def test_parse_amount_converts_canonical_decimals_to_minor_units(text: str, expected: int) -> None:
    amount = parse_amount(text)

    assert amount == expected
    assert type(amount) is int


@pytest.mark.parametrize(
    "text",
    ["", "abc", "-5", "+5", "40.555", "1,234.56", "40.", ".5", "S/ 40", "1e3", "٤٠"],
)
def test_parse_amount_rejects_non_canonical_text(text: str) -> None:
    with pytest.raises(InvalidAmountError):
        parse_amount(text)


def test_default_currency_is_peruvian_sol() -> None:
    assert DEFAULT_CURRENCY == "PEN"


@pytest.mark.parametrize("code", ["PEN", "USD"])
def test_validate_currency_accepts_iso_4217_codes(code: str) -> None:
    validate_currency(code)


@pytest.mark.parametrize("code", ["", "pen", "PE", "PENN", "P3N"])
def test_validate_currency_rejects_malformed_codes(code: str) -> None:
    with pytest.raises(InvalidCurrencyError):
        validate_currency(code)
