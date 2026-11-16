"""Unit tests for parsing, validation and normalization. No database needed."""

from decimal import Decimal

import pytest

from app.cleaning import (
    FieldError,
    Gender,
    Occupation,
    SleepQuality,
    StressLevel,
    normalize_country,
    parse_bool,
    parse_decimal,
    parse_enum,
    parse_int,
    parse_row,
    validate_header,
)

HEADER = [
    "ID",
    "Age",
    "Gender",
    "Country",
    "Coffee_Intake",
    "Caffeine_mg",
    "Sleep_Hours",
    "Sleep_Quality",
    "BMI",
    "Heart_Rate",
    "Stress_Level",
    "Physical_Activity_Hours",
    "Health_Issues",
    "Occupation",
    "Smoking",
    "Alcohol_Consumption",
]
VALID = [
    "1",
    "40",
    "Male",
    "Germany",
    "3.5",
    "328.1",
    "7.5",
    "Good",
    "24.9",
    "78",
    "Low",
    "14.5",
    "None",
    "Other",
    "0",
    "0",
]


def row_with(**overrides: str) -> list[str]:
    """VALID with some columns replaced, by CSV header name."""
    fields = list(VALID)
    for column, value in overrides.items():
        fields[HEADER.index(column)] = value
    return fields


# --------------------------------------------------------------------------- field parsers


@pytest.mark.parametrize(
    ("raw", "expected", "fixed"),
    [("40", 40, False), (" 40 ", 40, True), ("40.0", 40, True), ("-3", -3, False)],
)
def test_parse_int(raw: str, expected: int, fixed: bool) -> None:
    assert parse_int(raw) == (expected, fixed)


@pytest.mark.parametrize("raw", ["", "  ", "thirty", "4O", "unknown", "40.5", "1e3"])
def test_parse_int_rejects(raw: str) -> None:
    with pytest.raises(FieldError):
        parse_int(raw)


@pytest.mark.parametrize(
    ("raw", "expected", "fixed"),
    [
        ("7.5", Decimal("7.5"), False),
        ("7", Decimal("7.0"), False),
        ("\t7.5 ", Decimal("7.5"), True),
        ("7.25", Decimal("7.2"), True),  # rounded half-even to one decimal
        (".5", Decimal("0.5"), False),
    ],
)
def test_parse_decimal(raw: str, expected: Decimal, fixed: bool) -> None:
    assert parse_decimal(raw) == (expected, fixed)


@pytest.mark.parametrize("raw", ["", "seven", "n/a", "NaN", "inf", "1,5", "7.5.1"])
def test_parse_decimal_rejects(raw: str) -> None:
    with pytest.raises(FieldError):
        parse_decimal(raw)


@pytest.mark.parametrize(
    ("raw", "expected", "fixed"),
    [
        ("0", False, False),
        ("1", True, False),
        ("Y", True, True),
        ("yes", True, True),
        ("no", False, True),
        ("false", False, True),
        (" 1 ", True, True),
    ],
)
def test_parse_bool(raw: str, expected: bool, fixed: bool) -> None:
    assert parse_bool(raw) == (expected, fixed)


@pytest.mark.parametrize("raw", ["-1", "2", "", "maybe"])
def test_parse_bool_rejects(raw: str) -> None:
    with pytest.raises(FieldError):
        parse_bool(raw)


@pytest.mark.parametrize(
    ("raw", "enum", "expected", "fixed"),
    [
        ("Male", Gender, Gender.MALE, False),
        ("MALE", Gender, Gender.MALE, True),
        ("\tMale  ", Gender, Gender.MALE, True),
        ("  Female  ", Gender, Gender.FEMALE, True),
        ("GOOD", SleepQuality, SleepQuality.GOOD, True),
        ("medium", StressLevel, StressLevel.MEDIUM, True),
        ("\tHealthcare  ", Occupation, Occupation.HEALTHCARE, True),
    ],
)
def test_parse_enum(raw: str, enum: type[Gender], expected: Gender, fixed: bool) -> None:
    assert parse_enum(raw, enum) == (expected, fixed)


