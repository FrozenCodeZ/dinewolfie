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
