"""Fixes for the third code review (two /code-review runs, 22 distinct findings)."""
import pytest

from src import tools as T


def _dish(name, ingredients="", tags=(), allergens=()):
    return {"name": name, "description": "", "ingredients": ingredients, "allergens": list(allergens),
            "tags": list(tags)}


# --- food safety ----------------------------------------------------------------------------------

@pytest.mark.parametrize("allergy,dish", [("kiwi", "Kiwi Fruit Cup"), ("mustard", "Honey Mustard Chicken"),
                                          ("mustard", "Turkey Club|dijon mustard, mayo")])
def test_an_allergy_menus_dont_label_is_enforced_by_the_checker_itself(prefs, allergy, dish):
    """Not through a never_eat copy the agent could remove."""
    name, _, ingredients = dish.partition("|")
    assert T.allergy_conflicts(_dish(name, ingredients), T.allergen_codes([allergy])) == [allergy]
    prefs(allergies=[allergy], never_eat=[])
    assert T._exclusion(_dish(name, ingredients), T.allergen_codes([allergy]), [], [], [])


def test_removing_never_eat_entries_cannot_unblock_an_allergy(prefs):
    prefs()
    assert T.update_prefs({"allergies": "kiwi"})["status"] == "ok"
    T.update_prefs(remove={"never_eat": ["kiwi"]})
    codes = T.allergen_codes(T.read_prefs()["allergies"])
    assert T.allergy_conflicts(_dish("Kiwi Fruit Cup"), codes) == ["kiwi"]


def test_a_gluten_free_diet_gets_the_ingredient_safety_net():
    assert not T.diet_ok(_dish("Spaghetti Marinara", "semolina pasta, tomato"), ["gluten_free"])
    assert T.diet_ok(_dish("GF Penne", "rice pasta", tags=["avoiding_gluten"]), ["gluten_free"])
    assert T.diet_ok(_dish("Garden Salad", "lettuce, tomato"), ["gluten_free"])


@pytest.mark.parametrize("said,saved", [
    ("peanut oil", ["peanuts"]), ("sesame seeds", ["sesame"]), ("milk protein", ["milk"]),
    ("soy sauce", ["soy"]), ("gluten-free", ["gluten"]), ("no nuts", ["nuts"]), ("nut-free", ["nuts"]),
    ("avoid shellfish please", ["shellfish"]), ("peanuts and mustard", ["peanuts", "mustard"]),
    ("peanut, kiwi allergy", ["peanuts", "kiwi"]), ("no mustard", ["mustard"]),
])
def test_words_around_an_allergen_are_not_saved_as_more_allergies(prefs, said, saved):
    prefs()
    assert T.update_prefs({"allergies": said})["status"] == "ok"
    after = T.read_prefs()
    assert after["allergies"] == saved
    assert not {"oil", "seeds", "protein", "sauce", "free", "no", "please"} & set(after["never_eat"])


def test_love_it_and_never_again_keep_a_menu_name_with_commas_whole(prefs):
    prefs()
    assert T.update_prefs({"never_eat": ["Rice, Brown"]}, exact=True)["status"] == "ok"
    assert T.read_prefs()["never_eat"] == ["Rice, Brown"]  # and reading never splits it again
    assert not T.blacklist_hits(_dish("Fudge Brownie"), T.read_prefs()["never_eat"])
    assert T.blacklist_hits(_dish("Rice, Brown"), T.read_prefs()["never_eat"])


def test_the_agent_can_still_send_several_foods_in_one_entry(prefs):
    prefs()
    T.update_prefs({"never_eat": ["mushrooms, olives"], "dislikes": "beets; kale"})
    saved = T.read_prefs()
    assert saved["never_eat"] == ["mushrooms", "olives"] and saved["dislikes"] == ["beets", "kale"]


def test_the_memory_form_keeps_saved_names_with_commas_whole():
    assert T.split_entries("fries, Rice, Brown, ice cream", ["Rice, Brown"]) == ["fries", "Rice, Brown", "ice cream"]
    assert T.split_entries("", ["Rice, Brown"]) == []


