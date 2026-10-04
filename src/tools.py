"""The agent's tools, as plain Python functions.

Rule of thumb: the model decides, the code calculates. Every number the
agent reports (protein, calories, totals) comes from these functions, never
from the model's memory. Each function returns a dict; agent.py turns that
into the text Claude reads and into the step-by-step trace the UI shows.

Every result has a "summary" (one human sentence for the trace) and, where
something can go wrong, a "status" the agent must look at.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from src.fetch_menu import FetchError, fetch_hall_day
from src.parse_menu import parse_hall_day

HALLS = list(config.HALLS)

# --- Demo "chaos" switches ------------------------------------------------------
# Lets you show on camera how the agent adapts when things break. The agent is
# NOT told these are simulated; it just sees a missing menu or a network error.
SIMULATION = {"missing": set(), "offline": False}


def set_simulation(missing: list[tuple[str, str]] | None = None, offline: bool = False) -> None:
    SIMULATION["missing"] = {(h.title(), m.lower()) for h, m in (missing or [])}
    SIMULATION["offline"] = bool(offline)


def _load_simulation_from_env() -> None:
    # DINEWOLFIE_SIMULATE="missing:West:dinner,offline"
    spec = os.getenv("DINEWOLFIE_SIMULATE", "")
    missing, offline = [], False
    for part in filter(None, (p.strip() for p in spec.split(","))):
        bits = part.split(":")
        if bits[0] == "offline":
            offline = True
        elif bits[0] == "missing" and len(bits) == 3:
            missing.append((bits[1], bits[2]))
    set_simulation(missing, offline)


_load_simulation_from_env()


# --- Dates ----------------------------------------------------------------------

def resolve_date(value: str | None) -> date:
    if not value or str(value).lower() == "today":
        return date.today()
    if str(value).lower() == "tomorrow":
        return date.today() + timedelta(days=1)
    return date.fromisoformat(str(value)[:10])


# --- Loading menus ----------------------------------------------------------------
_day_cache: dict[tuple, tuple[list, dict]] = {}


def _sample_file(day: date) -> Path | None:
    exact = config.SAMPLE_DIR / f"{day.isoformat()}.json"
    if exact.exists():
        return exact
    files = sorted(config.SAMPLE_DIR.glob("20*.json"))
    return files[-1] if files else None


def load_items(hall: str, day: date) -> tuple[list[dict], dict]:
    """All normalized items for a hall on a day, plus where they came from."""
    key = (hall, day, config.DATA_MODE, SIMULATION["offline"])
    if key in _day_cache:
        return _day_cache[key]

    if config.DATA_MODE == "sample":
        path = _sample_file(day)
        if path is None:
            raise FetchError("No sample data in data/sample/.")
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["halls"].get(hall, [])
        meta = {"source": "sample", "note": f"sample data from {data['date']}", "errors": []}
    else:
        offline = SIMULATION["offline"]
        raw = fetch_hall_day(hall, day, offline=offline)
        items = parse_hall_day(hall, raw["stations"])
        sources = set(raw["sources"].values())
        source = "network" if "network" in sources else ("stale-cache" if "stale-cache" in sources else "cache")
        meta = {"source": source, "errors": raw["errors"]}
        if offline:
            meta["note"] = ("Network unavailable: using the copy cached earlier." if items
                            else "Network unavailable and nothing cached for this day.")
            if not items:
                raise FetchError("Couldn't reach Nutrislice and there is no cached copy of this menu.")
    _day_cache[key] = (items, meta)
    return items, meta


def clear_cache() -> None:
    _day_cache.clear()


def _menu(hall: str, meal: str, day: date) -> tuple[list[dict], dict]:
    """Items for one hall + meal. Status is 'ok', 'no_menu_posted' or 'error'."""
    hall = normalize_hall(hall)
    meal = normalize_meal(meal)
    if (hall, meal) in SIMULATION["missing"]:
        return [], {"status": "no_menu_posted", "source": "nutrislice", "errors": []}
    try:
        items, meta = load_items(hall, day)
    except FetchError as exc:
        return [], {"status": "error", "error": str(exc)}
    wanted = set(config.MEAL_ALIASES.get(meal, [meal]))
    selected = []
    for item in items:
        hit = [m for m in item["meals"] if m in wanted]
        if hit:
            # Show the station that serves this item at this meal.
            selected.append({**item, "station": item.get("station_by_meal", {}).get(hit[0], item["station"])})
    status = "ok" if selected else "no_menu_posted"
    return selected, {**meta, "status": status}


# --- Preferences / memory ---------------------------------------------------------
DEFAULT_PREFS = {
    "name": "",
    "diet": [],
    "allergies": [],
    "daily_protein_goal_g": None,
    "daily_calorie_goal": None,
    "max_calories_per_meal": None,
    "favorite_hall": None,
    "dislikes": [],
    "meals_to_plan": ["breakfast", "lunch", "dinner"],
    "notes": [],
}

ALLERGY_SYNONYMS = {
    "dairy": ["milk"], "lactose": ["milk"], "milk": ["milk"],
    "eggs": ["egg"], "egg": ["egg"],
    "nuts": ["tree_nuts", "peanuts"], "nut": ["tree_nuts", "peanuts"],
    "tree nuts": ["tree_nuts"], "tree_nuts": ["tree_nuts"], "tree nut": ["tree_nuts"],
    "peanut": ["peanuts"], "peanuts": ["peanuts"],
    "gluten": ["gluten", "wheat"], "wheat": ["wheat", "gluten"],
    "soy": ["soy"], "soya": ["soy"], "sesame": ["sesame"],
    "fish": ["fish"], "shellfish": ["shellfish"], "shrimp": ["shellfish"],
}
# Words to look for in item names/ingredients as a second safety net.
ALLERGY_WORDS = {
    "peanuts": ["peanut"], "tree_nuts": ["almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut"],
    "milk": ["cheese", "milk", "cream", "butter", "yogurt"], "egg": ["egg"],
    "fish": ["salmon", "tuna", "cod", "tilapia", "fish"], "shellfish": ["shrimp", "crab", "lobster"],
    "sesame": ["sesame", "tahini"], "soy": ["tofu", "soy", "edamame"],
    "gluten": [], "wheat": [],
}


def read_prefs() -> dict:
    prefs = dict(DEFAULT_PREFS)
    if config.PREFS_FILE.exists():
        prefs.update(json.loads(config.PREFS_FILE.read_text(encoding="utf-8")))
    return prefs


def write_prefs(prefs: dict) -> None:
    config.PREFS_FILE.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")


def allergen_codes(allergies: list[str]) -> set[str]:
    codes = set()
    for a in allergies or []:
        codes.update(ALLERGY_SYNONYMS.get(a.strip().lower(), [a.strip().lower().replace(" ", "_")]))
    return codes


def normalize_hall(hall: str) -> str:
    h = (hall or "").strip().lower()
    for name in HALLS:
        if h.startswith(name.lower()):
            return name
    raise ValueError(f"Unknown hall {hall!r}. Use one of {HALLS}.")


def normalize_meal(meal: str) -> str:
    m = (meal or "").strip().lower().replace(" ", "_").replace("-", "_")
    if m not in config.MEAL_ALIASES:
        raise ValueError(f"Unknown meal {meal!r}. Use one of {list(config.MEAL_ALIASES)}.")
    return m


# --- Filtering helpers ----------------------------------------------------------------

def _stem(word: str) -> str:
    w = word.strip().lower()
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def _text(item: dict) -> str:
    return f"{item['name']} {item.get('description', '')} {item.get('ingredients', '')}".lower()


def allergy_conflicts(item: dict, codes: set[str]) -> list[str]:
    """Allergens in an item that the user must avoid (icons first, then keywords)."""
    hits = set(item["allergens"]) & codes
    text = _text(item)
    for code in codes:
        if any(re.search(rf"\b{w}(s|es)?\b", text) for w in ALLERGY_WORDS.get(code, [])):
            hits.add(code)
    return sorted(hits)


def diet_ok(item: dict, diet: list[str]) -> bool:
    tags = set(item["tags"])
    for d in (x.lower() for x in diet or []):
        if d == "vegan" and "vegan" not in tags:
            return False
        if d == "vegetarian" and "vegetarian" not in tags:
            return False
        if d == "halal" and not ({"halal", "vegetarian"} & tags):
            return False
        if d == "pescatarian" and not ("vegetarian" in tags or {"fish", "shellfish"} & set(item["allergens"])):
            return False
        if d in ("gluten_free", "gluten-free", "gluten free") and (
                {"gluten", "wheat"} & set(item["allergens"])):
            return False
    return True


def dislike_hits(item: dict, dislikes: list[str]) -> list[str]:
    text = _text(item)
    return [d for d in dislikes or [] if _stem(d) and _stem(d) in text]


def compact(item: dict) -> dict:
    """The fields Claude needs to choose, without the bulky ones."""
    return {
        "id": item["id"], "name": item["name"], "station": item["station"],
        "serving": item["serving"], "calories": item["calories"], "protein_g": item["protein_g"],
        "carbs_g": item["carbs_g"], "fat_g": item["fat_g"],
        "allergens": item["allergens"], "tags": [t for t in item["tags"] if t in
                                                 ("vegan", "vegetarian", "halal", "avoiding_gluten", "eat_well")],
    }


def _density(item: dict) -> float:
    if not item["protein_g"] or not item["calories"]:
        return 0.0
    return item["protein_g"] / max(item["calories"], 1) * 100


SORTS = {
    "protein": lambda i: -(i["protein_g"] or 0),
    "protein_per_calorie": lambda i: -_density(i),
    "calories_low": lambda i: (i["calories"] is None, i["calories"] or 0),
    "calories_high": lambda i: -(i["calories"] or 0),
}


# =====================================================================================
# Tools
# =====================================================================================

def make_plan(steps: list[str]) -> dict:
    steps = [s for s in (steps or []) if str(s).strip()]
    return {"status": "ok", "steps": steps, "summary": f"Plan with {len(steps)} steps recorded."}


def get_prefs() -> dict:
    prefs = read_prefs()
    goal = prefs.get("daily_protein_goal_g")
    bits = [", ".join(prefs["diet"]) or "no diet restriction",
            f"allergies: {', '.join(prefs['allergies']) or 'none'}",
            f"protein goal {goal} g" if goal else "no protein goal"]
    return {"status": "ok", "prefs": prefs, "summary": "Memory loaded: " + "; ".join(bits) + "."}


EDITABLE_PREFS = set(DEFAULT_PREFS)


def update_prefs(changes: dict, reason: str = "") -> dict:
    prefs = read_prefs()
    unknown = sorted(set(changes) - EDITABLE_PREFS)
    if unknown:
        return {"status": "error", "error": f"Unknown preference keys {unknown}. "
                                            f"Allowed: {sorted(EDITABLE_PREFS)}",
                "summary": "Rejected unknown preference keys."}
    prefs.update(changes)
    write_prefs(prefs)
    return {"status": "ok", "prefs": prefs,
            "summary": f"Memory updated: {', '.join(f'{k}={v}' for k, v in changes.items())}."}


def get_meal_history(days: int = 3) -> dict:
    history = read_history()
    cutoff = (date.today() - timedelta(days=int(days or 3))).isoformat()
    recent = [h for h in history if h.get("date", "") >= cutoff]
    slim = [{"date": h["date"], "goal": h.get("goal", ""),
             "meals": {m["meal"]: [i["name"] for i in m["items"]] for m in h["plan"]["meals"]}}
            for h in recent[-5:]]
    return {"status": "ok", "recent_plans": slim,
            "summary": f"Found {len(slim)} recent plan(s) in memory." if slim else "No recent plans in memory."}


def get_menu(hall: str, meal: str, date: str | None = None) -> dict:
    day = resolve_date(date)
    hall, meal = normalize_hall(hall), normalize_meal(meal)
    items, meta = _menu(hall, meal, day)
    label = f"{hall} {meal.replace('_', ' ')} on {day.isoformat()}"
    if meta["status"] == "error":
        return {"status": "error", "hall": hall, "meal": meal, "date": day.isoformat(),
                "error": meta["error"], "summary": f"Couldn't load {label}: {meta['error']}"}
    if meta["status"] == "no_menu_posted":
        return {"status": "no_menu_posted", "hall": hall, "meal": meal, "date": day.isoformat(),
                "summary": f"No {meal.replace('_', ' ')} menu is posted for {hall} on {day.isoformat()}."}
    stations: dict[str, list] = {}
    for item in items:
        stations.setdefault(item["station"], []).append(compact(item))
    out = {"status": "ok", "hall": hall, "meal": meal, "date": day.isoformat(),
           "data_source": meta["source"], "item_count": len(items),
           "items_missing_nutrition": sum(1 for i in items if i["calories"] is None),
           "stations": stations,
           "summary": f"{label}: {len(items)} items across {len(stations)} stations"
                      f" ({meta['source']})."}
    if meta.get("note"):
        out["note"] = meta["note"]
        out["summary"] += f" {meta['note']}"
    return out


def search_items(meal: str, hall: str = "any", date: str | None = None,
                 min_protein_g: float | None = None, max_calories: float | None = None,
                 exclude_allergens: list[str] | None = None, require_tags: list[str] | None = None,
                 exclude_keywords: list[str] | None = None, station: str | None = None,
                 sort_by: str = "protein_per_calorie", limit: int = 12,
                 apply_my_prefs: bool = True) -> dict:
    day = resolve_date(date)
    meal = normalize_meal(meal)
    halls = HALLS if (hall or "any").lower() in ("any", "both", "all") else [normalize_hall(hall)]
    prefs = read_prefs() if apply_my_prefs else dict(DEFAULT_PREFS)
    codes = allergen_codes(list(exclude_allergens or []) + list(prefs["allergies"]))
    diet = list(require_tags or []) + list(prefs["diet"])
    dislikes = list(exclude_keywords or []) + list(prefs["dislikes"])

    kept, counts, hall_status = [], {"allergen": 0, "diet": 0, "dislike": 0, "no_nutrition": 0,
                                     "protein": 0, "calories": 0, "station": 0}, {}
    for h in halls:
        items, meta = _menu(h, meal, day)
        hall_status[h] = meta["status"]
        for item in items:
            if station and station.lower() not in item["station"].lower():
                counts["station"] += 1
            elif allergy_conflicts(item, codes):
                counts["allergen"] += 1
            elif not diet_ok(item, diet):
                counts["diet"] += 1
            elif dislike_hits(item, dislikes):
                counts["dislike"] += 1
            elif (min_protein_g or max_calories) and item["calories"] is None:
                counts["no_nutrition"] += 1
            elif min_protein_g and (item["protein_g"] or 0) < float(min_protein_g):
                counts["protein"] += 1
            elif max_calories and (item["calories"] or 0) > float(max_calories):
                counts["calories"] += 1
            else:
                kept.append({**compact(item), "hall": h})

    kept.sort(key=SORTS.get(sort_by, SORTS["protein_per_calorie"]))
    limit = max(1, min(int(limit or 12), 40))
    by_station: dict[str, int] = {}
    for item in kept:
        by_station[f"{item['hall']} / {item['station']}"] = by_station.get(f"{item['hall']} / {item['station']}", 0) + 1

    excluded = {k: v for k, v in counts.items() if v}
    status = "ok" if kept else ("no_menu_posted" if all(s != "ok" for s in hall_status.values()) else "no_matches")
    summary = (f"{len(kept)} {meal} items match at {', '.join(halls)}"
               + (f"; excluded {', '.join(f'{v} by {k}' for k, v in excluded.items())}" if excluded else "")
               + ".")
    missing = [h for h, s in hall_status.items() if s != "ok"]
    if missing:
        summary += f" Not available: {', '.join(f'{h} ({hall_status[h]})' for h in missing)}."
    return {"status": status, "meal": meal, "date": day.isoformat(), "hall_status": hall_status,
            "filters_applied": {"allergens_avoided": sorted(codes), "diet": diet, "dislikes": dislikes,
                                "min_protein_g": min_protein_g, "max_calories": max_calories},
            "match_count": len(kept), "matches_by_station": by_station, "excluded_counts": excluded,
            "items": kept[:limit], "summary": summary}


def compare_halls(meal: str, date: str | None = None, min_protein_g: float | None = None,
                  max_calories: float | None = None) -> dict:
    day = resolve_date(date)
    meal = normalize_meal(meal)
    report = {}
    for h in HALLS:
        res = search_items(meal, hall=h, date=day.isoformat(), min_protein_g=min_protein_g,
                           max_calories=max_calories, sort_by="protein", limit=40)
        top = res["items"][:3]
        report[h] = {
            "menu_status": res["hall_status"][h],
            "matching_items": res["match_count"],
            "best_protein_items": [f"{i['name']} ({i['protein_g']} g protein, {i['calories']} kcal)" for i in top],
            "protein_from_top_3_g": sum(i["protein_g"] or 0 for i in top),
        }
    ranked = sorted(HALLS, key=lambda h: (report[h]["menu_status"] == "ok", report[h]["protein_from_top_3_g"],
                                          report[h]["matching_items"]), reverse=True)
    best = ranked[0] if report[ranked[0]]["menu_status"] == "ok" else None
    summary = (f"For {meal} {day.isoformat()}: " + "; ".join(
        f"{h} {report[h]['matching_items']} matches, top-3 protein {report[h]['protein_from_top_3_g']} g"
        if report[h]["menu_status"] == "ok" else f"{h} {report[h]['menu_status']}" for h in HALLS)
        + (f". Edge: {best}." if best else ". Neither hall has a menu posted."))
    return {"status": "ok" if best else "no_menu_posted", "meal": meal, "date": day.isoformat(),
            "halls": report, "better_hall_by_protein": best, "summary": summary}


def get_item_details(ids: list[str], date: str | None = None) -> dict:
    day = resolve_date(date)
    found, missing = [], []
    for item_id in ids or []:
        item = find_item(item_id, day)
        (found.append(item) if item else missing.append(item_id))
    return {"status": "ok" if found else "error", "items": found, "unknown_ids": missing,
            "summary": f"Details for {len(found)} item(s)" + (f"; unknown ids {missing}" if missing else "") + "."}


def find_item(item_id: str, day: date) -> dict | None:
    item_id = str(item_id).strip()
    hall = next((h for h in HALLS if item_id.upper().startswith(h[0])), None)
    if not hall:
        return None
    try:
        items, _ = load_items(hall, day)
    except FetchError:
        return None
    return next((dict(i) for i in items if i["id"].upper() == item_id.upper()), None)


def sum_nutrition(items: list[dict], date: str | None = None) -> dict:
    """items: [{"id": "W123", "servings": 1}, ...]. All arithmetic happens here."""
    day = resolve_date(date)
    totals = {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "sodium_mg": 0}
    lines, missing_data, unknown = [], [], []
    for entry in items or []:
        item_id = entry.get("id") if isinstance(entry, dict) else entry
        servings = float(entry.get("servings", 1) or 1) if isinstance(entry, dict) else 1.0
        item = find_item(item_id, day)
        if not item:
            unknown.append(item_id)
            continue
        if item["calories"] is None:
            missing_data.append(item["name"])
        for key in totals:
            totals[key] += (item.get(key) or 0) * servings
        lines.append({"id": item["id"], "name": item["name"], "servings": servings,
                      "calories": None if item["calories"] is None else round(item["calories"] * servings),
                      "protein_g": None if item["protein_g"] is None else round(item["protein_g"] * servings, 1)})
    totals = {k: round(v) for k, v in totals.items()}
    summary = f"Totals: {totals['protein_g']} g protein, {totals['calories']} kcal"
    if missing_data:
        summary += f" ({len(missing_data)} item(s) have no nutrition listed, so the real total is higher)"
    if unknown:
        summary += f"; unknown ids {unknown}"
    return {"status": "ok" if not unknown else "error", "totals": totals, "items": lines,
            "items_missing_nutrition": missing_data, "unknown_ids": unknown, "summary": summary + "."}


def log_adaptation(problem: str, change: str, reason: str = "") -> dict:
    return {"status": "ok", "problem": problem, "change": change, "reason": reason,
            "summary": f"Adapted: {change}"}


# --- Final answer ------------------------------------------------------------------------

def validate_plan(plan: dict) -> tuple[dict | None, list[str]]:
    """Check a proposed plan against the real menu and the user's constraints."""
    problems = []
    try:
        day = resolve_date(plan.get("date"))
    except ValueError:
        return None, [f"Bad date {plan.get('date')!r}; use YYYY-MM-DD."]
    prefs = read_prefs()
    codes = allergen_codes(prefs["allergies"])
    meals_out, all_entries = [], []
    for meal_plan in plan.get("meals", []):
        try:
            meal = normalize_meal(meal_plan.get("meal", ""))
            hall = normalize_hall(meal_plan.get("hall", ""))
        except ValueError as exc:
            problems.append(str(exc))
            continue
        menu_items, meta = _menu(hall, meal, day)
        on_menu = {i["id"].upper(): i for i in menu_items}
        items_out = []
        for entry in meal_plan.get("items", []):
            item_id = str(entry.get("id", "")).upper()
            servings = float(entry.get("servings", 1) or 1)
            item = on_menu.get(item_id)
            if item is None:
                problems.append(f"{meal}: item id {item_id!r} is not on the {hall} {meal} menu for {day}. "
                                "Only use ids returned by get_menu/search_items for that hall and meal.")
                continue
            bad = allergy_conflicts(item, codes)
            if bad:
                problems.append(f"{meal}: {item['name']} conflicts with allergies {bad}. Remove it.")
            if not diet_ok(item, prefs["diet"]):
                problems.append(f"{meal}: {item['name']} doesn't fit the diet {prefs['diet']}. Remove it.")
            if not 0.25 <= servings <= 4:
                problems.append(f"{meal}: servings for {item['name']} must be between 0.25 and 4.")
            items_out.append({**compact(item), "servings": servings})
            all_entries.append({"id": item["id"], "servings": servings})
        if meta["status"] != "ok":
            problems.append(f"{meal}: {hall} has no {meal} menu ({meta['status']}). Pick another hall.")
        meal_totals = sum_nutrition([{"id": i["id"], "servings": i["servings"]} for i in items_out],
                                    day.isoformat())["totals"] if items_out else {}
        meals_out.append({"meal": meal, "hall": hall, "items": items_out,
                          "reason": meal_plan.get("reason", ""), "totals": meal_totals})
    if not meals_out:
        problems.append("The plan has no meals.")
    totals = sum_nutrition(all_entries, day.isoformat())
    goal = prefs.get("daily_protein_goal_g")
    result = {
        "date": day.isoformat(),
        "goal": plan.get("goal", ""),
        "headline": plan.get("headline", ""),
        "meals": meals_out,
        "totals": totals["totals"],
        "items_missing_nutrition": totals["items_missing_nutrition"],
        "protein_goal_g": goal,
        "protein_gap_g": max(0, round(goal - totals["totals"]["protein_g"])) if goal else None,
        "adaptations": plan.get("adaptations", []),
        "tips": plan.get("tips", []),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return result, problems


def submit_plan(plan: dict) -> dict:
    result, problems = validate_plan(plan)
    if problems:
        return {"status": "rejected", "problems": problems,
                "summary": f"Plan rejected by the checker ({len(problems)} problem(s)): {problems[0]}"}
    append_history(result)
    t = result["totals"]
    summary = f"Plan accepted: {t['protein_g']} g protein, {t['calories']} kcal"
    if result["protein_gap_g"]:
        summary += f" ({result['protein_gap_g']} g short of the {result['protein_goal_g']} g goal)"
    return {"status": "accepted", "plan": result, "summary": summary + "."}


# --- History (memory of past plans) ------------------------------------------------------

def read_history() -> list[dict]:
    if config.HISTORY_FILE.exists():
        try:
            return json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def append_history(plan: dict) -> None:
    history = read_history()
    history.append({"date": plan["date"], "goal": plan.get("goal", ""), "plan": plan})
    config.HISTORY_FILE.write_text(json.dumps(history[-60:], indent=2), encoding="utf-8")
