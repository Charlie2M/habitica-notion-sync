"""Pydantic models for the parts of the Habitica API responses we use.

Only fields confirmed in a real `hns discover --dump` are modeled; anything else
in the payload is ignored.
"""

from collections.abc import Iterable
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class HabiticaModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )


class DailyHistoryEntry(HabiticaModel):
    """Written when a daily is checked (completed=True) or at cron if it wasn't."""

    date: AwareDatetime  # the API sends milliseconds since epoch
    completed: bool
    is_due: bool


class HabitHistoryEntry(HabiticaModel):
    """One entry per day, created on the first click and updated by later ones."""

    date: AwareDatetime
    scored_up: int = 0
    scored_down: int = 0


class Daily(HabiticaModel):
    id: str
    text: str
    streak: int = 0
    history: list[DailyHistoryEntry] = []


class Habit(HabiticaModel):
    id: str
    text: str
    history: list[HabitHistoryEntry] = []


class Stats(HabiticaModel):
    lvl: int
    hp: float
    gp: float


class Preferences(HabiticaModel):
    day_start: int
    timezone_offset: int  # minutes behind UTC, e.g. 360 for UTC-6


class User(HabiticaModel):
    stats: Stats
    preferences: Preferences
    last_cron: AwareDatetime


class Tasks(HabiticaModel):
    dailies: list[Daily]
    habits: list[Habit]

    @classmethod
    def from_api(cls, raw_tasks: Iterable[dict[str, Any]]) -> "Tasks":
        """Build from the `GET /tasks/user` payload, ignoring todos and rewards."""
        raw_tasks = list(raw_tasks)
        return cls(
            dailies=[Daily.model_validate(t) for t in raw_tasks if t.get("type") == "daily"],
            habits=[Habit.model_validate(t) for t in raw_tasks if t.get("type") == "habit"],
        )
