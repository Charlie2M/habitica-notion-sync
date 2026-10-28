import json
import logging
from collections.abc import Iterator
from datetime import date

import httpx
import pytest
import respx
from notion_client import Client

from hns.notion_sync import (
    NotionError,
    NotionSync,
    build_properties,
    day_title,
    describe_properties,
    expected_schema,
)
from hns.summary import DailyStatus, DaySummary

NOTION_API = "https://api.notion.com/v1"
DATA_SOURCE_ID = "ds-123"
TOKEN = "test-notion-token-not-real"
COLUMNS = ["Gym", "Agua", "Caminata"]

SUMMARY = DaySummary(
    day=date(2026, 10, 27),
    completed=6,
    scheduled=8,
    dailies={"Gym": DailyStatus.DONE, "Agua": DailyStatus.MISSED},
    habits_up=2,
    habits_down=1,
    applications=1,
    level=6,
    health=39.04,
    gold=187.2567,
    gym_streak=7,
)


# --- Pure property building ---------------------------------------------------


@pytest.mark.parametrize(
    ("day", "title"),
    [
        (date(2026, 10, 27), "Mar 27 oct"),
        (date(2026, 10, 28), "Mié 28 oct"),
        (date(2026, 11, 1), "Dom 1 nov"),
        (date(2027, 1, 2), "Sáb 2 ene"),
    ],
)
def test_day_title(day: date, title: str) -> None:
    assert day_title(day) == title


def test_build_properties_with_stats() -> None:
    properties = build_properties(SUMMARY, COLUMNS, include_stats=True)

    assert properties == {
        "Día": {"title": [{"text": {"content": "Mar 27 oct"}}]},
        "Fecha": {"date": {"start": "2026-10-27"}},
        "Cumplidas": {"number": 6},
        "Programadas": {"number": 8},
        "Habits +": {"number": 2},
        "Habits -": {"number": 1},
        "Aplicaciones": {"number": 1},
        "Gym": {"select": {"name": "✅ Cumplida"}},
        "Agua": {"select": {"name": "❌ Fallada"}},
        "Caminata": {"select": None},  # no data that day: cleared
        "Nivel": {"number": 6},
        "Vida": {"number": 39.0},
        "Oro": {"number": 187.26},
        "Racha gym": {"number": 7},
    }


def test_build_properties_without_stats() -> None:
    properties = build_properties(SUMMARY, COLUMNS, include_stats=False)

    assert not {"Nivel", "Vida", "Oro", "Racha gym"} & properties.keys()


def test_never_writes_notes_or_formula() -> None:
    properties = build_properties(SUMMARY, COLUMNS, include_stats=True)

    assert "Notas" not in properties
    assert "Cumplimiento (%)" not in properties
    assert "Notas" not in expected_schema(COLUMNS)


def test_not_due_label() -> None:
    summary = DaySummary(**{**SUMMARY.__dict__, "dailies": {"Caminata": DailyStatus.NOT_DUE}})

    properties = build_properties(summary, ["Caminata"], include_stats=False)

    assert properties["Caminata"] == {"select": {"name": "➖ No tocaba"}}


def test_describe_properties() -> None:
    properties = build_properties(SUMMARY, ["Gym", "Caminata"], include_stats=False)

    assert describe_properties(properties) == [
        "Día: Mar 27 oct",
        "Fecha: 2026-10-27",
        "Cumplidas: 6",
        "Programadas: 8",
        "Habits +: 2",
        "Habits -: 1",
        "Aplicaciones: 1",
        "Gym: ✅ Cumplida",
        "Caminata: (empty)",
    ]


# --- API calls (mocked with respx) ---------------------------------------------


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=NOTION_API, assert_all_called=False) as router:
        yield router


@pytest.fixture
def sync() -> Iterator[NotionSync]:
    with Client(auth=TOKEN, retry=False, logger=logging.getLogger("notion_client")) as client:
        yield NotionSync(client, DATA_SOURCE_ID)


def query_result(*page_ids: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "results": [{"object": "page", "id": pid} for pid in page_ids],
            "has_more": False,
            "next_cursor": None,
        },
    )


