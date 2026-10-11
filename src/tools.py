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
from datetime import date, timedelta
from pathlib import Path

import requests

import config
from src import locations, storage
from src.fetch_menu import FetchError, NotPosted, fetch_hall_day
from src.parse_menu import parse_hall_day

HALLS = list(config.HALLS)  # East and West: always considered. Other places only when asked for.

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
        return config.today()
    if str(value).lower() == "tomorrow":
        return config.today() + timedelta(days=1)
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
        if hall not in data["halls"]:
            raise FetchError(f"There's no saved sample menu for {hall}; switch Menu data to live.")
        items = data["halls"][hall]
        meta = {"source": "sample", "note": f"sample data from {data['date']}", "errors": []}
    else:
        loc = locations.get(hall)
        raw = fetch_hall_day(loc.key, day, offline=offline)
        all_day = config.RETAIL_DEFAULT_MEALS if loc.kind == locations.RETAIL else None
        items = parse_hall_day(loc.key, raw["stations"], loc.code, all_day)
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
    except NotPosted as exc:
        return [], {"status": "no_menu_posted", "note": str(exc), "errors": []}
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
    "extra_locations": [],   # other places to consider besides East and West, e.g. ["Roth Cafe"]
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
# Words to look for in item names/ingredients as a second safety net, for dishes the menu doesn't
# label. Blocking a safe dish by mistake is far better than serving an unsafe one.
_WHEAT_WORDS = ["wheat", "flour", "bread", "breaded", "pasta", "penne", "spaghetti", "macaroni", "noodle",
                "couscous", "semolina", "bulgur", "barley", "rye", "seitan", "cracker", "croissant", "bagel",
                "muffin", "pancake", "waffle", "pizza", "tortilla", "pita", "bun", "crouton", "biscuit", "cake",
                "cookie", "brownie", "pretzel", "panko", "dumpling", "soy sauce", "teriyaki"]
ALLERGY_WORDS = {
    "peanuts": ["peanut", "satay"],
    "tree_nuts": ["almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia", "pine nut",
                  "brazil nut", "praline", "marzipan", "pesto", "nutella", "frangipane"],
    "milk": ["cheese", "milk", "cream", "butter", "yogurt", "whey", "casein", "ghee", "custard", "cheddar",
             "mozzarella", "parmesan", "ricotta", "feta", "provolone", "alfredo", "queso", "paneer", "gelato",
             "latte", "ranch", "tzatziki"],
    "egg": ["egg", "mayo", "mayonnaise", "aioli", "custard", "meringue", "frittata", "quiche", "omelet",
            "omelette", "french toast"],
    "fish": ["salmon", "tuna", "cod", "tilapia", "fish", "anchovy", "anchovies", "sardine", "trout", "halibut",
             "swordfish", "mahi", "pollock", "catfish", "haddock", "flounder", "worcestershire"],
    "shellfish": ["shrimp", "crab", "lobster", "prawn", "scallop", "clam", "mussel", "oyster", "crawfish",
                  "crayfish", "calamari", "squid"],
    "sesame": ["sesame", "tahini", "hummus"],
    "soy": ["tofu", "soy", "edamame", "miso", "tempeh", "tamari", "teriyaki"],
    "gluten": _WHEAT_WORDS, "wheat": _WHEAT_WORDS,
}
# Allergy words people use, in the order they're looked for -> the name the app saves. "Severe peanut
# allergy", "I'm allergic to peanuts" and "celiac" all become something the checker enforces.
ALLERGY_TERMS = [
    (r"tree[ _-]?nuts?|almonds?|cashews?|walnuts?|pecans?|pistachios?|hazelnuts?|macadamias?|pine nuts?"
     r"|brazil nuts?", "tree_nuts"),
    (r"peanuts?|groundnuts?", "peanuts"),
    (r"nuts?", "nuts"),
    (r"milk|dairy|lactose|whey|casein", "milk"),
    (r"eggs?", "egg"),
    (r"gluten|celiac|coeliac", "gluten"),
    (r"wheat", "wheat"),
    (r"soy|soya|soybeans?", "soy"),
    (r"sesame", "sesame"),
    (r"shellfish|shrimps?|prawns?|crabs?|lobsters?|scallops?|clams?|mussels?|oysters?|crustaceans?", "shellfish"),
    (r"fish", "fish"),
]
_ALLERGY_FILLER = {"i", "im", "i'm", "am", "have", "has", "a", "an", "my", "to", "of", "and", "severe", "severely",
                   "mild", "very", "allergy", "allergies", "allergic", "intolerant", "intolerance", "anaphylactic",
                   "anaphylaxis", "sensitive", "sensitivity", "life", "threatening", "life-threatening", "reaction"}


