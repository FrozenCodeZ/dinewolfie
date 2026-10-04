"""Parse a saved slice of real Nutrislice JSON and check the normalized fields."""
import json
from pathlib import Path

from src.parse_menu import meals_for_section, merge_duplicates, parse_food, parse_station_day

RAW = json.loads((Path(__file__).parent / "fixtures" / "west_2026-10-01_raw.json").read_text(encoding="utf-8"))


def parse(slug, name):
    return parse_station_day("West", slug, name, RAW[slug])


def test_items_have_the_full_schema():
    items = parse("dine-in-vegan-delights", "Rooted")
    assert len(items) == 8  # 10 rows minus 2 section headers
    for item in items:
        for key in ("id", "date", "hall", "meals", "station", "section", "name", "calories",
                    "protein_g", "carbs_g", "fat_g", "allergens", "tags"):
            assert key in item
        assert item["id"].startswith("W")
        assert item["hall"] == "West" and item["date"] == "2026-10-01"


def test_meal_comes_from_section_header():
    items = {i["name"]: i for i in parse("dine-in-vegan-delights", "Rooted")}
    assert items["Three Bean Chili"]["meals"] == ["dinner"]
    assert items["Cajun-Style Lentil Stew"]["meals"] == ["lunch"]


def test_header_without_meal_falls_back_to_station_default():
    assert meals_for_section("Halal Grill Options", "dine-in-grill") == ["lunch", "dinner"]
    assert meals_for_section("Morning Fruit and Yogurt Bar", "yogurt-fruit-bar") == ["breakfast"]
    assert meals_for_section("Late Night Specials", "anything") == ["late_night"]


def test_duplicate_food_is_merged_with_both_meals():
    grill = merge_duplicates(parse("dine-in-grill", "Grill"))
    fries = [i for i in grill if i["name"] == "French Fries"]
    assert len(fries) == 1
    assert fries[0]["meals"] == ["lunch", "dinner"]


def test_nutrition_and_tags_are_parsed():
    chili = next(i for i in parse("dine-in-vegan-delights", "Rooted") if i["name"] == "Three Bean Chili")
    assert isinstance(chili["calories"], int) and chili["calories"] > 0
    assert isinstance(chili["protein_g"], (int, float))
    assert "vegan" in chili["tags"] and "vegetarian" in chili["tags"]


def test_missing_nutrition_is_none_not_zero():
    item = parse_food({"name": "Mystery Special", "has_nutrition_info": False,
                       "rounded_nutrition_info": {"calories": None}, "icons": {"food_icons": []}})
    assert item["calories"] is None and item["protein_g"] is None
    assert item["has_nutrition"] is False


def test_icons_split_into_allergens_and_tags():
    item = parse_food({"name": "X", "has_nutrition_info": True, "rounded_nutrition_info": {"calories": 100},
                       "icons": {"food_icons": [{"slug": "milk"}, {"slug": "contains-gluten"},
                                                {"slug": "vegan"}, {"slug": "halal"}]}})
    assert item["allergens"] == ["gluten", "milk"]
    assert item["tags"] == ["halal", "vegan", "vegetarian"]
