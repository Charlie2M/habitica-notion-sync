import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hns.config import ConfigError, HabitsConfig, SyncConfig
from hns.models import (
    Daily,
    DailyHistoryEntry,
    Habit,
    HabitHistoryEntry,
    Preferences,
    Stats,
    Tasks,
    User,
)
from hns.summary import (
    DailyStatus,
    DayClock,
    find_cron_runs,
    latest_closed_days,
    summarize_day,
    summarize_days,
)

FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("America/El_Salvador")

DONE, MISSED, NOT_DUE = DailyStatus.DONE, DailyStatus.MISSED, DailyStatus.NOT_DUE


# --- Builders for small synthetic scenarios ---------------------------------


def at(day: str, time: str) -> datetime:
    """Local El Salvador time, e.g. at("2026-10-24", "05:50")."""
    return datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=TZ)


def done(when: datetime, *, due: bool = True) -> DailyHistoryEntry:
    return DailyHistoryEntry(date=when, completed=True, is_due=due)


def cron(when: datetime, *, due: bool = True) -> DailyHistoryEntry:
    return DailyHistoryEntry(date=when, completed=False, is_due=due)


def daily(task_id: str, *entries: DailyHistoryEntry, streak: int = 0) -> Daily:
    return Daily(id=task_id, text=task_id, streak=streak, history=list(entries))


def habit(task_id: str, *entries: tuple[datetime, int, int]) -> Habit:
    history = [HabitHistoryEntry(date=d, scored_up=up, scored_down=down) for d, up, down in entries]
    return Habit(id=task_id, text=task_id, history=history)


def make_tasks(*dailies: Daily, habits: tuple[Habit, ...] = ()) -> Tasks:
    """Add empty versions of the dailies and habit that CONFIG maps, if not given."""
    daily_list = list(dailies)
    given = {d.id for d in daily_list}
    daily_list += [daily(task_id) for task_id in CONFIG.dailies if task_id not in given]
    habit_list = list(habits)
    if not any(h.id == "apply" for h in habit_list):
        habit_list.append(habit("apply"))
    return Tasks(dailies=daily_list, habits=habit_list)


def make_user(last_cron: datetime) -> User:
    return User(
        stats=Stats(lvl=6, hp=39.5, gp=187.25),
        preferences=Preferences(day_start=4, timezone_offset=360),
        last_cron=last_cron,
    )


CONFIG = SyncConfig(
    timezone="America/El_Salvador",
    day_start_hour=4,
    gym_streak_task="gym",
    dailies={"gym": "Gym", "water": "Agua", "walk": "Caminata"},
    habits=HabitsConfig(applications="apply"),
)


# --- Synthetic scenarios -----------------------------------------------------


def test_day_without_any_entries() -> None:
    summary = summarize_day(
        date(2026, 10, 21), make_user(at("2026-10-22", "06:00")), make_tasks(), CONFIG
    )

    assert summary.completed == 0
    assert summary.scheduled == 0
    assert summary.dailies == {}
    assert (summary.habits_up, summary.habits_down, summary.applications) == (0, 0, 0)


def test_daily_that_was_not_due() -> None:
    tasks = make_tasks(daily("walk", cron(at("2026-10-23", "05:30"), due=False)))

    summary = summarize_day(date(2026, 10, 22), make_user(at("2026-10-23", "05:30")), tasks, CONFIG)

    assert summary.dailies == {"Caminata": NOT_DUE}
    assert (summary.completed, summary.scheduled) == (0, 0)


def test_failed_day() -> None:
    cron_time = at("2026-10-23", "05:30")
    tasks = make_tasks(daily("gym", cron(cron_time)), daily("water", cron(cron_time)))

    summary = summarize_day(date(2026, 10, 22), make_user(cron_time), tasks, CONFIG)

    assert summary.dailies == {"Gym": MISSED, "Agua": MISSED}
    assert (summary.completed, summary.scheduled) == (0, 2)


def test_month_change() -> None:
    oct_31_cron = at("2026-10-31", "05:40")
    nov_1_cron = at("2026-11-01", "05:45")
    tasks = make_tasks(
        # 20:00 local on Oct 31 is already Nov 1 in UTC.
        daily("gym", cron(oct_31_cron, due=False), done(at("2026-10-31", "20:00"))),
        # Checked after midnight but before the day start: still Oct 31.
        daily("water", cron(oct_31_cron), done(at("2026-11-01", "03:30"))),
        daily("walk", cron(oct_31_cron), cron(nov_1_cron)),
    )

    oct_31, nov_1 = summarize_days(
        [date(2026, 10, 31), date(2026, 11, 1)], make_user(nov_1_cron), tasks, CONFIG
    )

    assert oct_31.dailies == {"Gym": DONE, "Agua": DONE, "Caminata": MISSED}
    assert (oct_31.completed, oct_31.scheduled) == (2, 3)
    assert nov_1.dailies == {}


