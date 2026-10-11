"""Regression tests for the October 2026 code review (one test or more per finding, no network)."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from src import agent as A
from src import briefing, fetch_menu, locations, storage
from src import groq_agent as G
from src import tools as T
from tests.conftest import SAMPLE_DATE
from tests.test_hosting import _tool_call
from tests.test_providers import FakeCompat

DAY = T.resolve_date(SAMPLE_DATE)


def _in_run(fn, *args, **kwargs):
    run = A.AgentRun(goal="x")
    events = []
    token = A._current.set({"run": run, "on_event": events.append})
    try:
        return fn(*args, **kwargs), run, events
    finally:
        A._current.reset(token)


# --- 1. a place whose items have no nutrition doesn't crash the scout -----------------------------

def test_scout_keeps_items_without_nutrition_instead_of_crashing(prefs, monkeypatch):
    prefs()
    real = T._menu

    def menu(hall, meal, day):
        items, meta = real(hall, meal, day)
        if hall == "West":  # a place that lists no calories at all
            items = [{**i, "calories": None, "protein_g": None} for i in items]
        return items, meta
    monkeypatch.setattr(T, "_menu", menu)
    scout = T.scout_meal("lunch", SAMPLE_DATE)
    assert scout["halls"]["East"]["status"] == "ok"
    west = scout["halls"]["West"]
    assert west["status"] == "ok" and west["items"] and all(r["protein_g"] is None for r in west["items"])
    assert "West" in scout["summary"] and "no nutrition listed" in scout["summary"]
    text = briefing.render(T.read_prefs(), [], [scout])
    assert "LUNCH at West" in text and "LUNCH at East" in text


def test_one_broken_place_does_not_sink_the_others(prefs, monkeypatch):
    prefs()
    real = T._menu
    monkeypatch.setattr(T, "_menu", lambda h, m, d: (_ for _ in ()).throw(KeyError("station")) if h == "West"
                        else real(h, m, d))
    scout = T.scout_meal("lunch", SAMPLE_DATE)
    assert scout["status"] == "ok" and scout["halls"]["East"]["status"] == "ok"
    assert scout["halls"]["West"]["status"] == "error"


# --- 2. a failed save doesn't throw away a good plan ----------------------------------------------

class BrokenSaves(storage.MemoryStore):
    def save_history(self, history):
        raise RuntimeError("Couldn't save to your account (paused project).")

    def save_prefs(self, prefs):
        raise RuntimeError("Couldn't save to your account (paused project).")


def test_plan_is_accepted_even_if_history_cannot_be_saved(prefs):
    prefs()
    pick = T.scout_meal("dinner", SAMPLE_DATE)["halls"]["East"]["items"][0]
    storage.use(BrokenSaves())
    result, is_error = A.call_tool("submit_plan", {"plan": {
        "date": SAMPLE_DATE, "headline": "x",
        "meals": [{"meal": "dinner", "hall": "East", "items": [{"id": pick["id"]}], "reason": "r"}]}})
    assert not is_error and result["status"] == "accepted"
    assert "couldn't be saved" in result["summary"] and result["warning"]


# --- 3, 4, 5, 8. memory values are checked, merged and kept -----------------------------------------

def test_update_prefs_normalizes_values_the_app_can_show(prefs):
    prefs()
    res = T.update_prefs({"favorite_hall": "west", "meals_to_plan": "Breakfast, brunch", "cheat_days": "fri",
                          "treats": "Often", "daily_calorie_goal": "1,800 kcal", "allergies": "Peanut, dairy",
                          "diet": "Gluten free"})
    assert res["status"] == "ok", res
    saved = T.read_prefs()
    assert saved["favorite_hall"] == "West" and saved["meals_to_plan"] == ["breakfast", "brunch"]
    assert saved["cheat_days"] == ["Friday"] and saved["treats"] == "often"
    assert saved["daily_calorie_goal"] == 1800  # finding 8: not 1
    assert saved["allergies"] == ["peanuts", "milk"] and saved["diet"] == ["gluten_free"]


@pytest.mark.parametrize("changes", [{"favorite_hall": "Hogwarts"}, {"meals_to_plan": "second breakfast"},
                                     {"treats": "lots"}, {"cheat_days": "someday"},
                                     {"daily_protein_goal_g": "a lot"}, {"daily_calorie_goal": 90000}])
def test_update_prefs_refuses_values_it_cannot_use_and_saves_nothing(prefs, changes):
    before = prefs(favorite_hall="East")
    res = T.update_prefs({**changes, "name": "Sam"})
    assert res["status"] == "error" and "Nothing was saved" in res["error"]
    assert T.read_prefs()["name"] == before["name"]


def test_update_prefs_adds_to_lists_instead_of_replacing_them(prefs):
    prefs(allergies=["peanuts"], never_eat=["olives"])
    T.update_prefs({"allergies": "shellfish", "never_eat": ["Olives", "anchovies"]})
    saved = T.read_prefs()
    assert saved["allergies"] == ["peanuts", "shellfish"]  # finding 5: peanuts is still blocked
    assert saved["never_eat"] == ["olives", "anchovies"]
    T.update_prefs(remove={"never_eat": ["Olives"]})
    assert T.read_prefs()["never_eat"] == ["anchovies"]


def test_the_agent_cannot_remove_an_allergy(prefs):
    """Security review: a misunderstanding must never unblock an allergen (the user can, in the form)."""
    prefs(allergies=["peanuts"])
    res = T.update_prefs(remove={"allergies": ["peanuts"]})
    assert res["status"] == "error" and "memory form" in res["error"]
    assert T.read_prefs()["allergies"] == ["peanuts"]


@pytest.mark.parametrize("said,saved", [("peanuts and shellfish", ["peanuts", "shellfish"]),
                                        ("nut allergy", ["nut"]), ("eggs & dairy", ["egg", "milk"]),
                                        (["allergic to soya"], ["soy"])])
def test_allergies_are_saved_as_names_the_checker_can_enforce(prefs, said, saved):
    prefs()
    assert T.update_prefs({"allergies": said})["status"] == "ok"
    assert T.read_prefs()["allergies"] == saved
    assert T.allergen_codes(saved) - {"nut"}  # every saved name maps to codes the menus use


def test_an_allergy_menus_dont_label_is_also_blocked_by_name(prefs):
    prefs(never_eat=["olives"])
    res = T.update_prefs({"allergies": "mustard"})
    assert res["status"] == "ok" and "double-check" in res["summary"]
    saved = T.read_prefs()
    assert saved["allergies"] == ["mustard"] and saved["never_eat"] == ["olives", "mustard"]


def test_only_diets_the_checker_understands_are_saved(prefs):
    prefs()
    assert T.update_prefs({"diet": "keto"})["status"] == "error"
    assert T.update_prefs({"diet": "plant-based"})["status"] == "ok"
    assert T.read_prefs()["diet"] == ["vegan"]


def test_places_can_be_added_next_to_a_stale_one_and_removed_by_nickname(prefs, monkeypatch):
    from tests.test_locations import SCHOOLS
    monkeypatch.setattr(locations, "all_locations", lambda offline=None: locations.from_schools(SCHOOLS))
    prefs(extra_locations=["Somewhere Closed Today"])
    assert T.update_prefs({"extra_locations": ["roth"]})["status"] == "ok"  # no crash on the stale one
    assert T.read_prefs()["extra_locations"] == ["Somewhere Closed Today", "Roth Cafe"]
    assert T.update_prefs(remove={"extra_locations": ["Roth"]})["status"] == "ok"
    assert T.read_prefs()["extra_locations"] == ["Somewhere Closed Today"]
    missing = T.update_prefs(remove={"extra_locations": ["Starbucks at the Library"]})
    assert missing["status"] == "error" and "isn't saved" in missing["error"]  # no false "removed"


def test_notes_stay_whole(prefs):
    prefs(notes="Training for a marathon, needs carbs")
    assert T.read_prefs()["notes"] == ["Training for a marathon, needs carbs"]
    T.update_prefs({"notes": "Prefers spicy food, not too hot"})
    assert T.read_prefs()["notes"] == ["Training for a marathon, needs carbs", "Prefers spicy food, not too hot"]


def test_memory_entries_are_size_limited(prefs):
    prefs()
    assert T.update_prefs({"never_eat": "x" * 200})["status"] == "error"
    assert T.update_prefs({"favorites": [f"dish {i}" for i in range(T.MAX_ENTRIES + 1)]})["status"] == "error"


def test_read_prefs_repairs_bad_values_already_saved(prefs):
    prefs(favorite_hall="west", meals_to_plan=["Breakfast", "second breakfast"], treats="lots",
          cheat_days=["fri", "nope"], daily_protein_goal_g="110 g", diet="vegan")
    p = T.read_prefs()
    assert p["favorite_hall"] == "West" and p["meals_to_plan"] == ["breakfast"]
    assert p["treats"] == "sometimes" and p["cheat_days"] == ["Friday"]
    assert p["daily_protein_goal_g"] == 110 and p["diet"] == ["vegan"]


def _app():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60)


def test_memory_form_survives_odd_saved_values_and_keeps_them(prefs, monkeypatch):
    monkeypatch.setenv("DINEWOLFIE_PREFS", str(config.PREFS_FILE))
    raw = prefs(favorite_hall="Roth Cafe", allergies=["peanut", "mustard"], diet=["keto"],
                meals_to_plan=["brunch", "dinner"], extra_locations=["Somewhere Closed Today"])
    at = _app()
    at.run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Save memory").click().run()
    assert not at.exception, at.exception
    saved = json.loads(config.PREFS_FILE.read_text(encoding="utf-8"))
    assert saved["allergies"] == ["peanuts", "mustard"]  # finding 4: nothing silently dropped
    assert saved["diet"] == ["keto"] and saved["favorite_hall"] == raw["favorite_hall"]
    assert saved["meals_to_plan"] == ["brunch", "dinner"]
    assert saved["extra_locations"] == ["Somewhere Closed Today"]


# --- 6, 7, 15. the OpenAI-compatible loop ----------------------------------------------------------

def _rate_limited(retry_after="0"):
    return type("RateLimitError", (Exception,), {
        "status_code": 429, "response": SimpleNamespace(headers={"retry-after": retry_after})})("rate limit")


def test_zero_retry_after_cannot_loop_forever(prefs, monkeypatch):
    prefs()
    monkeypatch.setattr(config, "FALLBACK_TO_BUILTIN", True)
    fake = FakeCompat([_rate_limited() for _ in range(200)])
    slept = []
    run = A.AgentRun(goal="x", engine="groq")
    token = A._current.set({"run": run, "on_event": None})
    try:
        with pytest.raises(A.AIBusy):
            G.run_groq(run, "dinner", DAY, None, A.Engine("groq", "gsk-a", "openai/gpt-oss-120b"),
                       client=fake, sleep=slept.append)
    finally:
        A._current.reset(token)
    assert len(fake.requests) <= 20


def test_one_keys_rate_limit_does_not_block_another_key(prefs):
    prefs()
    G._cool("openai/gpt-oss-120b", 60, "groq", key="gsk-visitor")
    assert G.next_model("openai/gpt-oss-120b", G.time.monotonic(), "groq", key="gsk-visitor")[0] != \
        "openai/gpt-oss-120b"
    assert G.next_model("openai/gpt-oss-120b", G.time.monotonic(), "groq", key="gsk-owner")[0] == \
        "openai/gpt-oss-120b"
    assert not any("gsk-visitor" in k for k in G._cooldown)  # keys are never stored, only a fingerprint


def test_fast_mode_stops_after_the_fast_turn_limit(prefs):
    prefs()
    search = {"tool_calls": [_tool_call(1, "search_items", {"meal": "dinner", "hall": "East", "limit": 3})]}
    fake = FakeCompat([search] * 50)
    run = A.AgentRun(goal="x", engine="groq")
    token = A._current.set({"run": run, "on_event": None})
    try:
        G.run_groq(run, "dinner", DAY, None, A.Engine("groq", "gsk-a", "openai/gpt-oss-120b"), client=fake)
    finally:
        A._current.reset(token)
    assert len(fake.requests) == config.FAST_MAX_TURNS and run.error


# --- 9. "today" is today at Stony Brook ---------------------------------------------------------------

def test_today_is_new_york_time_not_the_servers(monkeypatch):
    from zoneinfo import ZoneInfo
    expected = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    assert abs((config.now() - expected).total_seconds()) < 60
    monkeypatch.setattr(config, "now", lambda: datetime(2026, 10, 9, 21, 30))  # 9:30 PM in New York
    assert T.resolve_date("today").isoformat() == "2026-10-09"
    assert T.resolve_date("tomorrow").isoformat() == "2026-10-10"


# --- 12. "Roth" means Roth -----------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [("Smash n' Shake", True), ("Fuze at Roth", True), ("Roth Café", True),
                                           ("Savor", True), ("Brothers Pizza", False), ("Bone Broth Bar", False),
                                           ("Savory Crepes", False), ("Popeyes", True)])
def test_roth_concepts_match_whole_words_only(name, expected):
    assert locations.is_roth(name) is expected


# --- 13. a Claude rate limit reported as a result still falls back -------------------------------------

def test_claude_rate_limit_result_falls_back_to_the_builtin_planner(prefs, monkeypatch):
    prefs()
    monkeypatch.setattr(config, "FALLBACK_TO_BUILTIN", True)

    async def limited(run, goal, plan_date, resume, engine):
        run.error = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'
    monkeypatch.setattr(A, "_run_claude", limited)
    events = []
    run = asyncio.run(A.run_agent_async("plan dinner", DAY, events.append, None, A.Engine("claude")))
    assert run.plan is not None and not run.error
    assert any(e["type"] == "adaptation" and "built-in" in e["change"] for e in events)


def test_other_claude_errors_are_explained(prefs, monkeypatch):
    prefs()

    async def broken(run, goal, plan_date, resume, engine):
        run.error = "Invalid API key · Fix external API key"
    monkeypatch.setattr(A, "_run_claude", broken)
    run = asyncio.run(A.run_agent_async("plan dinner", DAY, None, None, A.Engine("claude")))
    assert run.plan is None and "API key was rejected" in run.error


# --- 14. one cache file per Nutrislice station ----------------------------------------------------------

def test_station_cache_is_shared_by_a_group_and_its_members():
    start = fetch_menu.week_start(DAY)
    in_group = fetch_menu.Station("Roth Cafe", 900, "smash-menu", "Smash n' Shake", 6401)
    alone = fetch_menu.Station("Smash n' Shake", 900, "smash-menu", "Smash n' Shake", 6401)
    other = fetch_menu.Station("Fuze at Roth", 900, "smash-menu", "Bowls", 6404)
    assert fetch_menu.station_cache_path(in_group, start) == fetch_menu.station_cache_path(alone, start)
    assert fetch_menu.station_cache_path(in_group, start) != fetch_menu.station_cache_path(other, start)
    west = fetch_menu.Station("West", 2, "dine-in-grill", "Grill")  # East/West: id from config
    assert "6334" in str(fetch_menu.station_cache_path(west, start))


# --- improvements from the same review --------------------------------------------------------------

def test_owner_key_runs_fast_mode_only(monkeypatch):
    monkeypatch.setattr(config, "APP_URL", config.APP_URL)  # restored after the test
    at = _app()
    at.secrets["hosted"] = True
    at.secrets["share_keys_with_everyone"] = True
    at.secrets["GROQ_API_KEY"] = "gsk-owner"
    at.secrets["app_url"] = "https://dinewolfie.example/"
    at.run()
    at.sidebar.radio(key="provider").set_value("Groq").run()
    assert not at.exception
    assert at.selectbox(key="model_groq").disabled and at.radio(key="speed").disabled
    assert config.APP_URL == "https://dinewolfie.example/"  # notifications open this site


def test_retail_menus_count_for_every_meal(monkeypatch, prefs):
    prefs()
    raw = [{"date": "2026-10-01", "menu_items": [
        {"is_section_title": True, "text": "Burgers"},
        {"food": {"id": 1, "name": "Smash Burger", "rounded_nutrition_info": {"calories": 600, "g_protein": 30},
                  "icons": {"food_icons": []}}, "serving_size_info": {}}]}]
    from src.parse_menu import parse_station_day
    shop = parse_station_day("Smash n' Shake", "smash-menu", "Smash n' Shake", raw[0], "L6401-",
                             config.RETAIL_DEFAULT_MEALS)
    hall = parse_station_day("West", "dine-in-grill", "Grill", raw[0], "W")
    assert shop and set(shop[0]["meals"]) >= {"breakfast", "lunch", "dinner"}
    assert hall and hall[0]["meals"] == config.DEFAULT_MEALS


def test_a_fresh_cached_menu_is_read_without_waiting_for_the_network_lock():
    import threading
    station = fetch_menu.Station("West", 2, "dine-in-grill", "Grill")
    path = fetch_menu.station_cache_path(station, fetch_menu.week_start(DAY))
    fetch_menu._write_cache(path, "u", {"days": []})
    taken, release = threading.Event(), threading.Event()

    def hold():  # another visitor's slow Nutrislice request
        with fetch_menu._fetch_lock:
            taken.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    taken.wait(5)
    try:
        data, source = fetch_menu.fetch_station_week(station, DAY)
    finally:
        release.set()
        worker.join()
    assert source == "cache" and data == {"days": []}


def test_builtin_planner_picks_at_most_one_treat_per_meal():
    from src import planner
    rows = [{"id": f"T{i}", "name": f"Pudding {i}", "protein_g": 6, "calories": 150, "treat": True} for i in range(3)]
    rows.append({"id": "E1", "name": "Egg Whites", "protein_g": 12, "calories": 60})
    picks = planner._pick(rows, 40, None, set(), allow_treat=True)
    assert sum(1 for c in picks if c["_row"].get("treat")) == 1