def allergy_names(phrase: str) -> tuple[list[str], str]:
    """(allergy names the checker knows, any other food left in the phrase). "severe peanut allergy"
    -> (["peanuts"], ""); "allergic to mustard" -> ([], "mustard")."""
    text, found = " " + str(phrase).lower().replace("_", " ") + " ", []
    for pattern, name in ALLERGY_TERMS:
        regex = re.compile(rf"\b(?:{pattern})\b")
        if regex.search(text):
            if name not in found:
                found.append(name)
            text = regex.sub(" ", text)  # "tree nuts" isn't also "nuts"
    words = [w for w in re.findall(r"[a-z][a-z'-]*", text) if w not in _ALLERGY_FILLER]
    return found, " ".join(words)


def read_prefs() -> dict:
    """Saved memory, repaired so every value is one the app can show and the checker can use
    (older versions, or a model, may have saved "west", "fri" or "1,800")."""
    prefs = dict(DEFAULT_PREFS)
    prefs.update(storage.current().load_prefs() or {})
    for key in EDITABLE_PREFS:
        prefs[key] = _clean_pref(key, prefs.get(key), strict=False)
    return prefs


def write_prefs(prefs: dict) -> None:
    storage.current().save_prefs(prefs)


def allergen_codes(allergies: list[str]) -> set[str]:
    codes = set()
    for a in allergies or []:
        codes.update(ALLERGY_SYNONYMS.get(a.strip().lower(), [a.strip().lower().replace(" ", "_")]))
    return codes


def normalize_hall(hall: str) -> str:
    """Any location's key: "west side" -> "West", "roth" -> "Roth Cafe"."""
    return locations.get(hall).key


def considered(prefs: dict | None = None, extra: list[str] | None = None) -> list[str]:
    """Where to look by default: East, West, the user's extra_locations, and any `extra` named now."""
    prefs = prefs if prefs is not None else read_prefs()
    out = list(HALLS)
    for name in list(prefs.get("extra_locations") or []) + list(extra or []):
        try:
            key = normalize_hall(name)
        except ValueError:
            continue  # a place that no longer exists on Nutrislice
        if key not in out:
            out.append(key)
    return out


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
    gluten_free = "avoiding_gluten" in item.get("tags", [])  # Nutrislice's own "avoiding gluten" label
    for code in codes:
        if gluten_free and code in ("gluten", "wheat"):
            continue
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


def _exclusion(item: dict, codes: set[str], diet: list[str], never_eat: list[str],
               dislikes: list[str]) -> str | None:
    """Why the user's memory rules this item out (allergen, diet, never_eat, dislike), or None."""
    if allergy_conflicts(item, codes):
        return "allergen"
    if not diet_ok(item, diet):
        return "diet"
    if blacklist_hits(item, never_eat):
        return "never_eat"
    if dislike_hits(item, dislikes):
        return "dislike"
    return None


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
LIST_PREFS = {key for key, value in DEFAULT_PREFS.items() if isinstance(value, list)}
SET_LISTS = {"meals_to_plan", "cheat_days"}  # stated as a whole ("plan lunch and dinner"); other lists grow
# Names models often use for a memory key, and the key they mean.
PREF_ALIASES = {"protein_goal_g": "daily_protein_goal_g", "protein_goal": "daily_protein_goal_g",
                "daily_protein_goal": "daily_protein_goal_g", "calorie_goal": "daily_calorie_goal",
                "calories_goal": "daily_calorie_goal", "daily_calories": "daily_calorie_goal",
                "allergy": "allergies", "blacklist": "never_eat", "favorite_foods": "favorites"}
