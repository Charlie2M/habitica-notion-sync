from pathlib import Path
from typing import Any

import pytest

from hns.config import ConfigError, load_sync_config, parse_sync_config

VALID: dict[str, Any] = {
    "timezone": "America/El_Salvador",
    "day_start_hour": 4,
    "gym_streak_task": "gym-id",
    "dailies": {"gym-id": "Gym", "water-id": "Agua"},
    "habits": {"applications": "apply-id"},
}


def test_parses_valid_config() -> None:
    config = parse_sync_config(VALID)

    assert config.tz.key == "America/El_Salvador"
    assert config.dailies == {"gym-id": "Gym", "water-id": "Agua"}
    assert config.habits.applications == "apply-id"


def test_project_config_yaml_is_valid() -> None:
    config = load_sync_config(Path(__file__).parents[1] / "config.yaml")

    assert config.timezone == "America/El_Salvador"
    assert len(config.dailies) == 9


def test_placeholders_are_reported_exactly() -> None:
    data = {
        **VALID,
        "gym_streak_task": "<id-daily-gym>",
        "dailies": {"<id-gym>": "Gym", "water-id": "Agua"},
    }

    with pytest.raises(ConfigError, match=r"placeholder IDs in: dailies\[Gym\], gym_streak_task\."):
        parse_sync_config(data)


def test_duplicate_columns_are_rejected() -> None:
    data = {**VALID, "dailies": {"a": "Gym", "b": "Gym"}}

    with pytest.raises(ConfigError, match="mapped more than once: Gym"):
        parse_sync_config(data)


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ConfigError, match="timezone: .*unknown timezone 'Mars/Olympus'"):
        parse_sync_config({**VALID, "timezone": "Mars/Olympus"})


@pytest.mark.parametrize("hour", [-1, 24])
def test_day_start_hour_must_be_an_hour(hour: int) -> None:
    with pytest.raises(ConfigError, match="day_start_hour"):
        parse_sync_config({**VALID, "day_start_hour": hour})


def test_missing_and_unknown_keys_are_named() -> None:
    data = {key: value for key, value in VALID.items() if key != "habits"}
    data["dayly"] = {}

    with pytest.raises(ConfigError) as exc:
        parse_sync_config(data)

    assert "habits: Field required" in str(exc.value)
    assert "dayly: Extra inputs are not permitted" in str(exc.value)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Config file not found"):
        load_sync_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("dailies: [unclosed", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid YAML"):
        load_sync_config(path)


def test_non_mapping_yaml() -> None:
    with pytest.raises(ConfigError, match="must be a YAML mapping, got list"):
        parse_sync_config(["not", "a", "mapping"])
