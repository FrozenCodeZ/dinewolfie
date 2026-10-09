"""The built-in planner: a full-day plan with no AI call at all.

Used two ways:
  * as an engine you can pick ("Built-in" in the sidebar, DINEWOLFIE_ENGINE=builtin), and
  * automatically when the AI service is rate-limited (Groq's free tier), so a plan never stalls.

It is deliberately simple and says so: for each meal it looks at the same candidates the AI's
briefing uses (already filtered for allergies, diet, never-eat and dislikes), picks the most protein
per calorie that fits the goal, prefers favorites and variety, and sends the result through the same
checker as the AI. It doesn't understand free-text wishes the way the AI does ("something warm").
"""
from __future__ import annotations

import re
from datetime import date

from src import agent as A
from src import briefing, locations
from src import tools as T

MAX_ITEMS = 4


def _number(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    return float(match.group(1).replace(",", "")) if match else None


def targets(goal: str, prefs: dict) -> tuple[float | None, float | None]:
    """(daily protein g, daily calories) from the goal text, else from memory."""
    low = goal.lower()
    protein = _number(r"(\d{2,3})\s*g(?:rams?)?\s+(?:of\s+)?protein", low) or prefs.get("daily_protein_goal_g")
    calories = _number(r"(\d[\d,]{2,5})\s*(?:kcal|cal\b|calories)", low) or prefs.get("daily_calorie_goal")
    if protein is None and re.search(r"\bbulk|as much protein|max(?:imum)? protein", low):
        protein = 180
    return protein, calories


def restrict_places(goal: str, places: list[str]) -> list[str]:
    """'East only' / 'only West' keeps just that hall."""
    low = goal.lower()
    for hall in ("East", "West"):
        h = hall.lower()
        if re.search(rf"\b{h}\s+only\b|\bonly\s+(?:at\s+)?{h}\b|\bjust\s+{h}\b", low):
            return [hall]
    return places


def meals_for(goal: str, prefs: dict) -> list[str]:
    """Usually every meal to plan; 'just dinner' / 'only lunch' plans just those."""
    low = goal.lower()
    if re.search(r"\b(just|only)\b", low):
        named = [m for word, m in briefing.MEAL_WORDS.items() if word in low]
        if named:
            return list(dict.fromkeys(named))
    return briefing.meals_for(goal, prefs)


def value(row: dict) -> float:
    """Protein that's worth the calories: protein x sqrt(protein per calorie). A pure ratio would
    pick lettuce (1 g, 10 kcal); pure protein would pick the biggest plate whatever it costs."""
    protein = row.get("protein_g") or 0
    return protein * (protein / max(row.get("calories") or 1, 1)) ** 0.5


def _pick(rows: list[dict], protein_target: float | None, kcal_cap: float | None, recent: set[str],
          allow_treat: bool) -> list[dict]:
    """Greedy: favorites first, then the best protein for the calories, avoiding recent repeats."""
    pool = [r for r in rows if r.get("calories") is not None and (allow_treat or not r.get("treat"))
            and ((r.get("protein_g") or 0) >= 3 or r.get("favorite") or r.get("treat"))]
    pool.sort(key=lambda r: (not r.get("favorite"), r["name"].lower() in recent, -value(r)))
    chosen, protein, kcal = [], 0.0, 0.0
    limit = MAX_ITEMS if protein_target else 3
    for row in pool:
        if len(chosen) >= limit:
            break
        if kcal_cap and kcal + row["calories"] > kcal_cap and chosen:
            continue
        chosen.append({"id": row["id"], "servings": 1, "_row": row})
        protein += row["protein_g"] or 0
        kcal += row["calories"]
        if len(chosen) >= 2 and protein_target and protein >= protein_target:
            break
    # Still short: a second serving of the best item, if the calories allow it.
    if chosen and protein_target and protein < protein_target:
        best = max(chosen, key=lambda c: c["_row"]["protein_g"] or 0)
        if not kcal_cap or kcal + best["_row"]["calories"] <= kcal_cap:
            best["servings"] = 2
    return chosen


def _score(picks: list[dict], protein_target: float | None, place: str, prefs: dict) -> float:
    protein = sum((c["_row"]["protein_g"] or 0) * c["servings"] for c in picks)
    score = min(protein, protein_target or protein) * 2 + sum(3 for c in picks if c["_row"].get("favorite"))
    if prefs.get("favorite_hall") == place:
        score += 5
    return score


def build_plan(goal: str, plan_date: date, scouts: list[dict], prefs: dict, recent_names: set[str]) -> dict:
    protein_goal, calorie_goal = targets(goal, prefs)
    n = max(1, len(scouts))
    per_meal_protein = protein_goal / n if protein_goal else None
    per_meal_kcal = calorie_goal / n if calorie_goal else None
    cheat = T.is_cheat_day(plan_date, prefs)
    treats = prefs.get("treats") or "sometimes"
    meals, adaptations, best_items, estimate = [], [], [], 0.0
    for i, scout in enumerate(scouts):
        meal = scout["meal"]
        allow_treat = treats == "often" or (treats == "sometimes" and cheat) or (
            treats == "sometimes" and i == len(scouts) - 1 and not calorie_goal)
        options = []
        for place, info in scout["halls"].items():
            if info["status"] != "ok":
                continue
            picks = _pick(info["items"], per_meal_protein, None if cheat else per_meal_kcal, recent_names,
                          allow_treat)
            if picks:
                options.append((_score(picks, per_meal_protein, place, prefs), place, picks))
        missing = [p for p, info in scout["halls"].items() if info["status"] in ("no_menu_posted", "error")]
        if not options:
            adaptations.append(f"No {meal.replace('_', ' ')} menu fits anywhere today, so it's left out.")
            continue
        options.sort(key=lambda o: -o[0])
        _, place, picks = options[0]
        wanted = prefs.get("favorite_hall")
        if wanted in missing and place != wanted:
            adaptations.append(f"{wanted} has no {meal.replace('_', ' ')} menu posted, so {meal.replace('_', ' ')} "
                               f"is at {place}.")
        recent_names = recent_names | {c["_row"]["name"].lower() for c in picks}  # variety within the day too
        best_items.append((meal, max(picks, key=lambda c: c["_row"]["protein_g"] or 0)["_row"]["name"]))
        estimate += sum((c["_row"]["protein_g"] or 0) * c["servings"] for c in picks)
        entry = {"meal": meal, "hall": place,
                 "items": [{"id": c["id"], "servings": c["servings"],
                            **({"treat": True} if c["_row"].get("treat") else {})} for c in picks],
                 "reason": f"The most protein per calorie at {place} that fits your rules"
                           + (", with a favorite" if any(c["_row"].get("favorite") for c in picks) else "") + "."}
        if len(options) > 1:
            _, alt_place, alt_picks = options[1]
            entry["alternatives"] = [{"hall": alt_place, "note": f"If you'd rather eat at {alt_place}",
                                      "items": [{"id": c["id"], "servings": c["servings"]} for c in alt_picks]}]
        meals.append(entry)
    halls_used = ", ".join(f"{m['meal'].replace('_', ' ')} at {m['hall']}" for m in meals)
    tips = []
    if best_items and protein_goal and estimate < protein_goal:
        meal, name = best_items[0]
        tips.append(f"To close the protein gap, add another {name} at {meal.replace('_', ' ')}.")
    return {"date": plan_date.isoformat(), "goal": goal[:80], "headline": f"Built-in plan: {halls_used}.",
            "meals": meals, "adaptations": adaptations, "tips": tips}


def run_builtin(run, goal: str, plan_date: date, because: str | None = None) -> None:
    """Fill in `run` with a checked plan made without AI. `because`: why the AI wasn't used."""
    prefs = briefing.run_lookup("get_prefs", T.get_prefs, {}).get("prefs") or T.read_prefs()
    recent = briefing.run_lookup("get_meal_history", T.get_meal_history, {"days": 3}).get("recent_plans", [])
    recent_names = {name.lower() for p in recent for names in p.get("meals", {}).values() for name in names}
    known = locations.all_locations()
    places = restrict_places(goal, T.considered(prefs, [loc.key for loc in locations.named_in(goal, known)]))
    scouts = [briefing.run_lookup("scout_meal", T.scout_meal, {"meal": meal, "date": plan_date.isoformat(),
                                                          "places": places})
              for meal in meals_for(goal, prefs)]
    plan = build_plan(goal, plan_date, [s for s in scouts if "halls" in s], prefs, recent_names)
    plan["message"] = (f"{because} So DineWolfie's built-in planner made this plan from the same menus, and "
                       "it passed the same checks. Press Plan my day again in a minute for the AI's version."
                       if because else
                       "Made by DineWolfie's built-in planner (no AI): the most protein per calorie that fits "
                       "your rules, checked against the real menu.")
    result = briefing.run_lookup("submit_plan", T.submit_plan, {"plan": plan})
    if result.get("status") == "accepted":
        run.reply = A.plan_reply(run.plan or result["plan"])
    else:
        run.error = "The built-in planner couldn't make a plan that passes the checks: " + "; ".join(
            result.get("problems", [result.get("error", "unknown problem")])[:3])
