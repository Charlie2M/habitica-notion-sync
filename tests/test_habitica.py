from collections.abc import Iterator

import httpx
import pytest
import respx
from pydantic import SecretStr

from hns.config import HabiticaCredentials
from hns.habitica import BASE_URL, HabiticaClient, HabiticaError

USER_ID = "00000000-0000-4000-8000-000000000001"
API_TOKEN = "test-token-not-real"


class FakeTime:
    """Deterministic replacement for time.monotonic / time.sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def client(fake_time: FakeTime) -> Iterator[HabiticaClient]:
    credentials = HabiticaCredentials(user_id=USER_ID, api_token=SecretStr(API_TOKEN))
    with HabiticaClient(
        credentials, min_interval=30, sleep=fake_time.sleep, clock=fake_time.clock
    ) as client:
        yield client


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


def test_sends_auth_and_client_headers(client: HabiticaClient, api: respx.MockRouter) -> None:
    route = api.get("/tasks/user").mock(return_value=ok([]))

    client.get_tasks()

    headers = route.calls.last.request.headers
    assert headers["x-api-user"] == USER_ID
    assert headers["x-api-key"] == API_TOKEN
    assert headers["x-client"] == f"{USER_ID}-HabiticaNotionSync"


def test_get_tasks_returns_data_field(client: HabiticaClient, api: respx.MockRouter) -> None:
    tasks = [{"id": "t1", "type": "daily"}, {"id": "t2", "type": "habit"}]
    api.get("/tasks/user").mock(return_value=ok(tasks))

    assert client.get_tasks() == tasks


def test_get_user_requests_only_needed_fields(
    client: HabiticaClient, api: respx.MockRouter
) -> None:
    route = api.get("/user").mock(return_value=ok({"stats": {"lvl": 10}}))

    assert client.get_user() == {"stats": {"lvl": 10}}
    assert route.calls.last.request.url.params["userFields"] == "stats,preferences,lastCron"


def test_waits_min_interval_between_requests(
    client: HabiticaClient, api: respx.MockRouter, fake_time: FakeTime
) -> None:
    api.get("/tasks/user").mock(return_value=ok([]))
    api.get("/user").mock(return_value=ok({}))

    client.get_tasks()
    fake_time.now += 10
    client.get_user()

    assert fake_time.sleeps == [20]


def test_retries_429_honoring_retry_after(
    client: HabiticaClient, api: respx.MockRouter, fake_time: FakeTime
) -> None:
    api.get("/tasks/user").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "45.5"}), ok([])]
    )

    assert client.get_tasks() == []
    assert fake_time.sleeps == [45.5]


def test_retries_server_errors_with_backoff(
    client: HabiticaClient, api: respx.MockRouter, fake_time: FakeTime
) -> None:
    api.get("/tasks/user").mock(side_effect=[httpx.Response(503), httpx.Response(502), ok([])])

    assert client.get_tasks() == []
    # Backoff of 2 s and 4 s, each topped up to the 30 s spacing between requests.
    assert fake_time.sleeps == [2, 28, 4, 26]


def test_retries_network_errors(client: HabiticaClient, api: respx.MockRouter) -> None:
    api.get("/tasks/user").mock(side_effect=[httpx.ConnectError("boom"), ok([])])

    assert client.get_tasks() == []


def test_gives_up_after_max_attempts(client: HabiticaClient, api: respx.MockRouter) -> None:
    route = api.get("/tasks/user").mock(return_value=httpx.Response(503))

    with pytest.raises(HabiticaError, match="HTTP 503"):
        client.get_tasks()
    assert route.call_count == 4


def test_network_error_on_last_attempt_raises(
    client: HabiticaClient, api: respx.MockRouter
) -> None:
    api.get("/tasks/user").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(HabiticaError, match="Could not reach Habitica"):
        client.get_tasks()


def test_unauthorized_error_is_readable_and_hides_token(
    client: HabiticaClient, api: respx.MockRouter
) -> None:
    api.get("/user").mock(
        return_value=httpx.Response(
            401,
            json={"success": False, "error": "NotAuthorized", "message": "Missing auth."},
        )
    )

    with pytest.raises(HabiticaError) as exc:
        client.get_user()

    message = str(exc.value)
    assert "HTTP 401 (NotAuthorized: Missing auth.)" in message
    assert "HABITICA_API_TOKEN" in message
    assert API_TOKEN not in message


def test_success_false_in_body_is_an_error(client: HabiticaClient, api: respx.MockRouter) -> None:
    api.get("/tasks/user").mock(return_value=httpx.Response(200, json={"success": False}))

    with pytest.raises(HabiticaError, match="HTTP 200"):
        client.get_tasks()


def test_non_json_error_body(client: HabiticaClient, api: respx.MockRouter) -> None:
    api.get("/user").mock(return_value=httpx.Response(404, text="<html>Not found</html>"))

    with pytest.raises(HabiticaError, match=r"/user failed with HTTP 404$"):
        client.get_user()
