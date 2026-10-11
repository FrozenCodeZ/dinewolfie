"""Queue the next mornings' plans on ntfy, so they reach your phone while the site is closed.

The hosted site can't wake itself up on a schedule, but ntfy (the phone push service) can hold a
message and deliver it later, up to 3 days ahead. So one visit lines up to three mornings:
DineWolfie plans each day now, from the menus posted now and your saved memory, and ntfy delivers
each plan at the time you picked. Each day's push has a fixed id (notify.queued_id), so queuing
again replaces the queued pushes instead of doubling them, and cancel_days removes them.

Times are compared as real elapsed time, so the 3-day limit still holds across a clock change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import config
from src import notify
from src import tools as T

DAYS = 3
LATEST = timedelta(days=3) - timedelta(minutes=15)  # ntfy's limit is 3 days; leave time for planning
EARLIEST = timedelta(minutes=15)                    # anything sooner: "Send to my phone" does it now


@dataclass
class Queued:
    day: date
    at: datetime              # when ntfy delivers it (Stony Brook time)
    title: str = ""           # the notification's title, when it was queued
    skipped: str = ""         # why it wasn't
    replaceable: bool = True  # ntfy confirmed the day's id, so queuing again replaces this push


def local_now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def time_label(moment: time | datetime) -> str:
    """"7:30 AM" (written out by hand: %-I doesn't work on Windows)."""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def clock(moment: datetime) -> str:
    """"Wed 3:05 PM"."""
    return f"{moment.strftime('%a')} {time_label(moment)}"


def delivery_times(at: time, now: datetime | None = None, days: int = DAYS) -> list[datetime]:
    """The next `days` deliveries at `at` (Stony Brook time) that ntfy can still schedule from now."""
    now = now or local_now()
    zone = ZoneInfo(config.TIMEZONE)
    out = []
    for ahead in range(days + 1):
        when = datetime.combine(now.date() + timedelta(days=ahead), at, tzinfo=zone)
        wait = when.timestamp() - now.timestamp()  # real seconds, even across a clock change
        if EARLIEST.total_seconds() <= wait <= LATEST.total_seconds():
            out.append(when)
    return out[:days]


def menus_posted(day: date) -> bool:
    """Whether any place has a menu that day (Nutrislice posts each week's menus a few days ahead)."""
    for meal in T.read_prefs().get("meals_to_plan") or config.MEALS:
        if any(h.get("status") == "ok" for h in T.scout_meal(meal, day.isoformat())["halls"].values()):
            return True
    return False


def queue_days(at: time, topic: str | None, engine, goal: str | None = None, on_day=None,
               now: datetime | None = None) -> list[Queued]:
    """Plan each of the next mornings and queue it on ntfy for `at`. One bad day doesn't stop the rest."""
    from src.agent import default_goal, run_agent  # agent imports a lot; only needed here
    topic = notify.check_topic(topic or config.NTFY_TOPIC)
    now = now or local_now()
    goal = goal or default_goal(T.read_prefs())
    note = f"Planned {clock(now)} from the menus posted then."
    out = []
    for when in delivery_times(at, now):
        day = when.date()
        if on_day:
            on_day(day)
        if not menus_posted(day):
            out.append(Queued(day, when, skipped="the menus aren't posted yet"))
            continue
        run = run_agent(goal, plan_date=day, engine=engine)
        if not run.plan:
            out.append(Queued(day, when, skipped=run.error or "no plan fit that day's menus"))
            continue
        try:
            stored = notify.send_plan(run.plan, topic=topic, at=when, note=note)
        except Exception as exc:  # ntfy down, or a bad topic: say so for this day and try the next
            out.append(Queued(day, when, skipped=f"ntfy didn't take it ({exc})"))
            continue
        out.append(Queued(day, when, title=notify.plan_message(run.plan)[0],
                          replaceable=stored.get("sequence_id") == notify.queued_id(day)))
    return out


def cancel_days(topic: str | None, now: datetime | None = None, days: int = DAYS) -> None:
    """Delete any queued push from today through the last day a queue could reach."""
    topic = notify.check_topic(topic or config.NTFY_TOPIC)
    today = (now or local_now()).date()
    for ahead in range(days + 1):
        notify.cancel_queued(today + timedelta(days=ahead), topic)
