"""Send the day's plan to your phone with ntfy (https://ntfy.sh).

ntfy is free and needs no account: install the ntfy app on your iPhone,
subscribe to a long random topic name, and put the same name in .env as
NTFY_TOPIC. Sending is one HTTP POST. Anyone who knows the topic name can
read it, so keep it secret (it lives in .env, which Git ignores).
"""
from __future__ import annotations

from datetime import date

import requests

import config

MEAL_EMOJI = {"breakfast": "🥣", "brunch": "🥞", "lunch": "🥗", "dinner": "🍲", "late_night": "🌙"}


def _item_names(meal: dict, limit: int = 2) -> str:
    names = [i["name"] for i in meal["items"]]
    text = " + ".join(names[:limit])
    return text + (f" +{len(names) - limit}" if len(names) > limit else "")


def plan_message(plan: dict) -> tuple[str, str]:
    """(title, body). The first lines are what shows on the lock screen."""
    day = date.fromisoformat(plan["date"])
    title = f"🍽️ {config.APP_NAME} · {day.strftime('%a %b')} {day.day}"
    t = plan["totals"]
    lines = [f"{MEAL_EMOJI.get(m['meal'], '🍴')} {m['meal'].replace('_', ' ').title()} @ {m['hall']}: "
             f"{_item_names(m)}" for m in plan["meals"]]
    total = f"≈{t['protein_g']} g protein · {t['calories']} kcal"
    if plan.get("protein_gap_g"):
        total += f" ({plan['protein_gap_g']} g short of goal)"
    lines.append(total)
    for a in plan.get("adaptations", [])[:2]:
        lines.append(f"🔄 {a}")
    for tip in plan.get("tips", [])[:1]:
        lines.append(f"💡 {tip}")
    return title, "\n".join(lines)


def send(title: str, body: str, tags: str = "fork_and_knife", priority: str = "default",
         topic: str | None = None) -> None:
    topic = topic or config.NTFY_TOPIC
    if not topic:
        raise RuntimeError("NTFY_TOPIC isn't set. Add it to your .env file (see README > Phone notifications).")
    resp = requests.post(
        f"{config.NTFY_SERVER}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Tags": tags, "Priority": priority},
        timeout=15,
    )
    resp.raise_for_status()


def send_plan(plan: dict, topic: str | None = None) -> str:
    title, body = plan_message(plan)
    send(title, body, topic=topic)
    return body


def send_failure(reason: str, topic: str | None = None) -> None:
    send(f"🍽️ {config.APP_NAME}: no plan today", reason, tags="warning", priority="low", topic=topic)


if __name__ == "__main__":
    # python -m src.notify   -> sends a test notification
    send(f"{config.APP_NAME} test", "If you can read this on your phone, notifications work. 🐺")
    print("Sent. Check your phone.")