def test_record_yesterdays_activity_counts_for_the_previous_day() -> None:
    cron_time = at("2026-10-24", "06:00")
    tasks = make_tasks(
        daily("water", done(at("2026-10-24", "05:59"))),  # checked in the "yesterday" screen
        daily("gym", done(at("2026-10-24", "06:01"))),  # checked right after cron: today
        daily("walk", cron(cron_time, due=False)),
    )

    friday, saturday = summarize_days(
        [date(2026, 10, 23), date(2026, 10, 24)], make_user(cron_time), tasks, CONFIG
    )

    assert friday.dailies == {"Agua": DONE, "Caminata": NOT_DUE}
    assert saturday.dailies == {"Gym": DONE}


def test_completed_when_not_due_is_shown_but_not_counted() -> None:
    tasks = make_tasks(daily("gym", done(at("2026-10-22", "10:00"), due=False)))

    summary = summarize_day(date(2026, 10, 22), make_user(at("2026-10-22", "06:00")), tasks, CONFIG)

    assert summary.dailies == {"Gym": DONE}
    assert (summary.completed, summary.scheduled) == (0, 0)


def test_day_without_opening_the_app_has_no_data() -> None:
    # Cron ran on the 20th and then not again until the 22nd.
    tasks = make_tasks(
        daily("water", cron(at("2026-10-20", "06:00")), cron(at("2026-10-22", "06:00"))),
    )

    days = [date(2026, 10, 20), date(2026, 10, 21)]
    day_20, day_21 = summarize_days(days, make_user(at("2026-10-22", "06:00")), tasks, CONFIG)

    assert day_20.dailies == {"Agua": MISSED}
    assert day_21.dailies == {}


def test_entries_before_the_first_known_cron() -> None:
    first_cron = at("2026-10-20", "06:00")
    tasks = make_tasks(
        daily("gym", done(at("2026-10-19", "10:00"))),
        daily("water", cron(first_cron)),
    )

    summary = summarize_day(date(2026, 10, 19), make_user(first_cron), tasks, CONFIG)

    assert summary.dailies == {"Gym": DONE, "Agua": MISSED}


def test_a_completion_wins_over_a_cron_entry_for_the_same_day() -> None:
    cron_time = at("2026-10-23", "05:30")
    tasks = make_tasks(daily("gym", done(at("2026-10-22", "09:00")), cron(cron_time)))

    summary = summarize_day(date(2026, 10, 22), make_user(cron_time), tasks, CONFIG)

    assert summary.dailies == {"Gym": DONE}
    assert (summary.completed, summary.scheduled) == (1, 1)


def test_habits_are_summed_and_use_the_day_start() -> None:
    tasks = make_tasks(
        habits=(
            habit("apply", (at("2026-10-21", "15:00"), 2, 0)),
            habit("junk-food", (at("2026-10-22", "03:00"), 1, 3)),  # before 04:00: Oct 21
            habit("water-extra", (at("2026-10-22", "09:00"), 5, 0)),  # Oct 22
        )
    )

    summary = summarize_day(date(2026, 10, 21), make_user(at("2026-10-22", "06:00")), tasks, CONFIG)

    assert (summary.habits_up, summary.habits_down, summary.applications) == (3, 3, 2)


def test_stats_snapshot_and_gym_streak() -> None:
    tasks = make_tasks(daily("gym", streak=7))

    summary = summarize_day(date(2026, 10, 21), make_user(at("2026-10-22", "06:00")), tasks, CONFIG)

    assert (summary.level, summary.health, summary.gold, summary.gym_streak) == (6, 39.5, 187.25, 7)


def test_unknown_task_in_config_is_reported_exactly() -> None:
    config = CONFIG.model_copy(update={"dailies": {**CONFIG.dailies, "missing-id": "Postura"}})

    with pytest.raises(ConfigError, match=r"daily missing-id \(column 'Postura'\)"):
        summarize_day(
            date(2026, 10, 21), make_user(at("2026-10-22", "06:00")), make_tasks(), config
        )


def test_cron_entries_milliseconds_apart_are_one_run() -> None:
    runs = find_cron_runs(
        [
            daily("a", cron(at("2026-10-23", "05:30:00.100"))),
            daily("b", cron(at("2026-10-23", "05:30:00.130")), cron(at("2026-10-24", "06:10"))),
        ],
        last_cron=at("2026-10-24", "06:09:59.900"),
    )

    assert runs == [at("2026-10-23", "05:30:00.100"), at("2026-10-24", "06:09:59.900")]