NUMBER_PREFS = {"daily_protein_goal_g": 400, "daily_calorie_goal": 8000, "max_calories_per_meal": 4000}  # max
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Everyday words for an allergy -> the name the app uses (anything else is kept as written).
ALLERGY_NAMES = {"dairy": "milk", "lactose": "milk", "eggs": "egg", "peanut": "peanuts", "tree nuts": "tree_nuts",
                 "tree nut": "tree_nuts", "soya": "soy", "shrimp": "shellfish", "sesame seeds": "sesame"}
# Allergies the checker can find on menus (Nutrislice's labels, plus everyday words for them).
KNOWN_ALLERGIES = set(config.ALLERGEN_SLUGS.values()) | set(ALLERGY_SYNONYMS)
KNOWN_DIETS = ("vegetarian", "vegan", "halal", "pescatarian", "gluten_free")  # what diet_ok can check
DIET_NAMES = {"veggie": "vegetarian", "plant based": "vegan", "plant-based": "vegan", "pescetarian": "pescatarian",
              "gluten free": "gluten_free", "gluten-free": "gluten_free", "celiac": "gluten_free",
              "coeliac": "gluten_free"}
MAX_ENTRIES, MAX_CHARS, MAX_NOTE_CHARS = 50, 80, 300


def _as_list(value, key: str = "") -> list[str]:
    """A list preference as a list of strings. "peanuts and shellfish" is two allergies; a note
    is never split."""
    if value is None:
        return []
    if isinstance(value, dict):
        raise ValueError("expected a list of words, not an object")
    raw = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set)) else [value]
    out = []
    for entry in raw:
        if entry in (None, "") or isinstance(entry, (dict, list)):
            continue
        parts = [str(entry)] if key == "notes" else re.split(r"[,;]", str(entry))
        if key in ("allergies", "diet"):
            parts = [p for part in parts for p in re.split(r"\s+(?:and|&)\s+|\s*[&/]\s*", part)]
        out += [p.strip() for p in parts if p.strip()]
    return out


def _as_number(value, key: str):
    """"1,800 kcal" -> 1800; 0 or empty -> None (no goal)."""
    if value in (None, "") or value is False:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(value))  # thousands separators
        found = re.search(r"\d+(?:\.\d+)?", text)
        if not found:
            raise ValueError(f"{value!r} isn't a number")
        number = float(found.group())
    if not 0 <= number <= NUMBER_PREFS[key]:
        raise ValueError(f"{value!r} is out of range (0 to {NUMBER_PREFS[key]:,})")
    return round(number) or None


