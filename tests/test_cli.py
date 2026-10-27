import json
from pathlib import Path

import pytest

from hns import __version__, cli
from hns.cli import format_task_list, main, write_dump


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"hns {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: hns" in capsys.readouterr().out


def test_format_task_list_groups_dailies_and_habits() -> None:
    tasks = [
        {"id": "d1", "type": "daily", "text": "Gym 🏋️"},
        {"id": "h1", "type": "habit", "text": "Apliqué a un trabajo"},
        {"id": "t1", "type": "todo", "text": "Ignored"},
        {"id": "d2", "type": "daily", "text": "Agua"},
    ]

    assert format_task_list(tasks) == (
        "Dailies (2)\n  d1  Gym 🏋️\n  d2  Agua\n\nHabits (1)\n  h1  Apliqué a un trabajo"
    )


def test_format_task_list_with_no_tasks() -> None:
    assert format_task_list([]) == "Dailies (0)\n\nHabits (0)"


def test_write_dump_saves_utf8_json(tmp_path: Path) -> None:
    dump_dir = tmp_path / "local"
    user = {"stats": {"lvl": 10}}
    tasks = [{"id": "d1", "text": "Búsqueda de empleo ✅"}]

    paths = write_dump(dump_dir, user, tasks)

    assert paths == [dump_dir / "user.json", dump_dir / "tasks.json"]
    assert json.loads(paths[0].read_text(encoding="utf-8")) == user
    tasks_text = paths[1].read_text(encoding="utf-8")
    assert "Búsqueda de empleo ✅" in tasks_text
    assert json.loads(tasks_text) == tasks


def test_discover_without_credentials_fails_with_clear_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.delenv("HABITICA_USER_ID", raising=False)
    monkeypatch.delenv("HABITICA_API_TOKEN", raising=False)

    assert main(["discover"]) == 1
    assert "HABITICA_USER_ID, HABITICA_API_TOKEN" in caplog.text
