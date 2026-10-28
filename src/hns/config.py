"""Load and validate settings from environment variables and config.yaml."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


class HabiticaCredentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    api_token: SecretStr


def _require(env: Mapping[str, str], *names: str) -> dict[str, str]:
    values = {name: env.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in .env (see .env.example) or as GitHub repository secrets."
        )
    return values


def load_habitica_credentials(env: Mapping[str, str] | None = None) -> HabiticaCredentials:
    values = _require(os.environ if env is None else env, "HABITICA_USER_ID", "HABITICA_API_TOKEN")
    return HabiticaCredentials(
        user_id=values["HABITICA_USER_ID"],
        api_token=SecretStr(values["HABITICA_API_TOKEN"]),
    )


def _is_placeholder(task_id: str) -> bool:
    return task_id.startswith("<")


class HabitsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    applications: str


class SyncConfig(BaseModel):
    """Contents of config.yaml: how Habitica tasks map to Notion columns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str
    day_start_hour: int = Field(ge=0, le=23)
    gym_streak_task: str
    dailies: dict[str, str]
    habits: HabitsConfig

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("dailies")
    @classmethod
    def _unique_columns(cls, value: dict[str, str]) -> dict[str, str]:
        columns = list(value.values())
        duplicates = sorted({column for column in columns if columns.count(column) > 1})
        if duplicates:
            raise ValueError(f"column(s) mapped more than once: {', '.join(duplicates)}")
        return value

    def placeholder_fields(self) -> list[str]:
        """Return the config fields that still hold `<...>` placeholder IDs."""
        fields = [
            f"dailies[{column}]"
            for task_id, column in self.dailies.items()
            if _is_placeholder(task_id)
        ]
        if _is_placeholder(self.gym_streak_task):
            fields.append("gym_streak_task")
        if _is_placeholder(self.habits.applications):
            fields.append("habits.applications")
        return fields


def _format_validation_error(error: ValidationError) -> str:
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        problems.append(f"{location}: {item['msg']}")
    return "; ".join(problems)


def parse_sync_config(data: Any, source: str = "config.yaml") -> SyncConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{source} must be a YAML mapping, got {type(data).__name__}.")
    try:
        config = SyncConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid {source}: {_format_validation_error(exc)}") from exc

    placeholders = config.placeholder_fields()
    if placeholders:
        raise ConfigError(
            f"{source} still has placeholder IDs in: {', '.join(placeholders)}. "
            "Run `hns discover` to get the real task IDs."
        )
    return config


def load_sync_config(path: Path = DEFAULT_CONFIG_PATH) -> SyncConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    return parse_sync_config(data, source=str(path))
