"""Command-line entry point (`hns`)."""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from notion_client import Client
from pydantic import ValidationError

from hns import __version__
from hns.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    HabiticaCredentials,
    load_habitica_credentials,
    load_notion_settings,
    load_sync_config,
)
from hns.habitica import HabiticaClient, HabiticaError
from hns.models import Tasks, User
from hns.notion_sync import (
    NotionError,
    NotionSync,
    build_properties,
    day_title,
    describe_properties,
    expected_schema,
)
from hns.summary import day_settings_mismatches, latest_closed_days, summarize_days

logger = logging.getLogger(__name__)

DUMP_DIR = Path("local")
DISCOVER_TASK_TYPES = (("daily", "Dailies"), ("habit", "Habits"))
# Re-syncing a few closed days lets a late check-off or a missed run fix itself.
SYNC_DAYS = 3


class UsageError(Exception):
    """Raised when the command-line arguments can't be honored."""


def format_task_list(tasks: Sequence[dict[str, Any]]) -> str:
    """Render dailies and habits as `id  text` lines, grouped by type."""
    sections = []
    for task_type, title in DISCOVER_TASK_TYPES:
        matching = [task for task in tasks if task.get("type") == task_type]
        lines = [f"{title} ({len(matching)})"]
        lines += [f"  {task.get('id', '?')}  {task.get('text', '')}" for task in matching]
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def write_dump(dump_dir: Path, user: dict[str, Any], tasks: list[dict[str, Any]]) -> list[Path]:
    """Save raw API data as pretty-printed JSON files and return their paths."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in (("user", user), ("tasks", tasks)):
        path = dump_dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def run_discover(args: argparse.Namespace) -> int:
    credentials = load_habitica_credentials()
    with HabiticaClient(credentials) as client:
        tasks = client.get_tasks()
        print(format_task_list(tasks))
        if args.dump:
            user = client.get_user()
            for path in write_dump(DUMP_DIR, user, tasks):
                print(f"Saved {path}")
    return 0


def fetch_habitica(credentials: HabiticaCredentials) -> tuple[User, Tasks]:
    with HabiticaClient(credentials) as client:
        raw_tasks = client.get_tasks()
        raw_user = client.get_user()
    try:
        return User.model_validate(raw_user), Tasks.from_api(raw_tasks)
    except ValidationError as exc:
        raise HabiticaError(f"Unexpected Habitica response: {exc}") from exc


def days_to_sync(requested: date | None, closed_days: list[date]) -> list[date]:
    if requested is None:
        return closed_days
    if requested > closed_days[-1]:
        raise UsageError(
            f"{requested} is not closed yet; the latest closed day is {closed_days[-1]}. "
            "Habitica closes a day when its cron runs (when you open the app the next day)."
        )
    return [requested]


def run_sync(args: argparse.Namespace) -> int:
    config = load_sync_config(args.config)
    credentials = load_habitica_credentials()
    # Load Notion settings before the slow Habitica calls so a missing secret fails fast.
    notion_settings = None if args.dry_run else load_notion_settings()

    user, tasks = fetch_habitica(credentials)
    for problem in day_settings_mismatches(user, config):
        logger.warning("%s", problem)

    closed_days = latest_closed_days(user, config, SYNC_DAYS)
    newest_closed = closed_days[-1]
    columns = list(config.dailies.values())
    rows = [
        # Level, health, gold and streak are "as of now", so only the newest day gets them.
        (
            summary.day,
            build_properties(summary, columns, include_stats=summary.day == newest_closed),
        )
        for summary in summarize_days(days_to_sync(args.date, closed_days), user, tasks, config)
    ]

    if notion_settings is None:
        for day, properties in rows:
            print(f"{day_title(day)} ({day})")
            for line in describe_properties(properties):
                print(f"  {line}")
        print("Dry run: nothing was written to Notion.")
        return 0

    with Client(
        auth=notion_settings.token.get_secret_value(), logger=logging.getLogger("notion_client")
    ) as notion:
        sync = NotionSync(notion, notion_settings.data_source_id)
        sync.check_schema(expected_schema(columns))
        for day, properties in rows:
            action = sync.upsert(day, properties)
            print(f"{action.capitalize()} {day_title(day)} ({day})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hns",
        description="Sync daily Habitica stats into a Notion database.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    discover = subparsers.add_parser(
        "discover", help="list your Habitica dailies and habits with their IDs"
    )
    discover.add_argument(
        "--dump",
        action="store_true",
        help=f"also save the raw user and tasks JSON in {DUMP_DIR}/",
    )
    discover.set_defaults(handler=run_discover)

    sync = subparsers.add_parser(
        "sync", help=f"summarize the last {SYNC_DAYS} closed days and upsert them into Notion"
    )
    sync.add_argument(
        "--date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="sync only this day (must already be closed by Habitica's cron)",
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="print what would be written instead of writing"
    )
    sync.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"path to the task mapping (default: {DEFAULT_CONFIG_PATH})",
    )
    sync.set_defaults(handler=run_sync)
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; keep the output focused on our own messages.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    # Task names can contain emoji; avoid UnicodeEncodeError when output is piped on Windows.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    configure_logging(args.verbose)
    load_dotenv()
    try:
        return args.handler(args)
    except (ConfigError, HabiticaError, NotionError, UsageError) as exc:
        logger.error("%s", exc)
        return 1
