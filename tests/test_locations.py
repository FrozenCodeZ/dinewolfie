"""Locations beyond East and West (Roth Food Court and the rest), discovered from Nutrislice.

The fake server below answers like Nutrislice: a schools list with East, West, Roth and a
Starbucks, and a week for Roth's one station built from real West grill rows."""
import json
from datetime import date
from pathlib import Path

import pytest

import config
from src import fetch_menu, locations
from src import tools as T

DAY = date(2026, 10, 1)
RAW = json.loads((Path(__file__).parent / "fixtures" / "west_2026-10-01_raw.json").read_text(encoding="utf-8"))
SCHOOLS = [
    {"id": 6333, "name": "East Side Dine-In", "slug": "east-side-dining",
     "active_menu_types": [{"id": 1, "slug": "dine-in-grill", "name": "Grill"}]},
    {"id": 6334, "name": "West Side Dine-In", "slug": "west-side-dining",
     "active_menu_types": [{"id": 2, "slug": "dine-in-grill", "name": "Grill"}]},
    {"id": 6401, "name": "Roth Food Court", "slug": "roth-food-court",
     "active_menu_types": [{"id": 900, "slug": "roth-grill", "name": "Roth Grill"}]},
    {"id": 6402, "name": "Starbucks at the Library", "slug": "starbucks-library",
     "active_menu_types": [{"id": 901, "slug": "starbucks", "name": "Starbucks"}]},
    {"id": 6403, "name": "Closed For Renovation", "slug": "closed", "active_menu_types": []},
]


class Resp:
    def __init__(self, data):
        self.status_code, self.headers, self._data = 200, {}, data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def nutrislice(monkeypatch):
    """Live mode against the fake server; returns the list of requested URLs."""
    sent = []

    def fake_get(url, **kw):
        sent.append(url)
        if url == config.SCHOOLS_URL:
            return Resp(SCHOOLS)
        return Resp({"days": [{"date": DAY.isoformat(), "menu_items": RAW["dine-in-grill"]["menu_items"]}]})

    monkeypatch.setattr(config, "LIVE_FETCH", True)
    monkeypatch.setattr(config, "DATA_MODE", "live")
    monkeypatch.setattr(config, "POLITE_DELAY_S", 0)
    monkeypatch.setattr(fetch_menu, "_stopped_reason", None)
    monkeypatch.setattr(fetch_menu.requests, "get", fake_get)
    return sent


def test_every_open_location_is_discovered(nutrislice):
    found = {loc.key: loc for loc in locations.all_locations()}
    assert set(found) == {"East", "West", "Roth Food Court", "Starbucks at the Library"}  # closed one skipped
    assert found["East"].kind == "dine_in" and found["East"].paid_with == "meal swipe"
    assert found["Roth Food Court"].kind == "retail" and found["Roth Food Court"].paid_with == "dining dollars"
    assert locations.get("roth").key == "Roth Food Court"
    assert locations.get("West Side Dine-In").key == "West"  # old behavior kept
    assert locations.for_item_id("L6401-123").key == "Roth Food Court"
    assert locations.for_item_id("E123").key == "East"
    assert [loc.key for loc in locations.named_in("lunch at Roth today", locations.all_locations())] == \
        ["Roth Food Court"]
    with pytest.raises(ValueError, match="Unknown location"):
        locations.get("Hogwarts")


def test_roth_menu_loads_parses_and_passes_the_checker(nutrislice, prefs):
    prefs()
    menu = T.get_menu("Roth", "lunch", DAY.isoformat())
    assert menu["status"] == "ok" and menu["hall"] == "Roth Food Court"
    ids = [i["id"] for items in menu["stations"].values() for i in items]
    assert ids and all(i.startswith("L6401-") for i in ids)
    assert sum("/weeks/school/6401/menu-type/900/" in url for url in nutrislice) == 1

    result = T.submit_plan({"date": DAY.isoformat(), "headline": "Roth lunch",
                            "meals": [{"meal": "lunch", "hall": "roth", "items": [{"id": ids[0]}], "reason": "x"}]})
    assert result["status"] == "accepted", result.get("problems")
    assert result["plan"]["meals"][0]["hall"] == "Roth Food Court"

    T.clear_cache()
    T.get_menu("Roth", "dinner", DAY.isoformat())  # second load comes from the disk cache
    assert sum("/weeks/school/6401/" in url for url in nutrislice) == 1


def test_extra_locations_join_searches_and_scouting(nutrislice, prefs):
    prefs(extra_locations=["Roth Food Court", "Somewhere That Closed"])
    assert T.considered() == ["East", "West", "Roth Food Court"]
    search = T.search_items("lunch", date=DAY.isoformat(), limit=40)
    assert "Roth Food Court" in search["hall_status"]
    scout = T.scout_meal("lunch", DAY.isoformat())
    assert set(scout["halls"]) == {"East", "West", "Roth Food Court"}


def test_sample_mode_says_when_a_place_has_no_saved_menu(prefs, monkeypatch):
    prefs()
    monkeypatch.setattr(locations, "all_locations",
                        lambda offline=None: locations.from_schools(SCHOOLS))
    res = T.get_menu("Roth", "lunch", "2026-10-01")
    assert res["status"] == "error" and "no saved sample menu" in res["error"]


def test_the_briefing_scouts_places_the_goal_names(nutrislice, prefs):
    from src import agent as A
    from src import briefing
    prefs()
    run = A.AgentRun(goal="x")
    token = A._current.set({"run": run, "on_event": None})
    try:
        text = briefing.gather(DAY, "Plan my day, and I want lunch at Roth")
    finally:
        A._current.reset(token)
    assert "LUNCH at Roth Food Court" in text and "dining dollars" in text
