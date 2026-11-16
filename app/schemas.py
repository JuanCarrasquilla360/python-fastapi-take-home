"""Pydantic models for the participants API: query parameters and responses."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.cleaning import (
    FieldError,
    Gender,
    HealthIssues,
    Occupation,
    SleepQuality,
    StressLevel,
    normalize_country,
)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

# (min field, max field) pairs validated together: min must not exceed max.
RANGE_PAIRS: tuple[tuple[str, str], ...] = (
    ("min_age", "max_age"),
    ("min_coffee_intake", "max_coffee_intake"),
    ("min_bmi", "max_bmi"),
    ("min_sleep_hours", "max_sleep_hours"),
)

Age = Annotated[int | None, Field(ge=0, le=150)]
Cups = Annotated[float | None, Field(ge=0, le=100)]
Bmi = Annotated[float | None, Field(ge=0, le=200)]
Hours = Annotated[float | None, Field(ge=0, le=24)]


class ParticipantFilters(BaseModel):
    """Filters shared by the list and summary endpoints.

    Multi-valued filters match any of the given values (`?country=Spain&country=Italy`);
    different filters are combined with AND. Ranges are inclusive. Unknown query
    parameters are rejected so a typo (`min_ag=30`) is a 422, not a silently ignored filter.
    """

    model_config = ConfigDict(extra="forbid")

    country: list[str] = Field(
        default_factory=list, description="Case-insensitive country name(s), e.g. `South Korea`"
    )
    gender: list[Gender] = Field(default_factory=list)
    sleep_quality: list[SleepQuality] = Field(default_factory=list)
    stress_level: list[StressLevel] = Field(default_factory=list)
    health_issues: list[HealthIssues] = Field(default_factory=list)
    occupation: list[Occupation] = Field(default_factory=list)
    smoking: bool | None = None
    alcohol_consumption: bool | None = None
    min_age: Age = None
    max_age: Age = None
    min_coffee_intake: Cups = Field(None, description="Cups per day")
    max_coffee_intake: Cups = Field(None, description="Cups per day")
    min_bmi: Bmi = None
    max_bmi: Bmi = None
    min_sleep_hours: Hours = None
    max_sleep_hours: Hours = None

    @field_validator("country")
    @classmethod
    def _normalize_countries(cls, values: list[str]) -> list[str]:
        try:
            return sorted({normalize_country(v) for v in values})
        except FieldError as exc:
            raise ValueError(f"invalid country: {exc}") from exc

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        for low_name, high_name in RANGE_PAIRS:
            low, high = getattr(self, low_name), getattr(self, high_name)
            if low is not None and high is not None and low > high:
                raise ValueError(
                    f"{low_name} ({low}) must be less than or equal to {high_name} ({high})"
                )
        return self


class ListParams(ParticipantFilters):
    limit: int = Field(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size")
    after_id: int = Field(
        0,
        ge=0,
        description="Keyset cursor: return participants with `id` greater than this. "
        "Pass the previous page's `next_cursor`.",
    )
    include_total: bool = Field(
        False, description="Also count all matching rows (costs a full count on large tables)"
    )


class SummaryGroup(StrEnum):
    COUNTRY = "country"
    GENDER = "gender"
    OCCUPATION = "occupation"
    SLEEP_QUALITY = "sleep_quality"
    STRESS_LEVEL = "stress_level"
    HEALTH_ISSUES = "health_issues"
    AGE_GROUP = "age_group"
    COFFEE_INTAKE = "coffee_intake"


class SummaryParams(ParticipantFilters):
    group_by: SummaryGroup = SummaryGroup.COFFEE_INTAKE


class Participant(BaseModel):
    id: int
    age: int
    gender: Gender
    country: str
    coffee_intake: float = Field(description="Cups per day")
    caffeine_mg: float = Field(description="Milligrams per day")
    sleep_hours: float = Field(description="Hours per night")
    sleep_quality: SleepQuality
    bmi: float
    heart_rate: int = Field(description="Resting beats per minute")
    stress_level: StressLevel
    physical_activity_hours: float = Field(description="Hours per week")
    health_issues: HealthIssues
    occupation: Occupation
    smoking: bool
    alcohol_consumption: bool


class ParticipantPage(BaseModel):
    items: list[Participant]
    limit: int
    next_cursor: int | None = Field(
        description="Pass as `after_id` to get the next page; null on the last page"
    )
    total: int | None = Field(None, description="Only when `include_total=true`")


class SummaryRow(BaseModel):
    group: str
    participants: int
    avg_sleep_hours: float
    avg_coffee_intake: float
    avg_caffeine_mg: float
    avg_bmi: float
    avg_heart_rate: float


class Summary(BaseModel):
    group_by: SummaryGroup
    groups: list[SummaryRow]
