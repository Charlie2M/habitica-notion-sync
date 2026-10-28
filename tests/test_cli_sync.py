"""End-to-end tests for `hns sync`, with Habitica and Notion replaced by fakes."""

import json
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
import yaml

from hns import cli
from hns.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


FIXTURE_CONFIG = {
    "timezone": "America/El_Salvador",
    "day_start_hour": 4,
    "gym_streak_task": fixture_id(11),
    "dailies": {fixture_id(11): "Gym", fixture_id(13): "Agua", fixture_id(21): "Caminata"},
    "habits": {"applications": fixture_id(3)},
}


class FakeHabiticaClient:
    """Serves the anonymized fixtures instead of calling Habitica."""

    instances = 0

    def __init__(self, credentials: object) -> None:
        FakeHabiticaClient.instances += 1

    def __enter__(self) -> "FakeHabiticaClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    def get_tasks(self) -> list[dict[str, Any]]:
        return json.loads((FIXTURES / "tasks.json").read_text(encoding="utf-8"))

    def get_user(self) -> dict[str, Any]:
        return json.loads((FIXTURES / "user.json").read_text(encoding="utf-8"))


class FakeNotionSync:
    """Records upserts instead of calling Notion."""

    upserts: list[tuple[Any, dict[str, Any]]] = []
    checked_schema: dict[str, str] | None = None

    def __init__(self, client: object, data_source_id: str) -> None:
        self.data_source_id = data_source_id

    def check_schema(self, expected: dict[str, str]) -> None:
        FakeNotionSync.checked_schema = expected

    def upsert(self, day: Any, properties: dict[str, Any]) -> str:
        FakeNotionSync.upserts.append((day, properties))
        return "created"


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(FIXTURE_CONFIG, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "HabiticaClient", FakeHabiticaClient)
    monkeypatch.setattr(cli, "NotionSync", FakeNotionSync)
    FakeHabiticaClient.instances = 0
    FakeNotionSync.upserts = []
    FakeNotionSync.checked_schema = None
    monkeypatch.setenv("HABITICA_USER_ID", "00000000-0000-4000-8000-000000000000")
    monkeypatch.setenv("HABITICA_API_TOKEN", "test-token-not-real")
    monkeypatch.setenv("NOTION_TOKEN", "test-notion-token-not-real")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds-123")


def test_dry_run_prints_the_last_three_closed_days(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["sync", "--dry-run", "--config", str(config_path)]) == 0

    out = capsys.readouterr().out
    assert "Dom 25 oct (2026-10-25)" in out
    assert "Lun 26 oct (2026-10-26)" in out
    assert "Mar 27 oct (2026-10-27)" in out
    assert "Mié 28 oct" not in out  # still open
    assert out.count("Nivel: 6") == 1  # stats only on the newest closed day
    assert out.index("Nivel: 6") > out.index("Mar 27 oct")
    assert "Dry run: nothing was written to Notion." in out
    assert FakeNotionSync.upserts == []


def test_dry_run_does_not_need_notion_settings(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTION_TOKEN")
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID")

    assert main(["sync", "--dry-run", "--config", str(config_path)]) == 0


def test_sync_upserts_each_day_after_checking_the_schema(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["sync", "--config", str(config_path)]) == 0

    assert FakeNotionSync.checked_schema is not None
    assert {"Gym", "Agua", "Caminata"} <= FakeNotionSync.checked_schema.keys()
    days = [str(day) for day, _ in FakeNotionSync.upserts]
    assert days == ["2026-10-25", "2026-10-26", "2026-10-27"]
    newest = FakeNotionSync.upserts[-1][1]
    assert newest["Agua"] == {"select": {"name": "✅ Cumplida"}}
    assert newest["Racha gym"] == {"number": 7}
    assert "Created Mar 27 oct (2026-10-27)" in capsys.readouterr().out


def test_single_closed_date(config_path: Path) -> None:
    assert main(["sync", "--date", "2026-10-21", "--config", str(config_path)]) == 0

    assert [(str(day), "Nivel" in props) for day, props in FakeNotionSync.upserts] == [
        ("2026-10-21", False)
    ]


def test_open_day_is_rejected(config_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert main(["sync", "--date", "2026-10-28", "--config", str(config_path)]) == 1

    assert "2026-10-28 is not closed yet; the latest closed day is 2026-10-27" in caplog.text
    assert FakeNotionSync.upserts == []


def test_missing_notion_settings_fail_before_calling_habitica(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("NOTION_TOKEN")

    assert main(["sync", "--config", str(config_path)]) == 1

    assert "NOTION_TOKEN" in caplog.text
    assert FakeHabiticaClient.instances == 0


def test_placeholder_config_is_reported(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.yaml"
    data = {**FIXTURE_CONFIG, "gym_streak_task": "<id-daily-gym>"}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert main(["sync", "--dry-run", "--config", str(path)]) == 1
    assert "placeholder IDs in: gym_streak_task" in caplog.text