@pytest.mark.parametrize("raw", ["", "?", "N/A", "Robot"])
def test_parse_enum_rejects(raw: str) -> None:
    with pytest.raises(FieldError):
        parse_enum(raw, Gender)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Germany", "Germany"),
        ("BELGIUM", "Belgium"),
        ("  South   korea  ", "South Korea"),
        ("\tUSA ", "USA"),
        ("usa", "USA"),
        ("New Zealand", "New Zealand"),  # unknown but name-like: kept, trimmed
        ("Côte d'Ivoire", "Côte d'Ivoire"),
    ],
)
def test_normalize_country(raw: str, expected: str) -> None:
    assert normalize_country(raw) == expected


@pytest.mark.parametrize("raw", ["", "123", "N/A", "Germany!", "x" * 65])
def test_normalize_country_rejects(raw: str) -> None:
    with pytest.raises(FieldError):
        normalize_country(raw)


# --------------------------------------------------------------------------- whole rows


def test_valid_row_needs_no_fixes() -> None:
    result = parse_row(VALID)
    assert result.ok
    assert result.errors == []
    assert result.fixes == []
    assert result.record is not None
    assert result.record.as_row() == (
        1, 40, "Male", "Germany", Decimal("3.5"), Decimal("328.1"), Decimal("7.5"), "Good",
        Decimal("24.9"), 78, "Low", Decimal("14.5"), "None", "Other", False, False,
    )  # fmt: skip


def test_fixable_row_is_normalized_and_reports_fixes() -> None:
    result = parse_row(row_with(Gender="\tMALE ", Country="belgium", Smoking="no"))
    assert result.record is not None
    assert result.record.gender is Gender.MALE
    assert result.record.country == "Belgium"
    assert result.record.smoking is False
    assert result.fixes == ["Gender", "Country", "Smoking"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"Age": "17"}, "Age: 17 is outside the plausible range [18, 110]"),
        ({"Age": "212"}, "Age: 212 is outside"),
        ({"Sleep_Hours": "-1"}, "Sleep_Hours: -1.0 is outside"),
        ({"Sleep_Hours": "25.0"}, "Sleep_Hours: 25.0 is outside"),
        ({"BMI": "0.0"}, "BMI: 0.0 is outside"),
        ({"Heart_Rate": "350"}, "Heart_Rate: 350 is outside"),
        ({"Heart_Rate": "0"}, "Heart_Rate: 0 is outside"),
        ({"Coffee_Intake": "-0.5"}, "Coffee_Intake: -0.5 is outside"),
        ({"ID": "0"}, "ID: 0 is outside"),
        ({"Age": "thirty"}, "Age: 'thirty' is not an integer"),
        ({"Gender": "N/A"}, "Gender: 'N/A' is not one of"),
        ({"Smoking": "2"}, "Smoking: '2' is not a boolean"),
    ],
)
def test_invalid_row_is_rejected_with_reason(overrides: dict[str, str], message: str) -> None:
    result = parse_row(row_with(**overrides))
    assert not result.ok
    assert len(result.errors) == 1
    assert result.errors[0].startswith(message)


def test_all_errors_of_a_row_are_reported() -> None:
    result = parse_row(row_with(Age="", Gender="?", BMI="n/a"))
    assert not result.ok
    assert [e.split(":")[0] for e in result.errors] == ["Age", "Gender", "BMI"]


@pytest.mark.parametrize("length", [13, 15, 17])
def test_wrong_field_count_is_rejected(length: int) -> None:
    fields = (VALID + ["extra", "extra"])[:length]
    result = parse_row(fields)
    assert result.errors == [f"expected 16 fields, got {length}"]


def test_header_validation() -> None:
    validate_header(["\ufeffID", *HEADER[1:]])  # BOM and exact names are fine
    validate_header([h.lower() for h in HEADER])
    with pytest.raises(ValueError, match="unexpected CSV header"):
        validate_header(HEADER[:-1])
    with pytest.raises(ValueError, match="unexpected CSV header"):
        validate_header([HEADER[1], HEADER[0], *HEADER[2:]])