@pytest.mark.parametrize("name,codes,expected", [
    ("Cheeseburger", {"milk", "gluten"}, ["gluten", "milk"]),
    ("Philly Cheesesteak", {"milk"}, ["milk"]),
    ("Buttermilk Pancakes", {"milk", "gluten", "egg"}, ["egg", "gluten", "milk"]),
    ("Cornbread", {"gluten"}, ["gluten"]),
    ("Breadsticks", {"gluten"}, ["gluten"]),
    ("Garlic Knots", {"gluten"}, ["gluten"]),
    ("Chicken Ramen", {"gluten"}, ["gluten"]),
    ("Eggnog", {"egg", "milk"}, ["egg", "milk"]),
    ("Beer-Battered Cod", {"gluten", "fish"}, ["fish", "gluten"]),
    # and the words that look alike but aren't the allergen
    ("Eggplant Parmesan", {"egg"}, []),
    ("Butternut Squash Soup", {"milk"}, []),
    ("Scalloped Potatoes", {"shellfish"}, []),
    ("2 Piece Fried Chicken|cooked in the fryer", {"gluten"}, []),
    ("Pitaya Bowl", {"gluten"}, []),
    ("Waffle Fries", {"egg", "gluten"}, []),
    ("French Fries|potatoes, soybean oil", {"soy"}, []),
    ("Edamame|soybeans, salt", {"soy"}, ["soy"]),
    ("Overnight Oats|soy milk, oats", {"milk", "soy"}, ["soy"]),
    ("Almond Milk Smoothie", {"milk", "tree_nuts"}, ["tree_nuts"]),
    ("PB Toast|peanut butter, white bread", {"milk"}, []),
    ("Roast Pork Loin|sliced for cold sandwiches", {"gluten"}, []),
    ("Farfalle|*at risk for egg", {"egg"}, ["egg"]),
])
def test_the_keyword_safety_net_catches_compound_dish_names(name, codes, expected):
    title, _, ingredients = name.partition("|")
    assert T.allergy_conflicts(_dish(title, ingredients), codes) == expected


def test_a_full_list_says_so_instead_of_dropping_the_new_entry(prefs):
    prefs(notes=[f"note {i}" for i in range(T.MAX_ENTRIES)])
    res = T.update_prefs({"notes": "loves spicy food"})
    assert res["status"] == "error" and "full" in res["error"]
    assert "loves spicy food" not in T.read_prefs()["notes"]


# --- menu data --------------------------------------------------------------------------------------

import config  # noqa: E402
from src import fetch_menu, locations  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def live_hall(monkeypatch):
    """Live mode with fetch_hall_day scripted: each call returns the next answer."""
    answers, calls = [], []
    monkeypatch.setattr(config, "DATA_MODE", "live")
    clock = Clock()
    monkeypatch.setattr(T, "_clock", clock)

    def fake(hall, day, offline=False):
        calls.append(hall)
        return answers.pop(0) if len(answers) > 1 else answers[0]
    monkeypatch.setattr(T, "fetch_hall_day", fake)
    monkeypatch.setattr(T, "parse_hall_day", lambda hall, stations, code, all_day=None: list(stations))
    return answers, calls, clock


def _item(i=1):
    return {"id": f"E{i}", "name": "Tofu", "meals": ["lunch"], "station": "Grill", "allergens": [], "tags": []}


def test_a_day_not_posted_yet_is_asked_again_soon(live_hall):
    answers, calls, clock = live_hall
    answers += [{"stations": [], "sources": {}, "errors": []}, {"stations": [_item()], "sources": {}, "errors": []}]
    day = config.today()
    assert T.load_items("East", day)[0] == []
    T.load_items("East", day)
    assert len(calls) == 1  # one plan's many lookups don't ask again
    clock.t += T.EMPTY_CACHE_S + 1
    assert T.load_items("East", day)[0] == [_item()]


def test_a_good_menu_is_reread_after_a_while_and_the_cache_stays_small(live_hall):
    answers, calls, clock = live_hall
    answers.append({"stations": [_item()], "sources": {}, "errors": []})
    day = config.today()
    T.load_items("East", day)
    clock.t += T.DAY_CACHE_S + 1
    T.load_items("East", day)
    assert len(calls) == 2
    for n in range(T.MAX_CACHED_DAYS + 20):
        T.load_items("East", day + __import__("datetime").timedelta(days=n + 1))
    assert len(T._day_cache) <= T.MAX_CACHED_DAYS


def test_when_every_station_fails_the_hall_is_an_error_not_closed(live_hall):
    answers, _, _ = live_hall
    answers.append({"stations": [], "sources": {}, "errors": ["Couldn't load East Grill: HTTP 429"]})
    menu = T.get_menu("East", "lunch", config.today().isoformat())
    assert menu["status"] == "error" and "429" in menu["summary"]
    assert "East couldn't load" in T.scout_meal("lunch", config.today().isoformat(), places=["East"])["summary"]