def _list_entry(key: str, raw, strict: bool) -> str:
    """One entry of a list preference, in the form the app and the checker use."""
    text = " ".join(str(raw).split())
    limit = MAX_NOTE_CHARS if key == "notes" else MAX_CHARS
    if len(text) > limit:
        if strict:
            raise ValueError(f"{text[:30]!r}... is too long (at most {limit} characters)")
        text = text[:limit]
    low = text.lower()
    if key == "allergies":
        found, rest = allergy_names(low)
        if found:  # a short leftover is another food ("peanut, kiwi allergy"); a long one is just words
            return found + ([rest] if rest and len(rest.split()) <= 2 else [])
        if not rest:
            if strict:
                raise ValueError(f"couldn't tell which food {text!r} is about")
            return low
        if strict and len(rest.split()) > 3:
            raise ValueError(f"couldn't tell which food {text!r} is about; name just the food")
        return ALLERGY_NAMES.get(rest, rest)
    if key == "diet":
        name = DIET_NAMES.get(low, low.replace("-", "_").replace(" ", "_"))
        if strict and name not in KNOWN_DIETS:
            raise ValueError(f"DineWolfie can only check these diets: {', '.join(KNOWN_DIETS)}")
        return name
    if key == "meals_to_plan":
        return normalize_meal(low)  # ValueError for "second breakfast"
    if key == "cheat_days":
        day = next((d for d in WEEKDAYS if low[:3] == d[:3].lower()), None)
        if day is None:
            raise ValueError(f"{text!r} isn't a day of the week")
        return day
    if key == "extra_locations" and strict:
        return normalize_hall(text)
    return text


def _clean_pref(key: str, value, strict: bool = True):
    """A memory value in the form the app can show and the checker can use.

    strict (saving): raise ValueError for anything unusable. Not strict (reading what's already
    saved): repair what can be repaired, drop or reset the rest, never raise."""
    if key in LIST_PREFS:
        out = []
        try:
            entries = _as_list(value, key)
        except ValueError:
            if strict:
                raise
            entries = []
        for raw in entries:
            try:
                entry = _list_entry(key, raw, strict)
            except ValueError:
                if strict:
                    raise
                continue
            for one in entry if isinstance(entry, list) else [entry]:  # "peanut and egg allergy"
                if one.lower() not in (e.lower() for e in out):
                    out.append(one)
        if len(out) > MAX_ENTRIES:
            if strict:
                raise ValueError(f"too many entries (at most {MAX_ENTRIES})")
            out = out[:MAX_ENTRIES]
        return out
    try:
        if key in NUMBER_PREFS:
            return _as_number(value, key)
        if key == "treats":
            level = str(value or "sometimes").strip().lower()
            if level not in TREAT_LEVELS:
                raise ValueError(f"treats must be one of {list(TREAT_LEVELS)}")
            return level
        if key == "favorite_hall":
            text = str(value or "").strip()
            if text.lower() in ("", "none", "no preference", "any", "either"):
                return None
            if text.lower() in ("east", "west"):
                return text.title()
            return normalize_hall(text) if strict else text
        if key == "name":
            return str(value or "").strip()[:60]
    except ValueError:
        if strict:
            raise
        return DEFAULT_PREFS[key]
    return value


