"""Parsing, validation and normalization of raw coffee-health CSV records.

Everything here is pure (no I/O, no database) so it can be unit tested in isolation and
shared by the loader (to clean input) and the API (to normalize filter values the same way).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from functools import cache
from typing import Any


class Gender(StrEnum):
    MALE = "Male"
    FEMALE = "Female"
    OTHER = "Other"


class SleepQuality(StrEnum):
    POOR = "Poor"
    FAIR = "Fair"
    GOOD = "Good"
    EXCELLENT = "Excellent"


class StressLevel(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class HealthIssues(StrEnum):
    NONE = "None"
    MILD = "Mild"
    MODERATE = "Moderate"
    SEVERE = "Severe"


class Occupation(StrEnum):
    OFFICE = "Office"
    HEALTHCARE = "Healthcare"
    SERVICE = "Service"
    STUDENT = "Student"
    OTHER = "Other"


# CSV header -> record attribute, in the order the columns are stored in the database.
COLUMNS: Mapping[str, str] = {
    "ID": "id",
    "Age": "age",
    "Gender": "gender",
    "Country": "country",
    "Coffee_Intake": "coffee_intake",
    "Caffeine_mg": "caffeine_mg",
    "Sleep_Hours": "sleep_hours",
    "Sleep_Quality": "sleep_quality",
    "BMI": "bmi",
    "Heart_Rate": "heart_rate",
    "Stress_Level": "stress_level",
    "Physical_Activity_Hours": "physical_activity_hours",
    "Health_Issues": "health_issues",
    "Occupation": "occupation",
    "Smoking": "smoking",
    "Alcohol_Consumption": "alcohol_consumption",
}
EXPECTED_FIELDS = len(COLUMNS)


@dataclass(frozen=True, slots=True)
class Range:
    """Inclusive plausibility range for a numeric column."""

    min: Decimal
    max: Decimal


# Physically plausible limits. Intentionally generous: they reject impossible values
# (negative sleep, a 212-year-old, a resting heart rate of 350), not unusual ones.
# Keep in sync with the CHECK constraints in db/init/001_schema.sql.
LIMITS: Mapping[str, Range] = {
    "id": Range(Decimal(1), Decimal(2_147_483_647)),
    "age": Range(Decimal(18), Decimal(110)),  # dataset is adults only
    "coffee_intake": Range(Decimal(0), Decimal(20)),  # cups / day
    "caffeine_mg": Range(Decimal(0), Decimal(2000)),  # mg / day
    "sleep_hours": Range(Decimal(1), Decimal(18)),  # hours / night
    "bmi": Range(Decimal(10), Decimal(70)),
    "heart_rate": Range(Decimal(25), Decimal(220)),  # resting bpm
    "physical_activity_hours": Range(Decimal(0), Decimal(100)),  # hours / week
}

# Decimal columns are stored with one decimal place (the precision of the source data).
DECIMAL_QUANTUM = Decimal("0.1")

# Canonical spelling of the countries known to the source system. Matching is
# case-insensitive; unknown countries are accepted if they look like a name (see
# normalize_country), so a new country in a future export is not silently dropped.
KNOWN_COUNTRIES: tuple[str, ...] = (
    "Australia",
    "Belgium",
    "Brazil",
    "Canada",
    "China",
    "Finland",
    "France",
    "Germany",
    "India",
    "Italy",
    "Japan",
    "Mexico",
    "Netherlands",
    "Norway",
    "South Korea",
    "Spain",
    "Sweden",
    "Switzerland",
    "UK",
    "USA",
)
_COUNTRY_LOOKUP = {c.casefold(): c for c in KNOWN_COUNTRIES}
_COUNTRY_PATTERN = re.compile(r"[^\W\d_]+(?:[ '\-.][^\W\d_]+)*")
MAX_COUNTRY_LENGTH = 64

_INT_PATTERN = re.compile(r"[+-]?\d+")
_INT_WITH_ZERO_FRACTION = re.compile(r"[+-]?\d+\.0+")
_DECIMAL_PATTERN = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_WHITESPACE = re.compile(r"\s+")

_TRUE = frozenset({"1", "true", "t", "yes", "y"})
_FALSE = frozenset({"0", "false", "f", "no", "n"})


class FieldError(ValueError):
    """A single field could not be parsed or failed validation."""


@dataclass(frozen=True, slots=True)
class Record:
    id: int
    age: int
    gender: Gender
    country: str
    coffee_intake: Decimal
    caffeine_mg: Decimal
    sleep_hours: Decimal
    sleep_quality: SleepQuality
    bmi: Decimal
    heart_rate: int
    stress_level: StressLevel
    physical_activity_hours: Decimal
    health_issues: HealthIssues
    occupation: Occupation
    smoking: bool
    alcohol_consumption: bool

    def as_row(self) -> tuple[Any, ...]:
        """Values in COLUMNS order, ready for COPY (enums as their plain string value).

        Spelled out rather than built with getattr(): this runs once per loaded row.
        """
        return (
            self.id,
            self.age,
            str(self.gender),
            self.country,
            self.coffee_intake,
            self.caffeine_mg,
            self.sleep_hours,
            str(self.sleep_quality),
            self.bmi,
            self.heart_rate,
            str(self.stress_level),
            self.physical_activity_hours,
            str(self.health_issues),
            str(self.occupation),
            self.smoking,
            self.alcohol_consumption,
        )


@dataclass(slots=True)
class ParseResult:
    """Outcome of parsing one CSV row: a record, or the reasons it was rejected.

    `fixes` lists the fields that were valid but had to be normalized (trimmed,
    re-cased, boolean spelled differently, ...). A row can have fixes and still be valid.
    """

    record: Record | None = None
    errors: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.record is not None


# --------------------------------------------------------------------------- field parsers
# Each parser takes the raw string and returns (value, was_fixed) or raises FieldError.


def _strip(raw: str) -> str:
    """Trim the ends and collapse inner runs of whitespace ("South   Korea")."""
    text = raw.strip()
    if "  " in text or not text.isprintable():  # rare; skip the regex on the hot path
        text = _WHITESPACE.sub(" ", text)
    return text


def parse_int(raw: str) -> tuple[int, bool]:
    text = _strip(raw)
    if not text:
        raise FieldError("is empty")
    if _INT_PATTERN.fullmatch(text):
        return int(text), text != raw
    if _INT_WITH_ZERO_FRACTION.fullmatch(text):  # "40.0" is unambiguous
        return int(text.split(".")[0]), True
    raise FieldError(f"{raw!r} is not an integer")


def parse_decimal(raw: str) -> tuple[Decimal, bool]:
    text = _strip(raw)
    if not text:
        raise FieldError("is empty")
    if not _DECIMAL_PATTERN.fullmatch(text):
        raise FieldError(f"{raw!r} is not a number")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise FieldError(f"{raw!r} is not a number") from exc
    rounded = value.quantize(DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN)
    return rounded, text != raw or rounded != value


def parse_bool(raw: str) -> tuple[bool, bool]:
    text = _strip(raw).casefold()
    if text in _TRUE:
        return True, raw != "1"
    if text in _FALSE:
        return False, raw != "0"
    raise FieldError(f"{raw!r} is not a boolean (expected 0/1)")


@cache
def _enum_lookup[E: StrEnum](enum: type[E]) -> dict[str, E]:
    return {member.casefold(): member for member in enum}


def parse_enum[E: StrEnum](raw: str, enum: type[E]) -> tuple[E, bool]:
    member = _enum_lookup(enum).get(_strip(raw).casefold())
    if member is None:
        allowed = ", ".join(enum)
        raise FieldError(f"{raw!r} is not one of: {allowed}")
    return member, member != raw


def normalize_country(raw: str) -> str:
    """Canonical country spelling. Raises FieldError if it does not look like a name."""
    text = _strip(raw)
    if not text:
        raise FieldError("is empty")
    known = _COUNTRY_LOOKUP.get(text.casefold())
    if known is not None:
        return known
    if len(text) > MAX_COUNTRY_LENGTH or not _COUNTRY_PATTERN.fullmatch(text):
        raise FieldError(f"{raw!r} is not a valid country name")
    return text


def parse_country(raw: str) -> tuple[str, bool]:
    value = normalize_country(raw)
    return value, value != raw


_PARSERS: Mapping[str, Any] = {
    "id": parse_int,
    "age": parse_int,
    "gender": lambda raw: parse_enum(raw, Gender),
    "country": parse_country,
    "coffee_intake": parse_decimal,
    "caffeine_mg": parse_decimal,
    "sleep_hours": parse_decimal,
    "sleep_quality": lambda raw: parse_enum(raw, SleepQuality),
    "bmi": parse_decimal,
    "heart_rate": parse_int,
    "stress_level": lambda raw: parse_enum(raw, StressLevel),
    "physical_activity_hours": parse_decimal,
    "health_issues": lambda raw: parse_enum(raw, HealthIssues),
    "occupation": lambda raw: parse_enum(raw, Occupation),
    "smoking": parse_bool,
    "alcohol_consumption": parse_bool,
}

# (CSV column, attribute, parser, plausibility range or None), resolved once.
_FIELD_SPECS = tuple(
    (column, attr, _PARSERS[attr], LIMITS.get(attr)) for column, attr in COLUMNS.items()
)


# --------------------------------------------------------------------------- row parsing


def validate_header(header: Sequence[str]) -> None:
    """Raise ValueError unless the header contains exactly the expected columns, in order."""
    cleaned = [_strip(h).lstrip("\ufeff") for h in header]
    expected = list(COLUMNS)
    if [h.casefold() for h in cleaned] != [h.casefold() for h in expected]:
        raise ValueError(f"unexpected CSV header {cleaned!r}; expected {expected!r}")


def parse_row(fields: Sequence[str]) -> ParseResult:
    """Parse, validate and normalize one CSV row (header excluded).

    All field errors are collected, not just the first, so a rejected row's report
    explains everything that is wrong with it.
    """
    result = ParseResult()
    if len(fields) != EXPECTED_FIELDS:
        result.errors.append(f"expected {EXPECTED_FIELDS} fields, got {len(fields)}")
        return result

    values: dict[str, Any] = {}
    for (column, attr, parser, limits), raw in zip(_FIELD_SPECS, fields, strict=True):
        try:
            value, fixed = parser(raw)
            if limits is not None and not limits.min <= value <= limits.max:
                raise FieldError(
                    f"{value} is outside the plausible range [{limits.min}, {limits.max}]"
                )
        except FieldError as exc:
            result.errors.append(f"{column}: {exc}")
            continue
        values[attr] = value
        if fixed:
            result.fixes.append(column)

    if not result.errors:
        result.record = Record(**values)
    return result
