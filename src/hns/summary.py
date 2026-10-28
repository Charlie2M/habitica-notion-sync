"""Pure logic: turn Habitica tasks and their history into one summary per day.

No network calls happen here. For dailies, Habitica's day changes when cron
runs (the first time the app is opened after the custom day start), not at the
day start itself:

- A checked daily gets a `completed=True` entry stamped with the click time. It
  belongs to the day that was open then, i.e. the day opened by the latest cron
  before it. Dailies checked in "Record Yesterday's Activity" are stamped the
  next morning, but before that morning's cron, so they land on the right day.
- At cron, every daily that wasn't completed gets a `completed=False` entry
  stamped with the cron time. It describes the day being closed, which is the
  day opened by the previous cron.

Habits keep one entry per day and switch days at the custom day start.
"""

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from hns.config import ConfigError, SyncConfig
from hns.models import Daily, DailyHistoryEntry, Habit, Tasks, User

# Cron writes one entry per incomplete daily within milliseconds of each other.
CRON_CLUSTER_GAP = timedelta(minutes=1)


class DailyStatus(StrEnum):
    DONE = "done"
    MISSED = "missed"
    NOT_DUE = "not_due"


@dataclass(frozen=True)
class DayClock:
    """Maps instants to Habitica days for a timezone and custom day start."""

    tz: ZoneInfo
    day_start_hour: int

    def day_of(self, instant: datetime) -> date:
        return (instant.astimezone(self.tz) - timedelta(hours=self.day_start_hour)).date()


@dataclass(frozen=True)
class DaySummary:
    day: date
    completed: int
    scheduled: int
    # Notion column -> status. Columns whose daily has no entry that day are absent.
    dailies: dict[str, DailyStatus]
    habits_up: int
    habits_down: int
    applications: int
    level: int
    health: float
    gold: float
    gym_streak: int


def find_cron_runs(dailies: Iterable[Daily], last_cron: datetime) -> list[datetime]:
    """Return the start of every cron run visible in the dailies' history."""
    stamps = [entry.date for daily in dailies for entry in daily.history if not entry.completed]
    stamps.append(last_cron)
    stamps.sort()

    runs: list[datetime] = []
    previous: datetime | None = None
    for stamp in stamps:
        if previous is None or stamp - previous > CRON_CLUSTER_GAP:
            runs.append(stamp)
        previous = stamp
    return runs


class CronTimeline:
    """Decides which Habitica day a daily history entry belongs to."""

    def __init__(self, runs: Sequence[datetime], clock: DayClock) -> None:
        self._runs = sorted(runs)
        self._clock = clock

    def _run_index(self, instant: datetime) -> int:
        """Index of the latest cron run that started at or before `instant`, or -1."""
        return bisect_right(self._runs, instant) - 1

    def day_of_entry(self, entry: DailyHistoryEntry) -> date:
        index = self._run_index(entry.date)
        if entry.completed:
            if index >= 0:
                return self._clock.day_of(self._runs[index])
            # Before the first known cron: use the day start, unless that day's
            # cron hadn't run yet (the day wasn't open).
            day = self._clock.day_of(entry.date)
            if self._runs and self._clock.day_of(self._runs[0]) == day:
                return day - timedelta(days=1)
            return day

        if index >= 1:
            return self._clock.day_of(self._runs[index - 1])
        return self._clock.day_of(entry.date) - timedelta(days=1)


def daily_entries_by_day(daily: Daily, timeline: CronTimeline) -> dict[date, DailyHistoryEntry]:
    """Pick the entry that describes each day; a completion wins over a cron entry."""
    by_day: dict[date, DailyHistoryEntry] = {}
    for entry in daily.history:
        day = timeline.day_of_entry(entry)
        current = by_day.get(day)
        if current is None or (entry.completed and not current.completed):
            by_day[day] = entry
    return by_day


def status_of(entry: DailyHistoryEntry) -> DailyStatus:
    if entry.completed:
        return DailyStatus.DONE
    return DailyStatus.MISSED if entry.is_due else DailyStatus.NOT_DUE


def check_mapping(tasks: Tasks, config: SyncConfig) -> None:
    """Fail with the exact column and ID if config.yaml points at a missing task."""
    daily_ids = {daily.id for daily in tasks.dailies}
    habit_ids = {habit.id for habit in tasks.habits}
    problems = [
        f"daily {task_id} (column {column!r})"
        for task_id, column in config.dailies.items()
        if task_id not in daily_ids
    ]
    if config.gym_streak_task not in daily_ids:
        problems.append(f"daily {config.gym_streak_task} (gym_streak_task)")
    if config.habits.applications not in habit_ids:
        problems.append(f"habit {config.habits.applications} (habits.applications)")
    if problems:
        raise ConfigError(
            f"config.yaml refers to tasks that don't exist in Habitica: {'; '.join(problems)}. "
            "Run `hns discover` to see the current IDs."
        )


def _habit_counts(habit: Habit, clock: DayClock, day: date) -> tuple[int, int]:
    entries = [entry for entry in habit.history if clock.day_of(entry.date) == day]
    return sum(e.scored_up for e in entries), sum(e.scored_down for e in entries)


def summarize_days(
    days: Iterable[date], user: User, tasks: Tasks, config: SyncConfig
) -> list[DaySummary]:
    check_mapping(tasks, config)
    clock = DayClock(config.tz, config.day_start_hour)
    timeline = CronTimeline(find_cron_runs(tasks.dailies, user.last_cron), clock)
    entries = {daily.id: daily_entries_by_day(daily, timeline) for daily in tasks.dailies}
    gym_streak = next(d.streak for d in tasks.dailies if d.id == config.gym_streak_task)

    summaries = []
    for day in days:
        day_entries = {
            daily_id: by_day[day] for daily_id, by_day in entries.items() if day in by_day
        }
        due = [entry for entry in day_entries.values() if entry.is_due]
        habit_counts = {habit.id: _habit_counts(habit, clock, day) for habit in tasks.habits}

        summaries.append(
            DaySummary(
                day=day,
                completed=sum(entry.completed for entry in due),
                scheduled=len(due),
                dailies={
                    column: status_of(day_entries[task_id])
                    for task_id, column in config.dailies.items()
                    if task_id in day_entries
                },
                habits_up=sum(up for up, _ in habit_counts.values()),
                habits_down=sum(down for _, down in habit_counts.values()),
                applications=habit_counts[config.habits.applications][0],
                level=user.stats.lvl,
                health=user.stats.hp,
                gold=user.stats.gp,
                gym_streak=gym_streak,
            )
        )
    return summaries


def summarize_day(day: date, user: User, tasks: Tasks, config: SyncConfig) -> DaySummary:
    return summarize_days([day], user, tasks, config)[0]


def latest_closed_days(user: User, config: SyncConfig, count: int = 3) -> list[date]:
    """The `count` most recent days already closed by a cron, oldest first."""
    open_day = DayClock(config.tz, config.day_start_hour).day_of(user.last_cron)
    return [open_day - timedelta(days=offset) for offset in range(count, 0, -1)]
