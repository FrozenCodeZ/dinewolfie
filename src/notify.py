"""Send the day's plan to your phone with ntfy (https://ntfy.sh).

ntfy is free and needs no account: install the ntfy app on your iPhone,
subscribe to a long random topic name, and put the same name in .env as
NTFY_TOPIC. Sending is one HTTP POST. Anyone who knows the topic name can
read it, so keep it secret (it lives in .env, which Git ignores).

The message is plain text laid out with emoji and bullets, because the iPhone app shows
Markdown as raw symbols. The lock screen shows the title and the first lines, so those
carry the day at a glance: totals in the title, then the headline and which hall when.
Tapping the notification opens DineWolfie.

A push can also be queued: ntfy holds it and delivers it at a set time, up to 3 days ahead
(src/push_queue.py). Each queued day has a fixed id, so sending it again replaces it.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import quote

import requests

import config

MEAL_EMOJI = {"breakfast": "🥣", "brunch": "🥞", "lunch": "🥗", "dinner": "🍲", "late_night": "🌙"}
PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
RULE = "─" * 14
BAR_CELLS = 10


def _num(value) -> str:
    """1770 -> "1,770"; 12.0 -> "12"."""
    if value is None:
        return "?"
    value = round(float(value))
    return f"{value:,}"


def _food(item: dict) -> str:
    servings = float(item.get("servings") or 1)
    amount = "" if servings == 1 else (f"{servings:g}× ")
    marks = (" ♥" if item.get("favorite") else "") + (" 🍪" if item.get("treat") else "")
    return f"{amount}{item['name']}{marks}"


def _route(plan: dict) -> str:
    halls = [m["hall"] for m in plan["meals"]]
    if not halls:
        return ""
    if len(set(halls)) == 1:
        return f"📍 All day at {halls[0]}" if len(halls) > 1 else f"📍 {halls[0]}"
    return "📍 " + " → ".join(halls)


def _bar(value: float, goal: float) -> str:
    """🟩🟩🟩🟩🟩🟩🟩🟩⬜⬜ (yellow while short of the goal): emoji look the same on every phone."""
    filled = max(0, min(BAR_CELLS, round(BAR_CELLS * value / goal))) if goal else 0
    return ("🟩" if value >= goal else "🟨") * filled + "⬜" * (BAR_CELLS - filled)


def _meal_block(meal: dict) -> list[str]:
    t = meal.get("totals") or {}
    label = meal["meal"].replace("_", " ").upper()
    head = f"{MEAL_EMOJI.get(meal['meal'], '🍴')} {label} · {meal['hall']}"
    if t:
        head += f" · {_num(t.get('protein_g'))} g · {_num(t.get('calories'))} kcal"
    lines = [head] + [f"• {_food(item)}" for item in meal["items"]]
    alts = meal.get("alternatives") or []
    if alts:
        alt = alts[0]
        where = f" at {alt['hall']}" if alt.get("hall") and alt["hall"] != meal["hall"] else ""
        lines.append(f"↪ Or swap{where}: " + " + ".join(_food(i) for i in alt["items"]))
    return lines


def plan_message(plan: dict) -> tuple[str, str]:
    """(title, body). The title and the first lines are what shows on the lock screen."""
    day = date.fromisoformat(plan["date"])
    t = plan["totals"]
    title = f"{day.strftime('%a, %b')} {day.day} · {_num(t['protein_g'])} g protein · {_num(t['calories'])} kcal"

    lines = []
    if plan.get("headline"):
        lines.append(plan["headline"].strip())
    route = _route(plan)
    if route:
        lines.append(route + ("  ·  🎉 cheat day" if plan.get("cheat_day") else ""))
    for meal in plan["meals"]:
        lines += [""] + _meal_block(meal)

    lines += ["", RULE]
    goal = plan.get("protein_goal_g")
    if goal:
        done = " ✓" if not plan.get("protein_gap_g") else f"  ({plan['protein_gap_g']} g short)"
        lines.append(f"🎯 Protein {_num(t['protein_g'])} / {_num(goal)} g{done}")
        lines.append(_bar(t["protein_g"], goal))
    else:
        lines.append(f"🎯 Protein {_num(t['protein_g'])} g")
    calorie_goal = plan.get("calorie_goal")
    lines.append(f"🔥 {_num(t['calories'])} kcal" + (f" of {_num(calorie_goal)}" if calorie_goal else ""))
    for a in (plan.get("adaptations") or [])[:2]:
        lines.append(f"🔄 {a}")
    for tip in (plan.get("tips") or [])[:1]:
        lines.append(f"💡 {tip}")
    if plan.get("items_missing_nutrition"):
        lines.append("⚠️ No nutrition listed for " + ", ".join(plan["items_missing_nutrition"][:3]))
    if plan.get("message"):
        lines += ["", f"💬 {plan['message'].strip()}"]
    return title, "\n".join(lines)


def queued_id(day: date | str) -> str:
    """The id of a day's queued push: sending with it again replaces that push instead of adding one."""
    return f"dinewolfie-{day if isinstance(day, str) else day.isoformat()}"


