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
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import config
from src import storage
from src.fetch_menu import FetchError, fetch_hall_day
from src.parse_menu import parse_hall_day

HALLS = list(config.HALLS)

# --- Per-visitor settings ----------------------------------------------------------
# On the hosted site many people use the app at once, so the demo switches, the data
# mode and the Tavily key live in a ContextVar (one value per session) instead of globals.
_DEFAULT_SESSION = {"missing": frozenset(), "offline": False, "data_mode": None, "tavily_key": None}
_session: ContextVar = ContextVar("dinewolfie_session", default=_DEFAULT_SESSION)


def _settings() -> dict:
    return _session.get()


def configure(**changes) -> None:
    """Change this session's settings: data_mode='sample', tavily_key='tvly-...', etc."""
    _session.set({**_session.get(), **changes})


def data_mode() -> str:
    return _settings()["data_mode"] or config.DATA_MODE


def tavily_key() -> str | None:
    return _settings()["tavily_key"] or os.getenv("TAVILY_API_KEY") or None


# --- Demo "chaos" switches ------------------------------------------------------
# Lets you show on camera how the agent adapts when things break. The agent is
# NOT told these are simulated; it just sees a missing menu or a network error.

def set_simulation(missing: list[tuple[str, str]] | None = None, offline: bool = False) -> None:
    configure(missing=frozenset((h.title(), m.lower()) for h, m in (missing or [])), offline=bool(offline))


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
_DEFAULT_SESSION.update(_session.get())  # env switches (CLI) become the default for every session


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
    offline = _settings()["offline"]
    key = (hall, day, data_mode(), offline)
    if key in _day_cache:
        return _day_cache[key]

    if data_mode() == "sample":
        path = _sample_file(day)
        if path is None:
            raise FetchError("No sample data in data/sample/.")
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["halls"].get(hall, [])
        meta = {"source": "sample", "note": f"sample data from {data['date']}", "errors": []}
    else:
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
    if (hall, meal) in _settings()["missing"]:
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
            selected.append({**item, "station": item.get("station_by_meal", {}).get(hit[0], item["station"]),
                             "section": item.get("section_by_meal", {}).get(hit[0], item.get("section", ""))})
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
    "dislikes": [],          # soft: avoided unless nothing else works
    "never_eat": [],         # hard blacklist: the checker rejects these, like allergies
    "favorites": [],         # preferred when they're on the menu
    "treats": "sometimes",   # never | sometimes | often
    "cheat_days": [],        # e.g. ["Friday"]: relaxed calories, a treat is welcome
    "meals_to_plan": ["breakfast", "lunch", "dinner"],
    "notes": [],
}
TREAT_LEVELS = ("never", "sometimes", "often")

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
    prefs.update(storage.current().load_prefs() or {})
    return prefs


def write_prefs(prefs: dict) -> None:
    storage.current().save_prefs(prefs)


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


def blacklist_hits(item: dict, never_eat: list[str]) -> list[str]:
    """Hard blacklist: same keyword matching as dislikes, but enforced by the checker."""
    return dislike_hits(item, never_eat)


def is_favorite(item: dict, favorites: list[str]) -> bool:
    return bool(dislike_hits(item, favorites))


TREAT_WORDS = ("cookie", "brownie", "cake", "pie", "donut", "doughnut", "muffin", "ice cream", "fries",
               "pudding", "cheesecake", "cupcake", "churro", "milkshake", "croissant", "danish",
               "cinnamon roll", "nachos", "onion rings", "mozzarella sticks", "pizza", "waffle", "pancake")


def looks_like_treat(item: dict) -> bool:
    name = item["name"].lower()
    return item.get("station", "").lower().startswith("dessert") or any(w in name for w in TREAT_WORDS)


def is_cheat_day(day: date, prefs: dict | None = None) -> bool:
    prefs = prefs or read_prefs()
    return day.strftime("%A").lower() in {d.lower() for d in prefs.get("cheat_days") or []}


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
    if prefs.get("never_eat"):
        bits.append(f"never: {', '.join(prefs['never_eat'])}")
    if prefs.get("favorites"):
        bits.append(f"loves: {', '.join(prefs['favorites'])}")
    if prefs.get("cheat_days"):
        bits.append(f"cheat days: {', '.join(prefs['cheat_days'])}")
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


