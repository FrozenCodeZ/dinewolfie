"""Nutrislice must not be contacted unless live fetching is switched on, and must never be
contacted again after a refusal or rate limit (no network: requests.get is replaced)."""
from datetime import date

import pytest

import config
from src import fetch_menu
from src.fetch_menu import FetchError, Station


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return {"days": []}


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Record every attempted request instead of sending it."""
    sent = []
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "POLITE_DELAY_S", 0)
    monkeypatch.setattr(fetch_menu, "_stopped_reason", None)
    monkeypatch.setattr(fetch_menu.requests, "get", lambda url, **kw: sent.append(url) or FakeResponse(200))
    return sent


def test_live_fetch_is_off_by_default():
    assert config.LIVE_FETCH is False
    assert config.DATA_MODE == "sample"


def test_no_request_when_live_fetch_is_off(calls, monkeypatch):
    monkeypatch.setattr(config, "LIVE_FETCH", False)
    with pytest.raises(FetchError, match="turned off"):
        fetch_menu._get_json("https://example.invalid/menu")
    station = Station("West", 1, "dine-in-grill", "Grill")
    with pytest.raises(FetchError):
        fetch_menu.fetch_station_week(station, date.today())
    assert calls == []


@pytest.mark.parametrize("status,headers", [(429, {}), (403, {}), (401, {}), (503, {"Retry-After": "30"})])
def test_rate_limit_stops_all_further_requests(calls, monkeypatch, status, headers):
    monkeypatch.setattr(config, "LIVE_FETCH", True)
    monkeypatch.setattr(fetch_menu.requests, "get",
                        lambda url, **kw: calls.append(url) or FakeResponse(status, headers))
    with pytest.raises(FetchError, match="Stopped all fetching"):
        fetch_menu._get_json("https://example.invalid/1")
    assert fetch_menu.fetching_stopped()
    for n in range(2, 6):  # every later call refuses without touching the network
        with pytest.raises(FetchError, match="Not contacting Nutrislice again"):
            fetch_menu._get_json(f"https://example.invalid/{n}")
    assert calls == ["https://example.invalid/1"]


def test_hall_fetch_stops_after_first_rate_limit(calls, monkeypatch):
    monkeypatch.setattr(config, "LIVE_FETCH", True)
    stations = [Station("West", i, f"station-{i}", f"Station {i}") for i in range(5)]
    monkeypatch.setattr(fetch_menu, "list_stations", lambda hall, offline=False: stations)
    monkeypatch.setattr(fetch_menu.requests, "get", lambda url, **kw: calls.append(url) or FakeResponse(429))
    result = fetch_menu.fetch_hall_day("West", date.today())
    assert len(calls) == 1, "only the first station may be requested"
    assert len(result["errors"]) == 5
