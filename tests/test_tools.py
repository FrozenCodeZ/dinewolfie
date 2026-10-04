"""The agent's tools, run against the committed sample day (no network, no Claude)."""
from src import tools as T
from tests.conftest import SAMPLE_DATE


def test_get_menu_ok_and_grouped_by_station():
    menu = T.get_menu("West", "dinner", SAMPLE_DATE)
    assert menu["status"] == "ok"
    assert menu["item_count"] > 20
    assert all(isinstance(v, list) for v in menu["stations"].values())


def test_missing_menu_is_reported_not_invented():
    T.set_simulation(missing=[("West", "dinner")])
    menu = T.get_menu("West", "dinner", SAMPLE_DATE)
    assert menu["status"] == "no_menu_posted"
    assert "stations" not in menu


def test_search_respects_diet_and_allergies(prefs):
    prefs(diet=["vegetarian"], allergies=["milk", "egg"])
    res = T.search_items("dinner", date=SAMPLE_DATE, limit=40)
    assert res["items"], res["summary"]
    for item in res["items"]:
        assert "vegetarian" in item["tags"]
        assert "milk" not in item["allergens"] and "egg" not in item["allergens"]
    assert res["excluded_counts"].get("allergen", 0) > 0


def test_search_min_protein_and_sorting(prefs):
    prefs()
    res = T.search_items("lunch", date=SAMPLE_DATE, min_protein_g=20, sort_by="protein", limit=40)
    proteins = [i["protein_g"] for i in res["items"]]
    assert proteins and min(proteins) >= 20
    assert proteins == sorted(proteins, reverse=True)


def test_dislikes_filter_by_keyword(prefs):
    prefs(dislikes=["fries"])
    res = T.search_items("lunch", hall="West", date=SAMPLE_DATE, limit=40)
    assert not any("fries" in i["name"].lower() for i in res["items"])


def test_compare_halls_reports_both(prefs):
    prefs(diet=["vegan"])
    res = T.compare_halls("lunch", SAMPLE_DATE)
    assert set(res["halls"]) == {"East", "West"}
    assert res["better_hall_by_protein"] in ("East", "West")


def test_compare_halls_when_one_hall_is_missing(prefs):
    prefs()
    T.set_simulation(missing=[("West", "dinner")])
    res = T.compare_halls("dinner", SAMPLE_DATE)
    assert res["halls"]["West"]["menu_status"] == "no_menu_posted"
    assert res["better_hall_by_protein"] == "East"


def test_sum_nutrition_does_the_math():
    menu = T.get_menu("East", "lunch", SAMPLE_DATE)
    items = [i for group in menu["stations"].values() for i in group if i["calories"]][:2]
    res = T.sum_nutrition([{"id": items[0]["id"], "servings": 2}, {"id": items[1]["id"]}], SAMPLE_DATE)
    assert res["totals"]["calories"] == round(items[0]["calories"] * 2 + items[1]["calories"])
    assert res["unknown_ids"] == []


def test_sum_nutrition_flags_unknown_ids():
    res = T.sum_nutrition([{"id": "W000"}], SAMPLE_DATE)
    assert res["status"] == "error" and res["unknown_ids"] == ["W000"]


def _first(hall, meal, **search):
    res = T.search_items(meal, hall=hall, date=SAMPLE_DATE, sort_by="protein", **search)
    return res["items"][0]


def test_submit_plan_rejects_made_up_items(prefs):
    prefs()
    res = T.submit_plan({"date": SAMPLE_DATE, "headline": "x",
                         "meals": [{"meal": "dinner", "hall": "West", "items": [{"id": "W123"}], "reason": ""}]})
    assert res["status"] == "rejected"
    assert "not on the West dinner menu" in res["problems"][0]


def test_submit_plan_rejects_item_from_the_other_hall(prefs):
    prefs()
    east_item = _first("East", "dinner")
    res = T.submit_plan({"date": SAMPLE_DATE, "headline": "x", "meals": [
        {"meal": "dinner", "hall": "West", "items": [{"id": east_item["id"]}], "reason": ""}]})
    assert res["status"] == "rejected"


def test_submit_plan_rejects_allergens(prefs):
    prefs(allergies=["milk"])
    menu = T.get_menu("West", "breakfast", SAMPLE_DATE)
    milky = next(i for g in menu["stations"].values() for i in g if "milk" in i["allergens"])
    res = T.submit_plan({"date": SAMPLE_DATE, "headline": "x", "meals": [
        {"meal": "breakfast", "hall": "West", "items": [{"id": milky["id"]}], "reason": ""}]})
    assert res["status"] == "rejected"
    assert any("allerg" in p for p in res["problems"])


def test_submit_plan_accepts_valid_plan_and_remembers_it(prefs):
    prefs(diet=["vegetarian"], daily_protein_goal_g=120)
    b, l, d = _first("East", "breakfast"), _first("West", "lunch"), _first("East", "dinner")
    res = T.submit_plan({"date": SAMPLE_DATE, "headline": "Test day", "goal": "test", "meals": [
        {"meal": "breakfast", "hall": "East", "items": [{"id": b["id"], "servings": 2}], "reason": "r"},
        {"meal": "lunch", "hall": "West", "items": [{"id": l["id"]}], "reason": "r"},
        {"meal": "dinner", "hall": "East", "items": [{"id": d["id"]}], "reason": "r"}]})
    assert res["status"] == "accepted", res.get("problems")
    plan = res["plan"]
    expected = round(b["protein_g"] * 2 + l["protein_g"] + d["protein_g"])
    assert plan["totals"]["protein_g"] == expected
    assert plan["protein_gap_g"] == max(0, 120 - expected)
    assert T.get_meal_history(days=3650)["recent_plans"][0]["date"] == SAMPLE_DATE


