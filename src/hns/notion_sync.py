"""Write day summaries into the Notion data source, upserting by the "Fecha" date."""

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any, Literal

from notion_client import Client
from notion_client.errors import NotionClientErrorBase

from hns.summary import DailyStatus, DaySummary

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    DailyStatus.DONE: "✅ Cumplida",
    DailyStatus.MISSED: "❌ Fallada",
    DailyStatus.NOT_DUE: "➖ No tocaba",
}
WEEKDAYS = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

TITLE_PROPERTY = "Día"
DATE_PROPERTY = "Fecha"
COUNT_PROPERTIES = ("Cumplidas", "Programadas", "Habits +", "Habits -", "Aplicaciones")
STAT_PROPERTIES = ("Nivel", "Vida", "Oro", "Racha gym")
# "Notas" and "Cumplimiento (%)" are never written: one is free text, the other a formula.

Properties = dict[str, Any]


class NotionError(Exception):
    """Raised when the Notion API rejects a request or the database doesn't match."""


def day_title(day: date) -> str:
    """Short Spanish title, e.g. "Mar 27 oct" (independent of the system locale)."""
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def expected_schema(daily_columns: Iterable[str]) -> dict[str, str]:
    """Notion property name -> type for every property the sync writes."""
    schema = {TITLE_PROPERTY: "title", DATE_PROPERTY: "date"}
    schema |= {name: "number" for name in (*COUNT_PROPERTIES, *STAT_PROPERTIES)}
    schema |= {column: "select" for column in daily_columns}
    return schema


def build_properties(
    summary: DaySummary, daily_columns: Iterable[str], *, include_stats: bool
) -> Properties:
    """Notion properties for one row. Mapped columns without data are cleared."""
    properties: Properties = {
        TITLE_PROPERTY: {"title": [{"text": {"content": day_title(summary.day)}}]},
        DATE_PROPERTY: {"date": {"start": summary.day.isoformat()}},
        "Cumplidas": {"number": summary.completed},
        "Programadas": {"number": summary.scheduled},
        "Habits +": {"number": summary.habits_up},
        "Habits -": {"number": summary.habits_down},
        "Aplicaciones": {"number": summary.applications},
    }
    for column in daily_columns:
        status = summary.dailies.get(column)
        properties[column] = {"select": {"name": STATUS_LABELS[status]} if status else None}
    if include_stats:
        properties |= {
            "Nivel": {"number": summary.level},
            "Vida": {"number": round(summary.health, 1)},
            "Oro": {"number": round(summary.gold, 2)},
            "Racha gym": {"number": summary.gym_streak},
        }
    return properties


def describe_properties(properties: Mapping[str, Any]) -> list[str]:
    """Human-readable `name: value` lines, used by `hns sync --dry-run`."""
    lines = []
    for name, value in properties.items():
        if "title" in value:
            text = "".join(part["text"]["content"] for part in value["title"])
        elif "date" in value:
            text = value["date"]["start"]
        elif "select" in value:
            text = value["select"]["name"] if value["select"] else "(empty)"
        else:
            text = str(value["number"])
        lines.append(f"{name}: {text}")
    return lines


class NotionSync:
    def __init__(self, client: Client, data_source_id: str) -> None:
        self._client = client
        self._data_source_id = data_source_id

    def check_schema(self, expected: Mapping[str, str]) -> None:
        data_source = self._call(self._client.data_sources.retrieve, self._data_source_id)
        actual = {name: prop["type"] for name, prop in data_source["properties"].items()}
        problems = [
            f"{name!r} (expected {kind}, found {actual.get(name, 'nothing')})"
            for name, kind in expected.items()
            if actual.get(name) != kind
        ]
        if problems:
            raise NotionError(
                "The Notion database doesn't have the expected properties: " + "; ".join(problems)
            )

    def find_page_id(self, day: date) -> str | None:
        result = self._call(
            self._client.data_sources.query,
            self._data_source_id,
            filter={"property": DATE_PROPERTY, "date": {"equals": day.isoformat()}},
        )
        pages = result["results"]
        if len(pages) > 1:
            logger.warning("Found %d rows for %s; updating the first one", len(pages), day)
        return pages[0]["id"] if pages else None

    def upsert(self, day: date, properties: Properties) -> Literal["created", "updated"]:
        page_id = self.find_page_id(day)
        if page_id is None:
            self._call(
                self._client.pages.create,
                parent={"type": "data_source_id", "data_source_id": self._data_source_id},
                properties=properties,
            )
            return "created"
        self._call(self._client.pages.update, page_id, properties=properties)
        return "updated"

    @staticmethod
    def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except NotionClientErrorBase as exc:
            code = getattr(exc.code, "value", exc.code)
            status = getattr(exc, "status", None)
            message = f"Notion API error ({status or 'no status'}, {code}): {exc}"
            if code in ("object_not_found", "restricted_resource"):
                message += (
                    " Make sure the integration is connected to the 'Hábitos' page"
                    " (••• > Connections) and NOTION_DATA_SOURCE_ID is right."
                )
            elif code == "unauthorized":
                message += " Check NOTION_TOKEN."
            raise NotionError(message) from exc
