"""Turn a `hns discover --dump` into anonymized test fixtures.

Keeps only the fields the sync uses and replaces every task ID with a fake,
sequential UUID, so no real user ID, task ID or account data reaches git.

Usage: uv run python scripts/anonymize_dump.py [local] [tests/fixtures]
"""

import json
import sys
from pathlib import Path
from typing import Any

USER_FIELDS = {
    "stats": ("lvl", "hp", "gp", "exp"),
    "preferences": ("dayStart", "timezoneOffset"),
}
TASK_FIELDS = ("type", "text", "streak", "isDue", "completed", "history")
TASK_TYPES = ("daily", "habit")


def fake_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def anonymize_user(user: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        section: {key: user[section][key] for key in keys if key in user[section]}
        for section, keys in USER_FIELDS.items()
    }
    result["lastCron"] = user["lastCron"]
    return result


def anonymize_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = [task for task in tasks if task.get("type") in TASK_TYPES]
    return [
        {"id": fake_id(index), **{key: task[key] for key in TASK_FIELDS if key in task}}
        for index, task in enumerate(kept, start=1)
    ]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(source: Path, target: Path) -> None:
    user = json.loads((source / "user.json").read_text(encoding="utf-8"))
    tasks = json.loads((source / "tasks.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "user.json", anonymize_user(user))
    write_json(target / "tasks.json", anonymize_tasks(tasks))
    print(f"Wrote {target / 'user.json'} and {target / 'tasks.json'}")


if __name__ == "__main__":
    args = sys.argv[1:]
    main(
        Path(args[0]) if args else Path("local"),
        Path(args[1]) if len(args) > 1 else Path("tests/fixtures"),
    )