def test_latest_closed_days_excludes_the_open_day() -> None:
    days = latest_closed_days(make_user(at("2026-11-01", "05:45")), CONFIG)

    assert days == [date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31)]


def test_day_clock_uses_local_time_and_day_start() -> None:
    clock = DayClock(TZ, 4)

    assert clock.day_of(at("2026-10-22", "03:59")) == date(2026, 10, 21)
    assert clock.day_of(at("2026-10-22", "04:00")) == date(2026, 10, 22)


# --- Real (anonymized) Habitica data -----------------------------------------
# Fixture IDs come from scripts/anonymize_dump.py; expected values were worked
# out by hand from the dump, in local time.


def fixture_id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


FIXTURE_CONFIG = SyncConfig(
    timezone="America/El_Salvador",
    day_start_hour=4,
    gym_streak_task=fixture_id(11),
    dailies={
        fixture_id(11): "Gym",
        fixture_id(21): "Caminata",
        fixture_id(16): "Postura",
        fixture_id(12): "Comer el plan",
        fixture_id(13): "Agua",
        fixture_id(10): "Despertar 6:00",
        fixture_id(17): "Corte de juegos",
        fixture_id(15): "Búsqueda de empleo",
        fixture_id(14): "Proyecto personal",
    },
    habits=HabitsConfig(applications=fixture_id(3)),
)


@pytest.fixture(scope="module")
def real() -> tuple[User, Tasks]:
    user = User.model_validate_json((FIXTURES / "user.json").read_text(encoding="utf-8"))
    raw_tasks = json.loads((FIXTURES / "tasks.json").read_text(encoding="utf-8"))
    return user, Tasks.from_api(raw_tasks)


def test_real_tuesday_with_dailies_checked_the_next_morning(real: tuple[User, Tasks]) -> None:
    # Agua, Postura and Corte de juegos were checked on Wed 05:51:49.35, a fraction
    # of a second before Wednesday's cron (05:51:49.59), so they belong to Tuesday.
    summary = summarize_day(date(2026, 10, 27), *real, FIXTURE_CONFIG)

    assert summary.dailies == {
        "Gym": DONE,
        "Caminata": NOT_DUE,
        "Postura": DONE,
        "Comer el plan": MISSED,
        "Agua": DONE,
        "Despertar 6:00": DONE,
        "Corte de juegos": DONE,
        "Búsqueda de empleo": MISSED,
        "Proyecto personal": DONE,
    }
    assert (summary.completed, summary.scheduled) == (6, 8)
    assert (summary.habits_up, summary.habits_down, summary.applications) == (0, 1, 0)


def test_real_saturday(real: tuple[User, Tasks]) -> None:
    # Agua was checked on Sun 06:17:59.9, just before Sunday's cron at 06:18:00.1.
    summary = summarize_day(date(2026, 10, 24), *real, FIXTURE_CONFIG)

    assert summary.dailies == {
        "Gym": DONE,
        "Caminata": NOT_DUE,
        "Postura": MISSED,
        "Comer el plan": MISSED,
        "Agua": DONE,
        "Despertar 6:00": DONE,
        "Corte de juegos": MISSED,
        "Búsqueda de empleo": NOT_DUE,
        "Proyecto personal": NOT_DUE,
    }
    assert (summary.completed, summary.scheduled) == (3, 6)
    assert (summary.habits_up, summary.habits_down) == (1, 1)


def test_real_first_day_before_any_cron(real: tuple[User, Tasks]) -> None:
    summary = summarize_day(date(2026, 10, 19), *real, FIXTURE_CONFIG)

    # 9 dailies were due (Pesarme too, which has no column); only Postura was missed.
    assert (summary.completed, summary.scheduled) == (8, 9)
    assert summary.dailies["Postura"] == MISSED
    assert summary.dailies["Caminata"] == NOT_DUE


def test_real_job_applications(real: tuple[User, Tasks]) -> None:
    summary = summarize_day(date(2026, 10, 21), *real, FIXTURE_CONFIG)

    assert (summary.habits_up, summary.habits_down, summary.applications) == (2, 1, 1)


def test_real_cron_runs_and_closed_days(real: tuple[User, Tasks]) -> None:
    user, tasks = real

    assert len(find_cron_runs(tasks.dailies, user.last_cron)) == 9  # one per day, Oct 20-28
    assert latest_closed_days(user, FIXTURE_CONFIG) == [
        date(2026, 10, 25),
        date(2026, 10, 26),
        date(2026, 10, 27),
    ]


def test_real_stats_snapshot(real: tuple[User, Tasks]) -> None:
    summary = summarize_day(date(2026, 10, 27), *real, FIXTURE_CONFIG)

    assert (summary.level, summary.health, summary.gold, summary.gym_streak) == (6, 39, 187, 7)