def test_a_refusal_pauses_fetching_for_a_while_not_forever(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(fetch_menu, "_clock", clock)
    monkeypatch.setattr(fetch_menu, "_stopped_reason", None)
    monkeypatch.setattr(config, "LIVE_FETCH", True)
    monkeypatch.setattr(config, "POLITE_DELAY_S", 0)
    sent = []

    class Resp:
        def __init__(self, status, headers=None):
            self.status_code, self.headers = status, headers or {}

        def raise_for_status(self):
            pass

        def json(self):
            return {}
    replies = [Resp(429, {"Retry-After": "120"}), Resp(200)]
    monkeypatch.setattr(fetch_menu.requests, "get", lambda url, **kw: sent.append(url) or replies.pop(0))
    with pytest.raises(fetch_menu.FetchError):
        fetch_menu._get_json("https://example.invalid/1")
    clock.t += 10 * 60  # still within the pause (at least 15 minutes)
    with pytest.raises(fetch_menu.FetchError, match="Not contacting Nutrislice"):
        fetch_menu._get_json("https://example.invalid/2")
    clock.t += fetch_menu.MIN_PAUSE_S
    assert fetch_menu._get_json("https://example.invalid/3") == {}
    assert sent == ["https://example.invalid/1", "https://example.invalid/3"]


def test_a_blocked_request_pauses_longest():
    assert fetch_menu._pause_for(403, {}) == fetch_menu.BLOCKED_PAUSE_S
    assert fetch_menu._pause_for(429, {}) == fetch_menu.RATE_LIMIT_PAUSE_S
    assert fetch_menu._pause_for(503, {"Retry-After": "7200"}) == 7200
    assert fetch_menu._pause_for(503, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) == fetch_menu.RATE_LIMIT_PAUSE_S
    assert fetch_menu._pause_for(429, {"Retry-After": "999999"}) == fetch_menu.MAX_PAUSE_S


def test_roth_over_a_break_is_closed_not_unknown(monkeypatch):
    schools = [{"id": 6333, "name": "East Side Dine-In", "slug": "east-side-dining", "active_menu_types": [{}]}]
    monkeypatch.setattr(fetch_menu, "load_schools", lambda offline=False, recheck=False: schools)
    assert locations.get("Roth Cafe", locations.from_schools(schools)).key == "Roth Cafe"
    with pytest.raises(fetch_menu.NotPosted, match="probably closed"):
        fetch_menu.list_stations("Roth Cafe")


def test_a_saved_place_that_is_closed_today_is_reported_closed(prefs):
    prefs(extra_locations=["Starbucks at the Library"])
    assert "Starbucks at the Library" in T.considered()
    scout = T.scout_meal("lunch", config.today().isoformat())
    assert scout["halls"]["Starbucks at the Library"]["status"] == "no_menu_posted"
    assert "probably closed" in scout["summary"]


def test_sample_mode_never_contacts_nutrislice_for_the_list_of_places(monkeypatch):
    monkeypatch.setattr(config, "LIVE_FETCH", True)
    monkeypatch.setattr(fetch_menu, "_get_json", lambda url: pytest.fail("contacted Nutrislice in sample mode"))
    T.configure(data_mode="sample")
    assert [loc.key for loc in locations.all_locations()][:2] == ["East", "West"]
    T.configure(data_mode="live")
    T.set_simulation(offline=True)  # the "Network down" switch
    locations._memo.clear()
    assert [loc.key for loc in locations.all_locations()][:2] == ["East", "West"]


def test_a_renumbered_hall_with_menus_beats_its_old_empty_entry():
    loc = locations.get("West", locations.builtin())
    old = {"id": 6334, "name": "West Side Dine-In", "slug": "west-side-dining", "active_menu_types": []}
    new = {"id": 7001, "name": "West Side Dine-In", "slug": "west-side-dining", "active_menu_types": [{"id": 1}]}
    assert fetch_menu._schools_of(loc, [old, new]) == [new]


# --- checker, agent and queue ---------------------------------------------------------------------------

from datetime import date, datetime, time as clock_time, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from src import notify, push_queue  # noqa: E402
from src import groq_agent as G  # noqa: E402
from src.agent import Engine, default_goal  # noqa: E402
from tests.conftest import SAMPLE_DATE  # noqa: E402

NY = ZoneInfo("America/New_York")


def _pick():
    return T.scout_meal("dinner", SAMPLE_DATE)["halls"]["East"]["items"][0]


def test_null_or_odd_text_fields_are_stored_as_text(prefs):
    prefs()
    plan = {"date": SAMPLE_DATE, "headline": None, "goal": ["x"], "message": None,
            "meals": [{"meal": "dinner", "hall": "East", "items": [{"id": _pick()["id"]}], "reason": None,
                       "alternatives": [{"items": [{"id": _pick()["id"]}], "note": None}]}]}
    result = T.submit_plan(plan)
    assert result["status"] == "accepted"
    saved = result["plan"]
    assert saved["headline"] == "" and saved["goal"] == "x" and saved["message"] == ""
    assert saved["meals"][0]["reason"] == "" and saved["meals"][0]["alternatives"][0]["note"] == ""


@pytest.mark.parametrize("servings", ["nan", "inf", 1e309, float("nan")])
def test_servings_that_are_not_finite_are_rejected_not_a_crash(prefs, servings):
    prefs()
    result = T.submit_plan({"date": SAMPLE_DATE, "meals": [
        {"meal": "dinner", "hall": "East", "items": [{"id": _pick()["id"], "servings": servings}]}]})
    assert result["status"] == "rejected" and "servings" in result["problems"][0]


def test_stepping_up_from_flash_lite_goes_to_flash_not_an_older_lite():
    ids = ["gemini-2.0-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3.5-flash",
           "gemini-2.5-flash"]
    assert G.rank_models("gemini", ids) == ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash",
                                            "gemini-2.5-flash-lite", "gemini-2.0-flash-lite"]


