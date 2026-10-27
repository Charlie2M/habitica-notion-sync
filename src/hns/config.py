"""Load and validate settings from environment variables."""

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr


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
