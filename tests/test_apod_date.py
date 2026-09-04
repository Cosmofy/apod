from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.errors import Code, Error
from app.nasa import resolve_date


client = TestClient(app)


def test_first_apod_date_is_allowed() -> None:
    assert resolve_date("1995-06-16") == date(1995, 6, 16)


def test_no_date_returns_today_in_mountain_time() -> None:
    today = datetime.now(ZoneInfo("America/Denver")).date()

    assert resolve_date(None) == today


def test_today_as_explicit_date_is_allowed() -> None:
    today = datetime.now(ZoneInfo("America/Denver")).date()

    assert resolve_date(today.isoformat()) == today


@pytest.mark.parametrize(
    "requested_date",
    [
        "",
        "today",
        "08-17-2026",
        "2026/08/17",
        "2026-8-17",
    ],
)
def test_bad_date_shapes_are_invalid_format(requested_date: str) -> None:
    with pytest.raises(Error) as error:
        resolve_date(requested_date)

    assert error.value.code is Code.INVALID_DATE_FORMAT


def test_date_with_impossible_day_is_invalid_format() -> None:
    with pytest.raises(Error) as error:
        resolve_date("2026-08-32")

    assert error.value.code is Code.INVALID_DATE_FORMAT


def test_date_before_apod_started_is_too_early() -> None:
    with pytest.raises(Error) as error:
        resolve_date("1995-06-15")

    assert error.value.code is Code.DATE_TOO_EARLY


def test_future_date_is_not_allowed() -> None:
    with pytest.raises(Error) as error:
        resolve_date("2999-01-01")

    assert error.value.code is Code.DATE_IN_FUTURE


def test_error_handler_returns_error_json() -> None:
    response = client.get("/apod?date=1995-06-15")

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "DATE_TOO_EARLY",
            "message": "NASA's Astronomy Picture of the Day archive begins on June 16, 1995.",
        }
    }