def check_topic(topic: str | None) -> str:
    """ntfy topic names are 1-64 letters, numbers, - or _. Checked before a name goes into a URL."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("Add your ntfy topic first (NTFY_TOPIC in .env, or the topic box on the website). "
                         "An ntfy topic is 1 to 64 letters, numbers, - or _.")
    if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        raise ValueError("An ntfy topic is 1 to 64 letters, numbers, - or _ (no spaces). Check the topic name.")
    return topic


def send(title: str, body: str, tags: str | list[str] = "wolf", priority: str = "default",
         topic: str | None = None, click: str | None = None, open_label: str | None = None,
         at: datetime | None = None, sequence_id: str | None = None) -> dict:
    """Publish one notification (ntfy's JSON API, so emoji in the title arrive intact). With `at`,
    ntfy delivers it then instead of now. Returns ntfy's answer (the message as it stored it)."""
    topic = topic or config.NTFY_TOPIC
    if not topic:
        raise RuntimeError("NTFY_TOPIC isn't set. Add it to your .env file (see README > Phone notifications).")
    tags = [t.strip() for t in tags.split(",")] if isinstance(tags, str) else list(tags)
    payload = {"topic": topic, "title": title, "message": body, "tags": [t for t in tags if t],
               "priority": PRIORITY.get(priority, 3)}
    if click:
        payload["click"] = click  # tapping the notification opens this
        if open_label:
            payload["actions"] = [{"action": "view", "label": open_label, "url": click}]
    if at is not None:
        payload["delay"] = str(int(at.timestamp()))  # a Unix time: the same instant in every time zone
    if sequence_id:
        payload["sequence_id"] = sequence_id
    resp = requests.post(config.NTFY_SERVER, json=payload, timeout=15)
    resp.raise_for_status()
    try:
        return resp.json()
    except (AttributeError, ValueError):
        return {}


def send_plan(plan: dict, topic: str | None = None, at: datetime | None = None, note: str = "") -> dict:
    """Send a plan now, or queue it for `at` (then under the day's fixed id). Returns ntfy's answer."""
    title, body = plan_message(plan)
    if note:
        body += f"\n\n🕒 {note}"
    tags = ["wolf"] + (["tada"] if plan.get("cheat_day") else [])
    return send(title, body, tags=tags, topic=topic, click=config.APP_URL or None,
                open_label=f"Open {config.APP_NAME}", at=at, sequence_id=queued_id(plan["date"]) if at else None)


def cancel_queued(day: date, topic: str | None = None) -> None:
    """Delete a day's queued push before it's delivered (nothing happens if there is none)."""
    topic = check_topic(topic or config.NTFY_TOPIC)
    resp = requests.delete(f"{config.NTFY_SERVER}/{quote(topic)}/{queued_id(day)}", timeout=15)
    resp.raise_for_status()


def send_failure(reason: str, topic: str | None = None) -> None:
    send("No plan today", reason, tags=["wolf", "warning"], priority="low", topic=topic,
         click=config.APP_URL or None, open_label="Try again")


if __name__ == "__main__":
    # python -m src.notify   -> sends a test notification
    send(f"{config.APP_NAME} test", "If you can read this on your phone, notifications work.",
         click=config.APP_URL or None)
    print("Sent. Check your phone.")