def menu_items(hall: str, meal: str, date: str | None = None) -> list[dict]:
    """Full items for one hall + meal, in Nutrislice's order (for the menu browser)."""
    items, _ = _menu(normalize_hall(hall), normalize_meal(meal), resolve_date(date))
    return items


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
    never_eat = list(prefs.get("never_eat") or [])
    favorites = list(prefs.get("favorites") or [])

    kept, counts, hall_status = [], {"allergen": 0, "diet": 0, "never_eat": 0, "dislike": 0,
                                     "no_nutrition": 0, "protein": 0, "calories": 0, "station": 0}, {}
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
            elif blacklist_hits(item, never_eat):
                counts["never_eat"] += 1
            elif dislike_hits(item, dislikes):
                counts["dislike"] += 1
            elif (min_protein_g or max_calories) and item["calories"] is None:
                counts["no_nutrition"] += 1
            elif min_protein_g and (item["protein_g"] or 0) < float(min_protein_g):
                counts["protein"] += 1
            elif max_calories and (item["calories"] or 0) > float(max_calories):
                counts["calories"] += 1
            else:
                row = {**compact(item), "hall": h}
                if is_favorite(item, favorites):
                    row["favorite"] = True
                if looks_like_treat(item):
                    row["treat"] = True
                kept.append(row)

    kept.sort(key=SORTS.get(sort_by, SORTS["protein_per_calorie"]))
    if favorites:  # favorites float to the top, otherwise keep the chosen order
        kept.sort(key=lambda i: not i.get("favorite"))
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
            "filters_applied": {"allergens_avoided": sorted(codes), "diet": diet, "never_eat": never_eat,
                                "dislikes": dislikes,
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
    posted = [h for h in HALLS if report[h]["menu_status"] == "ok"]
    if not posted:
        return {"status": "no_menu_posted", "meal": meal, "date": day.isoformat(), "halls": report,
                "better_hall_by_protein": None,
                "summary": f"For {meal} {day.isoformat()}: neither hall has a menu posted."}
    if not any(report[h]["matching_items"] for h in posted):
        # Nothing passes the filters: say what the best item actually is, so the agent can adapt
        # (combine items, use extra servings, or relax the filter) instead of picking a hall at random.
        best_any = []
        for h in posted:
            top = search_items(meal, hall=h, date=day.isoformat(), sort_by="protein", limit=1)["items"]
            if top:
                best_any.append((top[0]["protein_g"] or 0, h, top[0]["name"]))
        best_any.sort(reverse=True)
        hint = (f" The highest-protein single item is {best_any[0][2]} at {best_any[0][1]} "
                f"({best_any[0][0]} g per serving)." if best_any else "")
        limits = " and ".join(x for x in [f"at least {min_protein_g:g} g protein" if min_protein_g else "",
                                          f"at most {max_calories:g} kcal" if max_calories else ""] if x)
        return {"status": "no_matches", "meal": meal, "date": day.isoformat(), "halls": report,
                "better_hall_by_protein": None,
                "summary": f"No {meal} item at either hall has {limits or 'a match'} in one serving.{hint} "
                           "Combine several items or servings, or relax the filter."}
    ranked = sorted(posted, key=lambda h: (report[h]["protein_from_top_3_g"], report[h]["matching_items"]),
                    reverse=True)
    best = ranked[0]
    summary = (f"For {meal} {day.isoformat()}: " + "; ".join(
        f"{h} {report[h]['matching_items']} matches, top-3 protein {report[h]['protein_from_top_3_g']} g"
        if report[h]["menu_status"] == "ok" else f"{h} {report[h]['menu_status']}" for h in HALLS)
        + f". Edge: {best}.")
    return {"status": "ok", "meal": meal, "date": day.isoformat(),
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

def _totals(rows: list[dict], day: date) -> dict:
    if not rows:
        return {}
    return sum_nutrition([{"id": i["id"], "servings": i["servings"]} for i in rows], day.isoformat())["totals"]


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

    def check_items(entries: list, hall: str, meal: str, on_menu: dict, label: str) -> list[dict]:
        """Validate one list of {id, servings, treat} against the menu and the user's hard rules."""
        out = []
        for entry in entries or []:
            item_id = str(entry.get("id", "")).upper()
            servings = float(entry.get("servings", 1) or 1)
            item = on_menu.get(item_id)
            if item is None:
                problems.append(f"{label}: item id {item_id!r} is not on the {hall} {meal} menu for {day}. "
                                "Only use ids returned by get_menu/search_items for that hall and meal.")
                continue
            bad = allergy_conflicts(item, codes)
            if bad:
                problems.append(f"{label}: {item['name']} conflicts with allergies {bad}. Remove it.")
            if not diet_ok(item, prefs["diet"]):
                problems.append(f"{label}: {item['name']} doesn't fit the diet {prefs['diet']}. Remove it.")
            banned = blacklist_hits(item, prefs.get("never_eat"))
            if banned:
                problems.append(f"{label}: {item['name']} is on the never-eat list ({', '.join(banned)}). "
                                "Remove it.")
            if not 0.25 <= servings <= 4:
                problems.append(f"{label}: servings for {item['name']} must be between 0.25 and 4.")
            row = {**compact(item), "servings": servings}
            if entry.get("treat"):
                row["treat"] = True
            if is_favorite(item, prefs.get("favorites")):
                row["favorite"] = True
            out.append(row)
        return out
    for meal_plan in plan.get("meals", []):
        try:
            meal = normalize_meal(meal_plan.get("meal", ""))
            hall = normalize_hall(meal_plan.get("hall", ""))
        except ValueError as exc:
            problems.append(str(exc))
            continue
        menu_items, meta = _menu(hall, meal, day)
        on_menu = {i["id"].upper(): i for i in menu_items}
        if meta["status"] != "ok":
            problems.append(f"{meal}: {hall} has no {meal} menu ({meta['status']}). Pick another hall.")
        items_out = check_items(meal_plan.get("items", []), hall, meal, on_menu, meal)
        all_entries.extend({"id": i["id"], "servings": i["servings"]} for i in items_out)

        alternatives = []
        for n, alt in enumerate(meal_plan.get("alternatives") or [], 1):
            try:
                alt_hall = normalize_hall(alt.get("hall") or hall)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            alt_menu = on_menu if alt_hall == hall else {
                i["id"].upper(): i for i in _menu(alt_hall, meal, day)[0]}
            alt_items = check_items(alt.get("items", []), alt_hall, meal, alt_menu, f"{meal} swap {n}")
            if alt_items:
                alternatives.append({"hall": alt_hall, "items": alt_items, "note": alt.get("note", ""),
                                     "totals": _totals(alt_items, day)})
        meals_out.append({"meal": meal, "hall": hall, "items": items_out,
                          "reason": meal_plan.get("reason", ""), "totals": _totals(items_out, day),
                          "alternatives": alternatives})
    if not meals_out:
        problems.append("The plan has no meals.")
    totals = sum_nutrition(all_entries, day.isoformat())
    # A daily goal only makes sense for a full-day plan, not "just plan dinner".
    planned = {m["meal"] for m in meals_out}
    covered = planned | ({"breakfast", "lunch"} if "brunch" in planned else set())
    full_day = set(prefs.get("meals_to_plan") or config.MEALS) <= covered
    goal = prefs.get("daily_protein_goal_g") if full_day else None
    result = {
        "date": day.isoformat(),
        "goal": plan.get("goal", ""),
        "headline": plan.get("headline", ""),
        "meals": meals_out,
        "totals": totals["totals"],
        "items_missing_nutrition": totals["items_missing_nutrition"],
        "protein_goal_g": goal,
        "protein_gap_g": max(0, round(goal - totals["totals"]["protein_g"])) if goal else None,
        "full_day": full_day,
        "adaptations": plan.get("adaptations", []),
        "tips": plan.get("tips", []),
        "cheat_day": is_cheat_day(day, prefs),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return result, problems


def submit_plan(plan: dict | None = None, **fields) -> dict:
    """Accepts the plan as one dict or as keyword fields (date=..., meals=...)."""
    plan = {**(plan or {}), **fields}
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
    return storage.current().load_history()


def append_history(plan: dict) -> None:
    history = read_history()
    history.append({"date": plan["date"], "goal": plan.get("goal", ""), "plan": plan})
    storage.current().save_history(history)


# --- Web nutrition lookup (Tavily) --------------------------------------------------------

def lookup_nutrition(item_name: str, details: str = "") -> dict:
    """Search the web for typical nutrition of an item SBU's menu lists without nutrition.

    The result is an estimate from the web, not SBU data: it's shown to the user labeled
    as such and never added to plan totals (sum_nutrition and the checker only use menu data).
    """
    key = tavily_key()
    if not key:
        return {"status": "error", "error": "Web lookup isn't set up (no Tavily key).",
                "summary": "Web nutrition lookup is off."}
    query = f"{item_name} {details} nutrition facts per serving calories protein".strip()
    try:
        resp = requests.post("https://api.tavily.com/search", timeout=20,
                             headers={"Authorization": f"Bearer {key}"},
                             json={"query": query, "search_depth": "basic", "max_results": 3,
                                   "include_answer": "basic"})
    except requests.RequestException as exc:
        return {"status": "error", "error": f"Web lookup failed: {exc}", "summary": "Web lookup failed."}
    if resp.status_code != 200:
        return {"status": "error", "error": f"Web lookup failed (HTTP {resp.status_code}).",
                "summary": f"Web lookup failed (HTTP {resp.status_code})."}
    data = resp.json()
    sources = [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:300]}
               for r in data.get("results", [])[:3]]
    return {"status": "ok", "item": item_name, "answer": data.get("answer") or "", "sources": sources,
            "note": "Web estimate for a typical version of this dish, not SBU's recipe. Tell the user it's an "
                    "estimate and don't add these numbers to plan totals.",
            "summary": f"Looked up typical nutrition for {item_name} on the web ({len(sources)} sources)."}
