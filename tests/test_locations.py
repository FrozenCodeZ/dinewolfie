"""Locations beyond East and West (Roth Cafe and the rest), discovered from Nutrislice.

The fake server below answers like Nutrislice: a schools list with East, West, two Roth Cafe
concepts listed as their own locations (as SBU's site describes them), a Starbucks elsewhere, and
a closed location. Every station's week is built from real West grill rows."""
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
    {"id": 6401, "name": "Smash n' Shake", "slug": "smash-n-shake",
     "active_menu_types": [{"id": 900, "slug": "smash-n-shake-menu", "name": "Smash n' Shake"}]},
    {"id": 6404, "name": "Fuze at Roth", "slug": "fuze",
     "active_menu_types": [{"id": 902, "slug": "fuze-bowls", "name": "Bowls"}]},
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
        offset = int(url.split("/menu-type/")[1].split("/")[0]) * 10_000_000  # each station: its own food ids
        rows = [{**r, "food": {**r["food"], "id": r["food"]["id"] + offset}} if r.get("food") else r
                for r in RAW["dine-in-grill"]["menu_items"]]
        return Resp({"days": [{"date": DAY.isoformat(), "menu_items": rows}]})

    monkeypatch.setattr(config, "LIVE_FETCH", True)
    monkeypatch.setattr(config, "DATA_MODE", "live")
    monkeypatch.setattr(config, "POLITE_DELAY_S", 0)
    monkeypatch.setattr(fetch_menu, "_stopped_reason", None)
    monkeypatch.setattr(fetch_menu.requests, "get", fake_get)
    return sent


def test_every_open_location_is_discovered(nutrislice):
    found = {loc.key: loc for loc in locations.all_locations()}
    assert set(found) == {"East", "West", "Smash n' Shake", "Fuze at Roth", "Starbucks at the Library",
                          "Roth Cafe"}  # the closed one is skipped
    assert found["East"].kind == "dine_in" and found["East"].paid_with == "meal swipe"
    roth = found["Roth Cafe"]
    assert roth.kind == "retail" and roth.paid_with == "dining dollars"
    assert roth.members == (6401, 6404)  # concepts gathered even without "Roth" in the name
    assert 6402 not in roth.members      # a Starbucks elsewhere on campus is not Roth
    assert locations.get("roth").key == "Roth Cafe" and locations.get("Roth Café").key == "Roth Cafe"
    assert locations.get("smash").key == "Smash n' Shake"
    assert locations.get("West Side Dine-In").key == "West"  # old behavior kept
    assert locations.for_item_id("LROTH-123").key == "Roth Cafe"
    assert locations.for_item_id("L6401-123").key == "Smash n' Shake"
    assert locations.for_item_id("E123").key == "East"
    assert [loc.key for loc in locations.named_in("lunch at Roth today", locations.all_locations())] == \
        ["Roth Cafe"]  # the group, not each of its concepts again
    with pytest.raises(ValueError, match="Unknown location"):
        locations.get("Hogwarts")


def test_roth_cafe_menu_loads_every_concept_and_passes_the_checker(nutrislice, prefs):
    prefs()
    menu = T.get_menu("Roth", "lunch", DAY.isoformat())
    assert menu["status"] == "ok" and menu["hall"] == "Roth Cafe"
    assert set(menu["stations"]) == {"Smash n' Shake", "Fuze at Roth: Bowls"}
    ids = [i["id"] for items in menu["stations"].values() for i in items]
    assert ids and all(i.startswith("LROTH-") for i in ids)
    assert sum("/weeks/school/6401/menu-type/900/" in url for url in nutrislice) == 1
    assert sum("/weeks/school/6404/menu-type/902/" in url for url in nutrislice) == 1

    result = T.submit_plan({"date": DAY.isoformat(), "headline": "Roth lunch",
                            "meals": [{"meal": "lunch", "hall": "roth", "items": [{"id": ids[0]}], "reason": "x"}]})
    assert result["status"] == "accepted", result.get("problems")
    assert result["plan"]["meals"][0]["hall"] == "Roth Cafe"

    T.clear_cache()
    T.get_menu("Roth", "dinner", DAY.isoformat())  # second load comes from the disk cache
    assert sum("/weeks/school/6401/" in url for url in nutrislice) == 1


def test_a_single_roth_concept_works_too(nutrislice, prefs):
    prefs()
    menu = T.get_menu("Smash n' Shake", "lunch", DAY.isoformat())
    assert menu["status"] == "ok"
    assert all(i["id"].startswith("L6401-") for items in menu["stations"].values() for i in items)


def test_a_failed_location_list_is_retried_not_remembered(monkeypatch):
    calls = {"n": 0}

    def flaky(offline=False):
        calls["n"] += 1
        raise fetch_menu.FetchError("network hiccup")

    monkeypatch.setattr(fetch_menu, "load_schools", flaky)
    assert [loc.key for loc in locations.all_locations(offline=False)] == ["East", "West"]
    assert locations.last_error == "network hiccup"
    locations.all_locations(offline=False)
    assert calls["n"] == 1  # not hammered on every call...
    monkeypatch.setattr(locations, "RETRY_AFTER_FAILURE_S", 0)
    locations.all_locations(offline=False)
    assert calls["n"] == 2  # ...but tried again later


def test_extra_locations_join_searches_and_scouting(nutrislice, prefs):
    prefs(extra_locations=["Roth Cafe", "Somewhere That Closed"])
    assert T.considered() == ["East", "West", "Roth Cafe"]
    search = T.search_items("lunch", date=DAY.isoformat(), limit=40)
    assert "Roth Cafe" in search["hall_status"]
    scout = T.scout_meal("lunch", DAY.isoformat())
    assert set(scout["halls"]) == {"East", "West", "Roth Cafe"}


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
    assert "LUNCH at Roth Cafe" in text and "dining dollars" in text
    assert "LUNCH at Smash n' Shake" not in text  # scouted once, as the group