def test_submit_plan_accepts_keyword_fields_like_the_agent_sends(prefs):
    prefs()
    item = _first("West", "dinner")
    res = T.submit_plan(date=SAMPLE_DATE, headline="x", meals=[
        {"meal": "dinner", "hall": "West", "items": [{"id": item["id"]}], "reason": "r"}])
    assert res["status"] == "accepted", res.get("problems")


def test_partial_day_plan_is_not_judged_against_the_daily_goal(prefs):
    prefs(daily_protein_goal_g=120)
    item = _first("East", "dinner")
    plan = T.submit_plan(date=SAMPLE_DATE, headline="x", meals=[
        {"meal": "dinner", "hall": "East", "items": [{"id": item["id"]}], "reason": "r"}])["plan"]
    assert plan["full_day"] is False
    assert plan["protein_goal_g"] is None and plan["protein_gap_g"] is None


def test_every_tool_accepts_its_schema_fields():
    """Each schema property must be a keyword the Python function accepts (the bug the agent found)."""
    import inspect

    from src.agent import TOOL_SPECS
    for name, _, schema, fn, _ in TOOL_SPECS:
        params = inspect.signature(fn).parameters
        takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())
        for prop in schema.get("properties", {}):
            assert prop in params or takes_kwargs, f"{name} doesn't accept {prop!r}"


def test_update_prefs_rejects_unknown_keys(prefs):
    prefs()
    assert T.update_prefs({"favourite_colour": "red"})["status"] == "error"
    assert T.update_prefs({"allergies": ["peanuts"]})["status"] == "ok"
    assert T.read_prefs()["allergies"] == ["peanuts"]


def test_allergy_keywords_catch_unlabeled_items():
    item = {"name": "Peanut Noodles", "description": "", "ingredients": "", "allergens": []}
    assert T.allergy_conflicts(item, T.allergen_codes(["peanuts"])) == ["peanuts"]
    eggplant = {"name": "Eggplant Parm", "description": "", "ingredients": "", "allergens": []}
    assert T.allergy_conflicts(eggplant, T.allergen_codes(["eggs"])) == []


# --- the "human" features: blacklist, favorites, treats, swaps, cheat days ------------------

def test_never_eat_is_hard_blocked_in_search_and_checker(prefs):
    prefs()
    target = _first("West", "dinner")
    word = target["name"].split()[-1]
    prefs(never_eat=[word])
    res = T.search_items("dinner", hall="West", date=SAMPLE_DATE, limit=40)
    assert not any(word.lower() in i["name"].lower() for i in res["items"])
    assert res["excluded_counts"].get("never_eat", 0) >= 1
    checked = T.submit_plan(date=SAMPLE_DATE, headline="x", meals=[
        {"meal": "dinner", "hall": "West", "items": [{"id": target["id"]}], "reason": "r"}])
    assert checked["status"] == "rejected"
    assert any("never-eat" in p for p in checked["problems"])


def test_favorites_float_to_the_top_and_are_marked(prefs):
    prefs(favorites=["yogurt"])
    res = T.search_items("breakfast", date=SAMPLE_DATE, sort_by="calories_high", limit=40)
    assert res["items"][0].get("favorite") is True
    assert "yogurt" in res["items"][0]["name"].lower()


def test_alternatives_are_checked_and_totaled(prefs):
    prefs()
    main, swap = (T.search_items("lunch", hall="East", date=SAMPLE_DATE, sort_by="protein")["items"][:2])
    west = _first("West", "lunch")
    plan = T.submit_plan(date=SAMPLE_DATE, headline="x", meals=[{
        "meal": "lunch", "hall": "East", "items": [{"id": main["id"]}], "reason": "r",
        "alternatives": [{"items": [{"id": swap["id"]}], "note": "if the line is long"},
                         {"hall": "West", "items": [{"id": west["id"]}], "note": "if you're near West"}]}])
    assert plan["status"] == "accepted", plan.get("problems")
    alts = plan["plan"]["meals"][0]["alternatives"]
    assert [a["hall"] for a in alts] == ["East", "West"]
    assert alts[0]["totals"]["protein_g"] == round(swap["protein_g"])
    # swaps don't count toward the day's totals
    assert plan["plan"]["totals"]["protein_g"] == round(main["protein_g"])


def test_a_bad_swap_gets_the_plan_rejected(prefs):
    prefs()
    main = _first("East", "lunch")
    res = T.submit_plan(date=SAMPLE_DATE, headline="x", meals=[{
        "meal": "lunch", "hall": "East", "items": [{"id": main["id"]}], "reason": "r",
        "alternatives": [{"items": [{"id": "E000"}], "note": "made up"}]}])
    assert res["status"] == "rejected" and "swap 1" in res["problems"][0]


def test_treat_flag_and_cheat_day(prefs):
    prefs(cheat_days=["Thursday"])  # 2026-10-01 is a Thursday
    dessert = next(i for i in T.search_items("dinner", date=SAMPLE_DATE, limit=40, sort_by="calories_high")["items"]
                   if i.get("treat"))
    plan = T.submit_plan(date=SAMPLE_DATE, headline="x", meals=[
        {"meal": "dinner", "hall": dessert["hall"], "items": [{"id": dessert["id"], "treat": True}], "reason": "r"}])
    assert plan["status"] == "accepted", plan.get("problems")
    assert plan["plan"]["cheat_day"] is True
    assert plan["plan"]["meals"][0]["items"][0]["treat"] is True