@pytest.mark.parametrize("limit", ["eight", "8 items", None, -3, 999])
def test_an_odd_search_limit_never_crashes_the_run(prefs, limit):
    prefs()
    result = G._execute("search_items", '{"meal": "lunch", "limit": %s}' % (
        f'"{limit}"' if isinstance(limit, str) else "null" if limit is None else limit), {"search_items"})
    assert result["status"] in ("ok", "no_matches") and len(result["items"]) <= G.SEARCH_LIMIT
    assert T.search_items("lunch", date=SAMPLE_DATE, limit=limit)["status"] in ("ok", "no_matches")


def test_a_plan_for_the_wrong_day_is_rejected_while_queuing(prefs):
    prefs()
    with T.plan_date_locked(date(2026, 10, 3)):
        result = T.submit_plan({"date": SAMPLE_DATE, "meals": [
            {"meal": "dinner", "hall": "East", "items": [{"id": _pick()["id"]}]}]})
    assert result["status"] == "rejected" and "2026-10-03" in result["problems"][0]


def test_the_queue_never_sends_a_plan_under_another_days_id(prefs, monkeypatch):
    prefs()
    sent = []
    monkeypatch.setattr(notify, "send_plan", lambda plan, **kw: sent.append(plan) or {})
    monkeypatch.setattr(push_queue, "menus_posted", lambda day: True)

    class Run:
        plan = {"date": "2026-10-01", "meals": [], "totals": {"protein_g": 1, "calories": 1}}
        error = None
    monkeypatch.setattr("src.agent.run_agent", lambda goal, plan_date=None, engine=None: Run())
    queued = push_queue.queue_days(clock_time(7, 30), "dw-test", Engine("builtin"),
                                   now=datetime(2026, 10, 1, 15, 0, tzinfo=NY))
    assert sent == [] and all("another day" in q.skipped for q in queued)


def test_cancel_keeps_going_when_one_day_fails(monkeypatch):
    tried = []

    def cancel(day, topic=None):
        tried.append(day)
        if len(tried) == 1:
            raise notify.requests.ConnectionError("blip")
    monkeypatch.setattr(notify, "cancel_queued", cancel)
    with pytest.raises(RuntimeError, match="blip"):
        push_queue.cancel_days("dw-test", now=datetime(2026, 10, 11, 15, 0, tzinfo=NY))
    assert len(tried) == 4  # every day was still tried


def test_nothing_queued_for_a_day_is_not_a_cancel_error(monkeypatch):
    class NotFound:
        status_code = 404

        def raise_for_status(self):
            raise notify.requests.HTTPError("404")
    monkeypatch.setattr(notify.requests, "delete", lambda url, **kw: NotFound())
    notify.cancel_queued(date(2026, 10, 12), "dw-test")  # no error


def test_a_posted_menu_where_nothing_fits_still_counts_as_posted(prefs, monkeypatch):
    prefs()
    monkeypatch.setattr(T, "scout_meal", lambda meal, date=None, **kw: {
        "halls": {"East": {"status": "no_matches"}, "West": {"status": "no_menu_posted"}}})
    assert push_queue.menus_posted(date(2026, 10, 12))


