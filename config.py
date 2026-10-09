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
# Every other location (Roth Cafe, ...) is discovered from SCHOOLS_URL: see src/locations.py.
HALLS = {
    "East": {"school_id": 6333, "slug": "east-side-dining", "name": "East Side Dine-In"},
    "West": {"school_id": 6334, "slug": "west-side-dining", "name": "West Side Dine-In"},
}

# Roth Café's food concepts (from stonybrook.edu/dining). A Nutrislice location whose name mentions
# Roth or one of these is gathered into the "Roth Cafe" location (src/locations.py).
ROTH_WORDS = ("roth", "smash n", "smash-n", "fuze", "savor", "cocina fresca", "cocina-fresca", "popeyes",
              "pasta saute", "pasta-saute", "the drop")

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

# live   = today's menus from Nutrislice, cached in data/menus/ (default)
# sample = the menus saved in data/sample/ (works offline, any date; used by the tests)
DATA_MODE = os.getenv("DINEWOLFIE_DATA_MODE", "live").strip().lower()

# SBU Campus Dining has given permission, so contacting Nutrislice is ON by default.
# DINEWOLFIE_LIVE_FETCH=off sends no request at all (only cached or sample menus are used).
LIVE_FETCH = os.getenv("DINEWOLFIE_LIVE_FETCH", "on").strip().lower() in ("on", "1", "true", "yes")

# --- Agent -------------------------------------------------------------------
# fast (default): the code gathers memory + menus first and the model decides in about one call.
# thorough: the model drives every step itself (slower, shows more of its reasoning).
MODE = os.getenv("DINEWOLFIE_MODE", "fast").strip().lower() or "fast"
# Claude Haiku 5.5 is the fastest, cheapest current Claude model ($0.10 / $0.50 per million tokens).
# Bigger options: claude-sonnet-5-5 ($2 / $10) or claude-opus-5-5 ($4 / $20).
MODEL = os.getenv("DINEWOLFIE_MODEL", "claude-haiku-5-5").strip() or None
CLAUDE_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
EFFORT = os.getenv("DINEWOLFIE_EFFORT", "medium").strip() or None            # thorough mode
FAST_EFFORT = os.getenv("DINEWOLFIE_FAST_EFFORT", "low").strip() or None     # fast mode
MAX_TURNS = int(os.getenv("DINEWOLFIE_MAX_TURNS", "40"))
FAST_MAX_TURNS = int(os.getenv("DINEWOLFIE_FAST_MAX_TURNS", "8"))
# Optional second engine. Any Groq model with tool use works; see console.groq.com/docs/models.
GROQ_MODEL = os.getenv("DINEWOLFIE_GROQ_MODEL", "openai/gpt-oss-120b").strip()
GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
# When a Groq model hits its per-minute limit, the next model in this list is tried at once
# (Groq's limits are per model, so each one is a separate budget). Unknown or retired models
# are skipped automatically.
GROQ_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "DINEWOLFIE_GROQ_FALLBACKS",
    "openai/gpt-oss-20b,llama-3.3-70b-versatile,meta-llama/llama-4-scout-17b-16e-instruct").split(",") if m.strip()]
# If every AI option is rate-limited, make the plan with the built-in planner (no AI) instead of
# making the student wait. The plan is labeled so it's clear the AI didn't make it.
FALLBACK_TO_BUILTIN = os.getenv("DINEWOLFIE_FALLBACK_BUILTIN", "on").strip().lower() in ("on", "1", "true", "yes")
# Gemini and Cerebras (both optional, OpenAI-compatible). "auto" asks the service for its model list
# and picks the newest suitable one, so renamed models keep working. The lists are used when the
# service can't be asked, and as fallbacks; models it doesn't have are skipped.
GEMINI_MODEL = os.getenv("DINEWOLFIE_GEMINI_MODEL", "auto").strip() or "auto"
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "DINEWOLFIE_GEMINI_FALLBACKS", "gemini-3.8-flash,gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]
CEREBRAS_MODEL = os.getenv("DINEWOLFIE_CEREBRAS_MODEL", "auto").strip() or "auto"
CEREBRAS_MODELS = [m.strip() for m in os.getenv("DINEWOLFIE_CEREBRAS_FALLBACKS", "gpt-oss-120b").split(",")
                   if m.strip()]
# How long thinking models (gpt-oss, Gemini) think before answering: low | medium | high.
GROQ_REASONING_EFFORT = os.getenv("DINEWOLFIE_GROQ_REASONING", "low").strip().lower() or "low"

# --- Notifications -------------------------------------------------------------
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
