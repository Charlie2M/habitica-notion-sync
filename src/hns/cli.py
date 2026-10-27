"""Command-line entry point (`hns`)."""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from hns import __version__
from hns.config import ConfigError, load_habitica_credentials
from hns.habitica import HabiticaClient, HabiticaError

logger = logging.getLogger(__name__)

DUMP_DIR = Path("local")
DISCOVER_TASK_TYPES = (("daily", "Dailies"), ("habit", "Habits"))


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
    except (ConfigError, HabiticaError) as exc:
        logger.error("%s", exc)
        return 1