def test_recent_plans_are_the_days_before_the_plan_date_only(prefs):
    prefs()
    for d in ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"):
        T.append_history({"date": d, "meals": [{"meal": "lunch", "items": [{"name": f"dish {d}"}]}]})
    recent = T.get_meal_history(days=3, date="2026-10-01")["recent_plans"]
    assert [r["date"] for r in recent] == ["2026-09-29", "2026-09-30"]


def test_the_default_goal_plans_only_the_saved_meals():
    goal = default_goal({"meals_to_plan": ["lunch", "dinner"], "diet": [], "daily_protein_goal_g": None,
                         "daily_calorie_goal": None, "favorite_hall": None})
    assert goal.startswith("Plan my lunch and dinner") and "breakfast" not in goal
    from src import briefing
    assert briefing.meals_for(goal, {"meals_to_plan": ["lunch", "dinner"]}) == ["lunch", "dinner"]


# --- accounts ---------------------------------------------------------------------------------------------

from src import accounts, storage  # noqa: E402
from tests.test_accounts import FakeSupabase  # noqa: E402


def _two_tabs():
    db, users = {}, {"wolf@sbu.edu": ("u1", "pw")}
    server = {"issued": 0, "valid": {}, "used": {}, "access": {}, "refreshes": 0, "revoked": 0}
    laptop, phone = FakeSupabase(db, users, server=server), FakeSupabase(db, users, server=server)
    laptop.current = phone.current = "u1"
    return db, storage.SupabaseStore(laptop, "u1"), storage.SupabaseStore(phone, "u1")


def test_an_allergy_added_on_the_phone_survives_a_save_from_an_older_tab():
    db, laptop, phone = _two_tabs()
    storage.use(laptop)
    T.read_prefs()  # the laptop tab has its copy
    storage.use(phone)
    assert T.update_prefs({"allergies": "peanuts"})["status"] == "ok"
    storage.use(laptop)
    assert T.update_prefs({"favorites": ["tofu"]})["status"] == "ok"  # re-reads before writing
    assert db["u1"]["prefs"]["allergies"] == ["peanuts"] and db["u1"]["prefs"]["favorites"] == ["tofu"]


def test_an_open_tab_sees_changes_from_another_device_after_a_few_seconds(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(storage, "_clock", clock)
    db, laptop, phone = _two_tabs()
    storage.use(phone)
    T.update_prefs({"allergies": "sesame"})
    storage.use(laptop)
    clock.t += storage.SupabaseStore.REFRESH_S + 1
    assert T.read_prefs()["allergies"] == ["sesame"]


def test_past_plans_from_two_tabs_are_both_kept():
    db, laptop, phone = _two_tabs()
    storage.use(laptop)
    T.read_history()
    storage.use(phone)
    T.append_history({"date": "2026-10-12", "headline": "phone"})
    storage.use(laptop)
    T.append_history({"date": "2026-10-13", "headline": "laptop"})
    assert [h["date"] for h in db["u1"]["history"]] == ["2026-10-12", "2026-10-13"]


@pytest.mark.parametrize("base,mine,theirs,expected", [
    ({"allergies": []}, {"allergies": []}, {"allergies": ["peanuts"]}, {"allergies": ["peanuts"]}),
    ({"never_eat": ["fries"]}, {"never_eat": []}, {"never_eat": ["fries", "olives"]}, {"never_eat": ["olives"]}),
    ({"daily_protein_goal_g": 100}, {"daily_protein_goal_g": 120}, {"daily_protein_goal_g": 110},
     {"daily_protein_goal_g": 120}),
    ({"daily_protein_goal_g": 100}, {"daily_protein_goal_g": 100}, {"daily_protein_goal_g": 110},
     {"daily_protein_goal_g": 110}),
])
def test_the_memory_form_merges_with_changes_made_elsewhere(base, mine, theirs, expected):
    assert T.merge_form(base, mine, theirs) == {**theirs, **expected}


def test_a_stale_tab_only_clears_the_cookie_if_it_is_still_its_own():
    script = accounts.cookie_script(None, only_if='rt-1"</script>')
    assert "max-age=0" in script and "decodeURIComponent" in script
    assert "</script>\"" not in script and "\\u003c/script>" in script  # the token can't break out
    assert accounts.cookie_script(None).count("max-age=0") == 1  # signing out here clears it outright
