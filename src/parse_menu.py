"""Turn raw Nutrislice JSON into a clean list of menu items.

Real field names are documented in docs/data-notes.md. The output schema is:

    {
      "id": "W1234567",            # location code + Nutrislice food id: E/W for the halls,
                                   # "L6401-1234567" for other locations (see src/locations.py)
      "date": "2026-10-03",
      "hall": "West",              # the location's key: "East", "West", "Roth Food Court", ...
      "meals": ["lunch"],          # every meal this item is served at
      "station": "Rooted",
      "section": "Rooted Lunch Specials",
      "name": "Kidney Bean Stew",
      "description": "...",
      "serving": "1 cup",
      "calories": 220,             # None when Nutrislice has no nutrition data
      "protein_g": 12, "carbs_g": 30, "fat_g": 5,
      "fiber_g": 8, "sugar_g": 3, "sodium_mg": 400,
      "allergens": ["soy"],
      "tags": ["vegan", "vegetarian"],
      "ingredients": "..."
    }
"""
from __future__ import annotations

import re

import config

NUTRIENT_FIELDS = {
    "calories": "calories",
    "protein_g": "g_protein",
    "carbs_g": "g_carbs",
    "fat_g": "g_fat",
    "fiber_g": "g_fiber",
    "sugar_g": "g_sugar",
    "sodium_mg": "mg_sodium",
}


def meals_for_section(header: str, station_slug: str) -> list[str]:
    """Which meal(s) a section belongs to, from its header text or the station."""
    text = (header or "").lower()
    for meal, keywords in config.MEAL_KEYWORDS:
        if any(k in text for k in keywords):
            return [meal]
    return list(config.STATION_DEFAULT_MEALS.get(station_slug, config.DEFAULT_MEALS))


def _number(value):
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return int(num) if num.is_integer() else round(num, 1)


def _clean(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")       # strip any HTML
    return re.sub(r"\s+", " ", text).replace("^", "").strip()


def parse_food(food: dict) -> dict:
    """Pull name, nutrition, allergens and tags out of one Nutrislice food object."""
    nutrition = food.get("rounded_nutrition_info") or {}
    has_nutrition = bool(food.get("has_nutrition_info")) and nutrition.get("calories") is not None
    item = {
        "name": _clean(food.get("name")),
        "description": _clean(food.get("description")),
        "serving": None,
        "has_nutrition": has_nutrition,
    }
    for ours, theirs in NUTRIENT_FIELDS.items():
        item[ours] = _number(nutrition.get(theirs)) if has_nutrition else None

    size = food.get("serving_size_info") or {}
    amount, unit = size.get("serving_size_amount"), size.get("serving_size_unit")
    if amount and unit:
        item["serving"] = f"{amount} {unit}"

    allergens, tags = set(), set()
    for icon in (food.get("icons") or {}).get("food_icons", []):
        slug = icon.get("slug", "")
        if slug in config.ALLERGEN_SLUGS:
            allergens.add(config.ALLERGEN_SLUGS[slug])
        elif slug:
            tags.add(config.TAG_SLUGS.get(slug, slug.replace("-", "_")))
    # Vegan food is vegetarian too, even when only the vegan icon is set.
    if "vegan" in tags:
        tags.add("vegetarian")
    item["allergens"] = sorted(allergens)
    item["tags"] = sorted(tags)
    item["ingredients"] = _clean(food.get("ingredients"))[:600]
    return item


def parse_station_day(hall: str, station_slug: str, station_name: str, raw_day: dict,
                      code: str | None = None) -> list[dict]:
    """Normalize one station's rows for one day. `code` prefixes item ids (default: hall's first letter)."""
    code = code or hall[0]
    items = []
    header = ""
    for row in sorted(raw_day.get("menu_items", []), key=lambda r: (r.get("menu_id") or 0, r.get("position") or 0)):
        if row.get("is_station_header") or row.get("is_section_title"):
            header = _clean(row.get("text"))
            continue
        food = row.get("food")
        if not food or not food.get("name"):
            continue
        item = parse_food(food)
        item.update({
            "id": f"{code}{food.get('id')}",
            "date": raw_day.get("date"),
            "hall": hall,
            "meals": meals_for_section(header, station_slug),
            "station": station_name,
            "section": header or station_name,
        })
        items.append(item)
    return items


def merge_duplicates(items: list[dict]) -> list[dict]:
    """The same food can appear in several sections; keep one copy, union the meals,
    and remember which station serves it at each meal."""
    by_id: dict[str, dict] = {}
    for item in items:
        kept = by_id.setdefault(item["id"], {**item, "meals": [], "station_by_meal": {}, "section_by_meal": {}})
        for meal in item["meals"]:
            kept["station_by_meal"].setdefault(meal, item["station"])
            kept["section_by_meal"].setdefault(meal, item["section"])
        kept["meals"] = sorted(set(kept["meals"]) | set(item["meals"]), key=config.ALL_MEALS.index)
    return list(by_id.values())


def parse_hall_day(hall: str, stations_raw: list, code: str | None = None) -> list[dict]:
    """stations_raw is the "stations" list from fetch_menu.fetch_hall_day()."""
    items = []
    for station, raw_day in stations_raw:
        items.extend(parse_station_day(hall, station.slug, station.name, raw_day, code))
    return merge_duplicates(items)
