import pytest

from hns.config import ConfigError, load_habitica_credentials

USER_ID = "00000000-0000-4000-8000-000000000001"
API_TOKEN = "test-token-not-real"


def test_loads_credentials_and_strips_whitespace() -> None:
    credentials = load_habitica_credentials(
        {"HABITICA_USER_ID": f" {USER_ID} ", "HABITICA_API_TOKEN": f"{API_TOKEN}\n"}
    )

    assert credentials.user_id == USER_ID
    assert credentials.api_token.get_secret_value() == API_TOKEN


def test_token_is_hidden_in_repr() -> None:
    credentials = load_habitica_credentials(
        {"HABITICA_USER_ID": USER_ID, "HABITICA_API_TOKEN": API_TOKEN}
    )

    assert API_TOKEN not in repr(credentials)
    assert API_TOKEN not in str(credentials)


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({}, "HABITICA_USER_ID, HABITICA_API_TOKEN"),
        ({"HABITICA_USER_ID": USER_ID}, "HABITICA_API_TOKEN"),
        ({"HABITICA_USER_ID": "  ", "HABITICA_API_TOKEN": API_TOKEN}, "HABITICA_USER_ID"),
    ],
)
def test_missing_variables_are_named_exactly(env: dict[str, str], missing: str) -> None:
    with pytest.raises(ConfigError, match=f"variable\\(s\\): {missing}\\."):
        load_habitica_credentials(env)
