# Data notes: SBU's Nutrislice menus

Confirmed on 2026-10-03 by opening https://stonybrook.nutrislice.com/menu, clicking **View Menus**, and reading the browser's network requests.

## Endpoints

| What | URL |
|---|---|
| All dining locations and their stations | `https://stonybrook.api.nutrislice.com/menu/api/schools/` |
| One station, one week (Sunday to Saturday) | `https://stonybrook.api.nutrislice.com/menu/api/weeks/school/{school_id}/menu-type/{menu_type_id}/{YYYY}/{MM}/{DD}/` |

Nutrislice calls a location a **school** and a station a **menu type**. The URL template for each station is listed in the schools response under `active_menu_types[].urls.full_menu_by_date_api_url_template`.

| Hall | school id | slug |
|---|---|---|
| East Side Dine-In | 6333 | `east-side-dining` |
| West Side Dine-In | 6334 | `west-side-dining` |

Each hall has about 15 to 18 stations (Grill, Deli, Rooted, Pasta Specials, Omelet Bar, Salad Bar, and so on). We skip `todays-dine-in-specials-esd/-wsd` and `avoiding-gluten-online-menu` because they repeat items from the other stations.

**Important difference from the original plan:** SBU's menus are organized by *station*, not by breakfast/lunch/dinner. The meal comes from the section headers inside each station (see below).

## Week response

```
{ "start_date", "menu_type_id", "last_updated",
  "days": [ { "date": "2026-10-03", "menu_items": [ ... ] }, ... ] }
```

Each entry in `menu_items` is either:

* a **section header**: `is_station_header: true` or `is_section_title: true`, with the header in `text`, for example `"Rooted Dinner Specials"`, `"Grill Lunch Specials"`, `"Hot Breakfast Buffet"`, `"Late Night Specials"`, or
* a **food row**: `food` is an object (see below), with `position` and `menu_id` giving the order.

## Food fields we use

| Nutrislice field | Our field | Notes |
|---|---|---|
| `food.id` | `id` | Prefixed with the hall letter: `W2435942` |
| `food.name` | `name` | |
| `food.description` | `description` | |
| `food.rounded_nutrition_info.calories` | `calories` | Per serving |
| `...g_protein`, `g_carbs`, `g_fat`, `g_fiber`, `g_sugar` | `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `sugar_g` | |
| `...mg_sodium` | `sodium_mg` | |
| `food.has_nutrition_info` | `has_nutrition` | About 6% of items are `false`; all their nutrients become `None` |
| `food.serving_size_info.serving_size_amount` + `serving_size_unit` | `serving` | for example `"4 each"`, `"0.5 cups"` |
| `food.icons.food_icons[].slug` | `allergens` / `tags` | see below |
| `food.ingredients` | `ingredients` | Free text, used as a second check for allergy keywords |

### Icons (allergens and tags)

Both allergens and diet tags arrive as "food icons". Seen in a week of East + West data:

* **Allergens (contains):** `egg`, `fish`, `milk`, `sesame`, `shellfish`, `shellfish-shrimp`, `soy`, `tree-nuts`, `wheat`, `contains-gluten`
* **Tags:** `vegan`, `vegetarian`, `halal`, `avoiding-gluten`, `eat-well`, `plant-centric-entree`, `locally-sourced`, `climate-friendly`

Mapping lives in `config.py` (`ALLERGEN_SLUGS`, `TAG_SLUGS`). Vegan items always get the `vegetarian` tag too.

## Meals

From the section header text (first match wins): "late night" → `late_night`; "brunch" → `brunch`; "breakfast", "morning", "omelet", "pancake", "waffle", "cereal" → `breakfast`; "lunch" → `lunch`; "dinner" → `dinner`.

Headers without a meal word ("Grill", "Deli Breads", "Salad Bar") fall back to the station's default in `config.STATION_DEFAULT_MEALS`; anything else is assumed to be served at lunch and dinner. The same food can appear in several sections (French fries at Grill lunch and Grill dinner); we keep one copy and record every meal it is served at.

## Size and politeness

* A week for one station is about 300 KB of JSON. One hall-day needs about 17 requests the first time, then everything comes from disk.
* Cache layout: `data/menus/<week start>/<hall>/<menu_type_id>-<slug>.json`, each wrapped as `{fetched_at, url, data}`.
* Live fetching is off unless `DINEWOLFIE_LIVE_FETCH=on` (permission from SBU Campus Dining has been requested, not yet granted). Any refusal or rate limit (HTTP 401/403/429 or Retry-After) stops all fetching for the rest of the run.
* A cached week is reused all day. It is refreshed at most once per day (menus do change), and weeks fully in the past are never refetched.
* Requests are spaced 0.4 s apart and identify the project in the User-Agent. HTTP 401/403/429 stops fetching instead of retrying.
* The committed `data/sample/` days are normalized (not raw), so tests and demos run offline.
