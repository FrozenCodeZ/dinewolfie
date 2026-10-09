"""Fast mode: the code does the scouting, the model does the deciding.

Every full-day plan starts with the same lookups: read memory, check recent plans, and see what
each hall serves at each meal. In thorough mode the model asks for each one, and every ask is a
round trip that re-sends the whole conversation (about 12 calls and 40,000 tokens per plan).

In fast mode the code runs those lookups itself before the model's first call and hands over one
compact briefing. The model can usually pick the meals and submit them in a single call; the
checker still verifies every item, and the model can still call search_items for anything the
briefing lacks. The lookups show up in the live trace like any other tool call, marked "auto".
"""
from __future__ import annotations

from datetime import date

import config
from src import agent as A
from src import tools as T

MEAL_WORDS = {"breakfast": "breakfast", "brunch": "brunch", "lunch": "lunch", "dinner": "dinner",
              "late night": "late_night", "late-night": "late_night"}


def meals_for(goal: str, prefs: dict) -> list[str]:
    """The meals to scout: the user's usual meals plus any other meal the goal names."""
    meals = list(prefs.get("meals_to_plan") or config.MEALS)
    low = goal.lower()
    for word, meal in MEAL_WORDS.items():
        if word in low and meal not in meals:
            meals.append(meal)
    return meals


def _auto(name: str, fn, args: dict) -> dict:
    """Run one lookup and report it to the live trace, marked as done by code."""
    A.tool_started(name, args, auto=True)
    try:
        result = fn(**args)
    except Exception as exc:  # a broken lookup becomes a note in the briefing, not a crash
        message = f"{type(exc).__name__}: {exc}"
        A.tool_finished(name, args, None, message, auto=True)
        return {"status": "error", "error": message}
    A.tool_finished(name, args, result, auto=True)
    return result


def gather(plan_date: date, goal: str) -> str:
    """Run the standard lookups and return the briefing text for the model's first message."""
    prefs = _auto("get_prefs", T.get_prefs, {}).get("prefs") or T.read_prefs()
    recent = _auto("get_meal_history", T.get_meal_history, {"days": 3}).get("recent_plans", [])
    scouts = [_auto("scout_meal", T.scout_meal, {"meal": meal, "date": plan_date.isoformat()})
              for meal in meals_for(goal, prefs)]
    return render(prefs, recent, scouts)


def _num(value) -> str:
    return "?" if value is None else f"{value:g}"


def memory_line(prefs: dict) -> str:
    bits = [f"name {prefs['name']}" if prefs.get("name") else "",
            "diet: " + (", ".join(prefs.get("diet") or []) or "none"),
            "allergies: " + (", ".join(prefs.get("allergies") or []) or "none"),
            f"protein goal {prefs['daily_protein_goal_g']} g/day" if prefs.get("daily_protein_goal_g") else
            "no protein goal",
            f"calorie goal {prefs['daily_calorie_goal']} kcal/day" if prefs.get("daily_calorie_goal") else "",
            f"max {prefs['max_calories_per_meal']} kcal per meal" if prefs.get("max_calories_per_meal") else "",
            f"favorite hall {prefs['favorite_hall']}" if prefs.get("favorite_hall") else "",
            "never eat: " + ", ".join(prefs["never_eat"]) if prefs.get("never_eat") else "",
            "dislikes: " + ", ".join(prefs["dislikes"]) if prefs.get("dislikes") else "",
            "loves: " + ", ".join(prefs["favorites"]) if prefs.get("favorites") else "",
            f"treats {prefs.get('treats') or 'sometimes'}",
            "cheat days: " + ", ".join(prefs["cheat_days"]) if prefs.get("cheat_days") else "",
            "meals to plan: " + ", ".join(prefs.get("meals_to_plan") or config.MEALS),
            "notes: " + "; ".join(map(str, prefs["notes"])) if prefs.get("notes") else ""]
    return "; ".join(b for b in bits if b) + "."


def render(prefs: dict, recent: list[dict], scouts: list[dict]) -> str:
    lines = ["BRIEFING (gathered by the app just now; it is current, so don't fetch it again)",
             "Memory: " + memory_line(prefs)]
    if recent:
        days = []
        for plan in recent[-3:]:
            meals = "; ".join(f"{meal}: {', '.join(names)}" for meal, names in plan.get("meals", {}).items())
            days.append(f"{plan['date']} ({meals})")
        lines.append("Recent plans, for variety: " + " | ".join(days))
    else:
        lines.append("Recent plans: none.")
    lines += ["", "Candidates, already filtered for allergies, diet, never-eat and dislikes. "
                  "Columns: id | name | station | serving | protein g | kcal | flags (per serving)."]
    for scout in scouts:
        meal = scout.get("meal", "?").replace("_", " ").upper()
        if scout.get("status") == "error":
            lines.append(f"{meal}: couldn't scout ({scout.get('error')}). Use search_items.")
            continue
        for hall, info in scout["halls"].items():
            head = f"{meal} at {hall}: "
            if info["status"] == "ok":
                lines.append(head + f"{info['fits']} items fit; the best {len(info['items'])}:")
                for row in info["items"]:
                    flags = ",".join(f for f in ("favorite", "treat") if row.get(f))
                    lines.append(f"{row['id']} | {row['name']} | {row['station']} | {row['serving']} | "
                                 f"{_num(row['protein_g'])} | {_num(row['calories'])}" + (f" | {flags}" if flags else ""))
            elif info["status"] == "no_matches":
                lines.append(head + "menu is posted, but nothing on it fits the user's rules.")
            elif info["status"] == "no_menu_posted":
                lines.append(head + "NO MENU POSTED.")
            else:
                lines.append(head + f"COULDN'T LOAD THE MENU ({info.get('error') or info['status']}).")
    return "\n".join(lines)