def update_prefs(changes: dict | None = None, reason: str = "", remove: dict | None = None) -> dict:
    """Save lasting preferences. Lists (allergies, never_eat, favorites, ...) are added to, never
    replaced, so saving one new allergy can't wipe the others; `remove` takes entries out.
    meals_to_plan and cheat_days are set as a whole."""
    changes = {PREF_ALIASES.get(k, k): v for k, v in (changes or {}).items()}
    remove = {PREF_ALIASES.get(k, k): v for k, v in (remove or {}).items()}
    unknown = sorted((set(changes) | set(remove)) - EDITABLE_PREFS)
    if unknown:
        return {"status": "error", "error": f"Unknown preference keys {unknown}. "
                                            f"Allowed: {sorted(EDITABLE_PREFS)}",
                "summary": "Rejected unknown preference keys."}
    prefs, updated, problems, notes = read_prefs(), {}, [], []
    for key, value in changes.items():
        try:
            new = _clean_pref(key, value)
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
            continue
        if key == "allergies":
            unreadable = [a for a in new if a not in KNOWN_ALLERGIES]
            if unreadable:  # menus don't label it, so also block dishes that name it
                updated["never_eat"] = _clean_pref("never_eat", list(updated.get("never_eat", prefs["never_eat"]))
                                                   + unreadable, strict=False)
                notes.append(f"Menus don't label {', '.join(unreadable)}, so DineWolfie also blocks dishes that "
                             "mention it by name or ingredient. Tell the user to double-check with dining staff.")
        if key in LIST_PREFS and key not in SET_LISTS:  # add, keeping what's there
            new = _clean_pref(key, list(updated.get(key, prefs.get(key) or [])) + new, strict=False)
        updated[key] = new
    for key, value in remove.items():
        if key not in LIST_PREFS:
            problems.append(f"{key}: only lists can have entries removed")
            continue
        if key == "allergies":
            problems.append("allergies can only be removed by the user in the memory form (sidebar), so a "
                            "misunderstanding can never unblock an allergen")
            continue
        try:
            wanted = _clean_pref(key, value, strict=False)
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
            continue
        if key == "extra_locations":
            wanted = [_known_place(w) for w in wanted]
        gone = {g.lower() for g in wanted}
        before = list(updated.get(key, prefs.get(key) or []))
        updated[key] = [v for v in before if v.lower() not in gone]
        if len(updated[key]) == len(before):
            problems.append(f"{key}: {', '.join(wanted)} isn't saved there (saved: {', '.join(before) or 'nothing'})")
    if problems:
        return {"status": "error", "error": "Nothing was saved: " + "; ".join(problems),
                "summary": "Memory not updated: " + "; ".join(problems)}
    if not updated:
        return {"status": "error", "error": "Nothing to change.", "summary": "Memory not updated: nothing to change."}
    prefs.update(updated)
    write_prefs(prefs)
    summary = f"Memory updated: {', '.join(f'{k}={v}' for k, v in updated.items())}."
    return {"status": "ok", "prefs": prefs, "summary": " ".join([summary] + notes),
            **({"note": " ".join(notes)} if notes else {})}


def _known_place(name: str) -> str:
    try:
        return normalize_hall(name)
    except ValueError:
        return name


def get_meal_history(days: int = 3) -> dict:
    history = read_history()
    cutoff = (config.today() - timedelta(days=int(days or 3))).isoformat()
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
                "summary": f"No {meal.replace('_', ' ')} menu is posted for {hall} on {day.isoformat()}."
                           + (f" {meta['note']}" if meta.get("note") else "")}
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
    prefs = read_prefs() if apply_my_prefs else dict(DEFAULT_PREFS)
    halls = (considered(read_prefs()) if (hall or "any").lower() in ("any", "both", "all")
             else [normalize_hall(hall)])
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
            excluded_by = _exclusion(item, codes, diet, never_eat, dislikes)
            if station and station.lower() not in item["station"].lower():
                counts["station"] += 1
            elif excluded_by:
                counts[excluded_by] += 1
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
    places = considered()
    report = {}
    for h in places:
        res = search_items(meal, hall=h, date=day.isoformat(), min_protein_g=min_protein_g,
                           max_calories=max_calories, sort_by="protein", limit=40)
        top = res["items"][:3]
        report[h] = {
            "menu_status": res["hall_status"][h],
            "matching_items": res["match_count"],
            "best_protein_items": [f"{i['name']} ({i['protein_g']} g protein, {i['calories']} kcal)" for i in top],
            "protein_from_top_3_g": sum(i["protein_g"] or 0 for i in top),
        }
    posted = [h for h in places if report[h]["menu_status"] == "ok"]
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
        if report[h]["menu_status"] == "ok" else f"{h} {report[h]['menu_status']}" for h in places)
        + f". Edge: {best}.")
    return {"status": "ok", "meal": meal, "date": day.isoformat(),
            "halls": report, "better_hall_by_protein": best, "summary": summary}


SCOUT_PER_HALL = 10


