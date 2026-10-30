import pytest

from payments_bot.domain.errors import InvalidAmountError, InvalidCurrencyError
from payments_bot.domain.money import (
    DEFAULT_CURRENCY,
    MAX_AMOUNT_INTEGER_DIGITS,
    MAX_AMOUNT_MINOR,
    parse_amount,
    validate_currency,
)


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


def test_parse_amount_is_exact_up_to_its_digit_cap() -> None:
    text = "9" * MAX_AMOUNT_INTEGER_DIGITS + ".99"

    assert parse_amount(text) == int("9" * MAX_AMOUNT_INTEGER_DIGITS) * 100 + 99


@pytest.mark.parametrize(
    "text",
    ["1" * (MAX_AMOUNT_INTEGER_DIGITS + 1), "1" * 27, "1" * 27 + ".00", "9" * 5000],
    ids=["one-over-the-cap", "27-digits", "27-digits-with-decimals", "5000-digits"],
)
def test_parse_amount_rejects_over_long_input_instead_of_rounding_it(text: str) -> None:
    with pytest.raises(InvalidAmountError):
        parse_amount(text)


def test_digit_cap_still_admits_the_largest_ledger_amount() -> None:
    assert parse_amount(str(MAX_AMOUNT_MINOR // 100)) == MAX_AMOUNT_MINOR


def test_default_currency_is_peruvian_sol() -> None:
    assert DEFAULT_CURRENCY == "PEN"


def test_amounts_are_capped_at_one_billion_major_units() -> None:
    assert MAX_AMOUNT_MINOR == 1_000_000_000_00


@pytest.mark.parametrize("code", ["PEN", "USD"])
def test_validate_currency_accepts_iso_4217_codes(code: str) -> None:
    validate_currency(code)


@pytest.mark.parametrize("code", ["", "pen", "PE", "PENN", "P3N"])
def test_validate_currency_rejects_malformed_codes(code: str) -> None:
    with pytest.raises(InvalidCurrencyError):
        validate_currency(code)
