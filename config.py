"""Project-wide settings for DineWolfie.

Everything about *where* the menu data lives is in this one file, so if
Nutrislice ever changes its URLs you only have to edit this.
See docs/data-notes.md for how these values were confirmed.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

APP_NAME = "DineWolfie"

# --- Nutrislice -------------------------------------------------------------
# Confirmed 2026-10-03 from the browser's network requests on
# https://stonybrook.nutrislice.com/menu
API_BASE = "https://stonybrook.api.nutrislice.com/menu/api"
SCHOOLS_URL = f"{API_BASE}/schools/"
# One request returns a whole Sunday-to-Saturday week for one station.
WEEK_URL = API_BASE + "/weeks/school/{school_id}/menu-type/{menu_type_id}/{y}/{m:02d}/{d:02d}/"

USER_AGENT = f"{APP_NAME}/1.0 (SBU AI Community student project; cached, at most one fetch per station per day)"
REQUEST_TIMEOUT_S = 20
POLITE_DELAY_S = 0.4  # pause between requests so we never hammer the server

# The two all-you-care-to-eat dining halls. "school" is Nutrislice's word for a location.
HALLS = {
    "East": {"school_id": 6333, "slug": "east-side-dining", "name": "East Side Dine-In"},
    "West": {"school_id": 6334, "slug": "west-side-dining", "name": "West Side Dine-In"},
}

# Menu types we skip because they only repeat other stations' items.
SKIP_MENU_TYPE_SLUGS = {
    "avoiding-gluten-online-menu",
    "todays-dine-in-specials-esd",
    "todays-dine-in-specials-wsd",
}

# --- Meals -------------------------------------------------------------------
MEALS = ["breakfast", "lunch", "dinner"]
ALL_MEALS = ["breakfast", "brunch", "lunch", "dinner", "late_night"]

# Each section header usually names its meal ("Rooted Dinner Specials").
# Keywords are checked in this order against the lower-cased header text.
MEAL_KEYWORDS = [
    ("late_night", ("late night",)),
    ("brunch", ("brunch",)),
    ("breakfast", ("breakfast", "morning", "omelet", "pancake", "waffle", "cereal")),
    ("lunch", ("lunch",)),
    ("dinner", ("dinner",)),
]

# When a header doesn't name a meal, use what the station serves.
# Anything not listed here is assumed to be available at lunch and dinner.
STATION_DEFAULT_MEALS = {
    "dine-in-self-serve-pancakes-and-waffles": ["breakfast"],
    "dine-in-hot-breakfast-buffet": ["breakfast"],
    "dine-in-hot-cereal": ["breakfast"],
    "dine-in-omelet-bar": ["breakfast"],
    "dine-in-bagel-bar": ["breakfast"],
    "yogurt-fruit-bar": ["breakfast", "lunch", "dinner"],
    "dine-in-salad-bar": ["lunch", "dinner"],
    "dine-in-dessert": ["lunch", "dinner"],
    "argo-tea": ["breakfast", "lunch", "dinner"],
    "dine-in-late-night": ["late_night"],
}
DEFAULT_MEALS = ["lunch", "dinner"]

# On weekends a hall may serve brunch instead of breakfast + lunch, so a
# request for breakfast or lunch also accepts brunch items.
MEAL_ALIASES = {
    "breakfast": ["breakfast", "brunch"],
    "lunch": ["lunch", "brunch"],
    "dinner": ["dinner"],
    "brunch": ["brunch", "breakfast", "lunch"],
    "late_night": ["late_night"],
}

# Nutrislice "food icons", by slug. Allergens are things an item CONTAINS;
# tags describe the item (diet, sourcing). Unknown slugs become tags.
ALLERGEN_SLUGS = {
    "egg": "egg", "fish": "fish", "milk": "milk", "sesame": "sesame", "soy": "soy",
    "wheat": "wheat", "contains-gluten": "gluten", "tree-nuts": "tree_nuts",
    "peanuts": "peanuts", "peanut": "peanuts", "shellfish": "shellfish",
    "shellfish-shrimp": "shellfish",
}
TAG_SLUGS = {
    "vegan": "vegan", "vegetarian": "vegetarian", "halal": "halal",
    "avoiding-gluten": "avoiding_gluten", "eat-well": "eat_well",
    "plant-centric-entree": "plant_centric", "locally-sourced": "local",
    "climate-friendly": "low_emissions",
}

# --- Files -------------------------------------------------------------------
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "menus"
SAMPLE_DIR = DATA_DIR / "sample"
PREFS_FILE = Path(os.getenv("DINEWOLFIE_PREFS", ROOT / "prefs.json"))
HISTORY_FILE = Path(os.getenv("DINEWOLFIE_HISTORY", ROOT / "history.json"))

# sample = the menus saved in data/sample/ (default)
# live   = today's menus, from the cache in data/menus/ (and from Nutrislice only if LIVE_FETCH is on)
DATA_MODE = os.getenv("DINEWOLFIE_DATA_MODE", "sample").strip().lower()

# Contacting Nutrislice is OFF unless DINEWOLFIE_LIVE_FETCH=on. Only turn it on once SBU
# Campus Dining has given permission. When off, no request is ever sent.
LIVE_FETCH = os.getenv("DINEWOLFIE_LIVE_FETCH", "off").strip().lower() in ("on", "1", "true", "yes")

# --- Agent -------------------------------------------------------------------
MODEL = os.getenv("DINEWOLFIE_MODEL", "claude-opus-5-5").strip() or None
EFFORT = os.getenv("DINEWOLFIE_EFFORT", "medium").strip() or None
MAX_TURNS = int(os.getenv("DINEWOLFIE_MAX_TURNS", "40"))
# Optional second engine. Any Groq model with tool use works; see console.groq.com/docs/models.
GROQ_MODEL = os.getenv("DINEWOLFIE_GROQ_MODEL", "openai/gpt-oss-120b").strip()

# --- Notifications -------------------------------------------------------------
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