def notion_error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"object": "error", "status": status, "code": code, "message": message}
    )


def test_upsert_creates_a_row_when_the_date_is_new(sync: NotionSync, api: respx.MockRouter) -> None:
    query = api.post(f"/data_sources/{DATA_SOURCE_ID}/query").mock(return_value=query_result())
    create = api.post("/pages").mock(return_value=httpx.Response(200, json={"id": "new-page"}))
    properties = build_properties(SUMMARY, COLUMNS, include_stats=False)

    assert sync.upsert(SUMMARY.day, properties) == "created"

    assert json.loads(query.calls.last.request.content)["filter"] == {
        "property": "Fecha",
        "date": {"equals": "2026-10-27"},
    }
    body = json.loads(create.calls.last.request.content)
    assert body["parent"] == {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID}
    assert body["properties"] == properties
    assert create.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_upsert_updates_the_existing_row(sync: NotionSync, api: respx.MockRouter) -> None:
    api.post(f"/data_sources/{DATA_SOURCE_ID}/query").mock(return_value=query_result("page-1"))
    create = api.post("/pages")
    update = api.patch("/pages/page-1").mock(return_value=httpx.Response(200, json={}))
    properties = build_properties(SUMMARY, COLUMNS, include_stats=False)

    assert sync.upsert(SUMMARY.day, properties) == "updated"

    assert not create.called
    assert json.loads(update.calls.last.request.content)["properties"] == properties


def test_duplicate_rows_update_the_first_and_warn(
    sync: NotionSync, api: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    api.post(f"/data_sources/{DATA_SOURCE_ID}/query").mock(
        return_value=query_result("page-1", "page-2")
    )
    update = api.patch("/pages/page-1").mock(return_value=httpx.Response(200, json={}))

    assert sync.upsert(SUMMARY.day, {}) == "updated"
    assert update.called
    assert "Found 2 rows for 2026-10-27" in caplog.text


def test_check_schema_passes_for_the_expected_properties(
    sync: NotionSync, api: respx.MockRouter
) -> None:
    schema = expected_schema(COLUMNS)
    properties = {name: {"type": kind} for name, kind in schema.items()}
    properties["Notas"] = {"type": "rich_text"}
    api.get(f"/data_sources/{DATA_SOURCE_ID}").mock(
        return_value=httpx.Response(200, json={"object": "data_source", "properties": properties})
    )

    sync.check_schema(schema)


def test_check_schema_names_missing_and_mistyped_properties(
    sync: NotionSync, api: respx.MockRouter
) -> None:
    schema = expected_schema(COLUMNS)
    properties = {name: {"type": kind} for name, kind in schema.items() if name != "Agua"}
    properties["Gym"] = {"type": "checkbox"}
    api.get(f"/data_sources/{DATA_SOURCE_ID}").mock(
        return_value=httpx.Response(200, json={"object": "data_source", "properties": properties})
    )

    with pytest.raises(NotionError) as exc:
        sync.check_schema(schema)

    assert "'Gym' (expected select, found checkbox)" in str(exc.value)
    assert "'Agua' (expected select, found nothing)" in str(exc.value)


def test_not_found_explains_the_page_connection(sync: NotionSync, api: respx.MockRouter) -> None:
    api.get(f"/data_sources/{DATA_SOURCE_ID}").mock(
        return_value=notion_error(404, "object_not_found", "Could not find data source.")
    )

    with pytest.raises(NotionError) as exc:
        sync.check_schema({})

    message = str(exc.value)
    assert "404, object_not_found" in message
    assert "connected to the 'Hábitos' page" in message


def test_unauthorized_hints_at_the_token_without_showing_it(
    sync: NotionSync, api: respx.MockRouter
) -> None:
    api.post(f"/data_sources/{DATA_SOURCE_ID}/query").mock(
        return_value=notion_error(401, "unauthorized", "API token is invalid.")
    )

    with pytest.raises(NotionError) as exc:
        sync.upsert(SUMMARY.day, {})

    assert "Check NOTION_TOKEN" in str(exc.value)
    assert TOKEN not in str(exc.value)