def _scout_place(h: str, meal: str, day, prefs: dict, per_hall: int) -> dict:
    """One place's best candidates for one meal (see scout_meal)."""
    favorites = list(prefs.get("favorites") or [])
    items, meta = _menu(h, meal, day)
    if meta["status"] != "ok":
        return {"status": meta["status"], "fits": 0, "items": [], "error": meta.get("error") or meta.get("note")}
    codes = allergen_codes(prefs["allergies"])
    fits = [i for i in items if not _exclusion(i, codes, prefs["diet"], prefs.get("never_eat"), prefs["dislikes"])]
    known = [i for i in fits if i["calories"] is not None]
    picks: dict[str, dict] = {}  # lower-cased name -> item; the same dish can sit at two stations

    def add(seq, upto):
        for item in seq:
            if len(picks) >= upto:
                return
            picks.setdefault(item["name"].strip().lower(), item)

    add([i for i in fits if is_favorite(i, favorites)], 3)
    add(sorted(known, key=SORTS["protein"]), max(3, per_hall * 6 // 10))
    add(sorted(known, key=SORTS["protein_per_calorie"]), per_hall)
    if (prefs.get("treats") or "sometimes") != "never":
        add([i for i in known if looks_like_treat(i)], len(picks) + 2)
    # A place that lists no nutrition (common at retail spots) still has real food: show it.
    add([i for i in fits if i["calories"] is None], per_hall)
    rows = []
    for item in picks.values():
        row = {"id": item["id"], "name": item["name"], "station": item["station"],
               "serving": item["serving"], "protein_g": item["protein_g"], "calories": item["calories"]}
        if is_favorite(item, favorites):
            row["favorite"] = True
        if looks_like_treat(item):
            row["treat"] = True
        rows.append(row)
    return {"status": "ok" if rows else "no_matches", "fits": len(fits), "items": rows}


def _short(text, limit: int = 90) -> str:
    text = " ".join(str(text or "unknown error").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def scout_meal(meal: str, date: str | None = None, per_hall: int = SCOUT_PER_HALL,
               places: list[str] | None = None) -> dict:
    """Fast mode: one meal's best candidates at each hall, already filtered by the user's memory.

    Favorites first, then the highest-protein items, then the most protein per calorie (for
    cutting), plus up to two treats. That's usually enough for the model to choose a whole meal
    without searching again; it can still call search_items for anything else.
    """
    day, meal = resolve_date(date), normalize_meal(meal)
    prefs = read_prefs()
    places = places or considered(prefs)
    halls = {}
    for h in places:
        try:
            halls[h] = _scout_place(h, meal, day, prefs, per_hall)
        except Exception as exc:  # one broken place must not hide the others
            halls[h] = {"status": "error", "fits": 0, "items": [], "error": f"{type(exc).__name__}: {exc}"}
    posted = [h for h in places if halls[h]["status"] != "no_menu_posted" and halls[h]["status"] != "error"]
    status = ("ok" if any(halls[h]["status"] == "ok" for h in places)
              else "no_matches" if posted else "no_menu_posted")
    parts = []
    for h in places:
        info = halls[h]
        rated = [r for r in info["items"] if r["protein_g"] is not None]
        if info["status"] == "ok" and rated:
            best = max(rated, key=lambda r: r["protein_g"])
            parts.append(f"{h} {info['fits']} fit you (top: {best['name']}, {best['protein_g']} g)")
        elif info["status"] == "ok":
            parts.append(f"{h} {info['fits']} fit you (no nutrition listed)")
        elif info["status"] == "no_matches":
            parts.append(f"{h} nothing fits your diet/allergies")
        elif info["status"] == "error":
            parts.append(f"{h} couldn't load ({_short(info.get('error'))})")
        else:
            parts.append(f"{h} {info['status'].replace('_', ' ')}")
    return {"status": status, "meal": meal, "date": day.isoformat(), "halls": halls,
            "summary": f"{meal.replace('_', ' ').capitalize()}: " + "; ".join(parts) + "."}


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
    loc = locations.for_item_id(item_id)
    if loc is None:
        return None
    try:
        items, _ = load_items(loc.key, day)
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
        servings = _servings(entry.get("servings") if isinstance(entry, dict) else 1)
        if servings is None:
            unknown.append(f"{item_id} (servings must be a number)")
            continue
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


def _servings(value) -> float | None:
    """1, 2.5 or "2" -> a number (missing -> 1); "two" -> None."""
    if value in (None, ""):
        return 1.0
    try:
        return float(value) or 1.0
    except (TypeError, ValueError):
        return None


MEAL_SHAPE = '{"meal": "lunch", "hall": "East", "items": [{"id": "E123", "servings": 1}], "reason": "..."}'
SWAP_SHAPE = '{"hall": "East", "items": [{"id": "E123"}], "note": "..."}'


def _listed(value) -> list:
    """What a model sent as a list: a list as is, a single object (or id) as a list of one, nothing as []."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _texts(value) -> list[str]:
    return [str(v).strip() for v in _listed(value) if str(v).strip()]


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
        for entry in _listed(entries):
            entry = entry if isinstance(entry, dict) else {"id": entry}
            item_id = str(entry.get("id", "")).upper()
            servings = _servings(entry.get("servings"))
            if servings is None:
                problems.append(f"{label}: servings for {item_id} must be a number, like 1 or 2.")
                continue
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
    for n, meal_plan in enumerate(_listed(plan.get("meals")), 1):
        if not isinstance(meal_plan, dict) or "meal" not in meal_plan:
            problems.append(f"Meal {n} must be an object like {MEAL_SHAPE}, not {repr(meal_plan)[:60]}.")
            continue
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
        for n, alt in enumerate(_listed(meal_plan.get("alternatives")), 1):
            if not isinstance(alt, dict):
                problems.append(f"{meal} swap {n} must be an object like {SWAP_SHAPE}, not {repr(alt)[:60]}.")
                continue
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
        "message": str(plan.get("message") or "").strip(),
        "meals": meals_out,
        "totals": totals["totals"],
        "items_missing_nutrition": totals["items_missing_nutrition"],
        "protein_goal_g": goal,
        "protein_gap_g": max(0, round(goal - totals["totals"]["protein_g"])) if goal else None,
        "calorie_goal": prefs.get("daily_calorie_goal") if full_day else None,
        "full_day": full_day,
        "adaptations": _texts(plan.get("adaptations")),
        "tips": _texts(plan.get("tips")),
        "cheat_day": is_cheat_day(day, prefs),
        "created_at": config.now().isoformat(timespec="seconds"),
    }
    return result, problems


def submit_plan(plan: dict | None = None, **fields) -> dict:
    """Accepts the plan as one dict or as keyword fields (date=..., meals=...)."""
    plan = {**(plan or {}), **fields}
    result, problems = validate_plan(plan)
    if problems:
        return {"status": "rejected", "problems": problems,
                "summary": f"Plan rejected by the checker ({len(problems)} problem(s)): {problems[0]}"}
    t = result["totals"]
    summary = f"Plan accepted: {t['protein_g']} g protein, {t['calories']} kcal"
    if result["protein_gap_g"]:
        summary += f" ({result['protein_gap_g']} g short of the {result['protein_goal_g']} g goal)"
    accepted = {"status": "accepted", "plan": result, "summary": summary + "."}
    try:
        append_history(result)
    except Exception as exc:  # e.g. the account database is paused: the plan is still good
        accepted["warning"] = f"The plan couldn't be saved to your past plans ({exc})."
        accepted["summary"] += " " + accepted["warning"]
    return accepted


# --- History (memory of past plans) ------------------------------------------------------

def read_history() -> list[dict]:
    return storage.current().load_history()


def append_history(plan: dict) -> None:
    """Remember an accepted plan, one per date: a newer plan for the same day (queued again, or
    changed with a follow-up) replaces the older one."""
    history = [h for h in read_history() if not isinstance(h, dict) or h.get("date") != plan["date"]]
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
