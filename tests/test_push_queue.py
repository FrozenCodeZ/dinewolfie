"""Queue the next mornings' plans on ntfy: the delivery times, what is sent, and replacing/cancelling."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from src import notify, push_queue
from src import tools as T
from src.agent import Engine

NY = ZoneInfo("America/New_York")
BUILTIN = Engine("builtin")


def _labels(times):
    return [t.strftime("%a %b %d %H:%M %Z") for t in times]


def test_the_next_three_mornings_from_a_sunday_afternoon():
    now = datetime(2026, 10, 11, 15, 0, tzinfo=NY)
    assert _labels(push_queue.delivery_times(time(7, 30), now)) == [
        "Mon Oct 12 07:30 EDT", "Tue Oct 13 07:30 EDT", "Wed Oct 14 07:30 EDT"]


def test_a_time_too_soon_or_past_ntfys_three_day_limit_is_left_out():
    now = datetime(2026, 10, 11, 7, 20, tzinfo=NY)  # 7:30 today is 10 minutes away; Wednesday is 3 days + 10 min
    assert _labels(push_queue.delivery_times(time(7, 30), now)) == ["Mon Oct 12 07:30 EDT", "Tue Oct 13 07:30 EDT"]


def test_the_three_day_limit_is_real_hours_across_the_clock_change():
    """Clocks go back on Nov 1: Monday 7:30 is 72.5 real hours after Friday 8:00, past ntfy's limit."""
    now = datetime(2026, 10, 30, 8, 0, tzinfo=NY)
    times = push_queue.delivery_times(time(7, 30), now)
    assert _labels(times) == ["Sat Oct 31 07:30 EDT", "Sun Nov 01 07:30 EST"]
    assert times[1].astimezone(timezone.utc).hour == 12  # 7:30 EST is 12:30 UTC


@pytest.fixture
def ntfy(monkeypatch):
    """Captures publishes and deletes; answers like ntfy (echoing the sequence id)."""
    sent = {"post": [], "delete": []}

    class Reply:
        def __init__(self, data=None):
            self._data = data or {}

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    def post(url, json=None, **kw):
        sent["post"].append(json)
        return Reply({"id": "m1", "sequence_id": json.get("sequence_id")})

    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setattr(notify.requests, "delete", lambda url, **kw: sent["delete"].append(url) or Reply())
    return sent


def test_queueing_plans_each_day_and_asks_ntfy_to_deliver_it_then(prefs, ntfy):
    prefs(diet=["vegetarian"], daily_protein_goal_g=110)
    now = datetime(2026, 9, 30, 15, 5, tzinfo=NY)
    queued = push_queue.queue_days(time(7, 30), "dw-test", BUILTIN, now=now)
    assert [q.day for q in queued] == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]
    assert all(q.title and not q.skipped for q in queued)
    first = ntfy["post"][0]
    assert first["topic"] == "dw-test" and first["sequence_id"] == "dinewolfie-2026-10-01"
    assert first["delay"] == str(int(datetime(2026, 10, 1, 7, 30, tzinfo=NY).timestamp()))
    assert first["title"].startswith("Thu, Oct 1")
    assert first["message"].endswith("🕒 Planned Wed 3:05 PM from the menus posted then.")
    assert [p["sequence_id"] for p in ntfy["post"]] == [f"dinewolfie-2026-10-0{d}" for d in (1, 2, 3)]
    assert all(q.replaceable for q in queued)


def test_a_day_without_menus_is_skipped_and_named(prefs, ntfy, monkeypatch):
    prefs()
    monkeypatch.setattr(push_queue, "menus_posted", lambda day: day != date(2026, 10, 2))
    queued = push_queue.queue_days(time(7, 30), "dw-test", BUILTIN, now=datetime(2026, 9, 30, 15, 0, tzinfo=NY))
    assert [q.skipped for q in queued] == ["", "the menus aren't posted yet", ""]
    assert len(ntfy["post"]) == 2


def test_an_ntfy_error_skips_that_day_instead_of_stopping(prefs, monkeypatch):
    prefs()

    def down(url, **kw):
        raise notify.requests.ConnectionError("ntfy is down")
    monkeypatch.setattr(notify.requests, "post", down)
    queued = push_queue.queue_days(time(7, 30), "dw-test", BUILTIN, now=datetime(2026, 9, 30, 15, 0, tzinfo=NY))
    assert len(queued) == 3 and all("ntfy is down" in q.skipped for q in queued)


def test_a_server_that_doesnt_echo_the_id_is_flagged(prefs, monkeypatch):
    prefs()

    class Old:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "m1"}
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: Old())
    queued = push_queue.queue_days(time(7, 30), "dw-test", BUILTIN, now=datetime(2026, 9, 30, 15, 0, tzinfo=NY))
    assert queued and not any(q.replaceable for q in queued)


def test_cancel_deletes_each_upcoming_days_queued_push(ntfy):
    push_queue.cancel_days("dw-test", now=datetime(2026, 10, 11, 15, 0, tzinfo=NY))
    assert ntfy["delete"] == [f"{config.NTFY_SERVER}/dw-test/dinewolfie-2026-10-{d}" for d in (11, 12, 13, 14)]


@pytest.mark.parametrize("topic", ["", "has space", "../admin", "x" * 65])
def test_odd_topic_names_are_refused_before_anything_is_sent(topic, ntfy):
    with pytest.raises(ValueError, match="letters, numbers"):
        push_queue.cancel_days(topic)
    with pytest.raises(ValueError, match="letters, numbers"):
        push_queue.queue_days(time(7, 30), topic, BUILTIN)
    assert not ntfy["post"] and not ntfy["delete"]


def test_history_keeps_one_plan_per_date(prefs):
    prefs()
    T.append_history({"date": "2026-10-12", "headline": "first"})
    T.append_history({"date": "2026-10-13", "headline": "other day"})
    T.append_history({"date": "2026-10-12", "headline": "queued again"})
    assert [(h["date"], h["plan"]["headline"]) for h in T.read_history()] == [
        ("2026-10-13", "other day"), ("2026-10-12", "queued again")]


def test_the_site_queues_the_next_mornings_from_the_plan_tab(prefs, ntfy, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("DINEWOLFIE_PREFS", str(config.PREFS_FILE))
    prefs(diet=["vegetarian"])
    monkeypatch.setattr(config, "NTFY_TOPIC", "dw-test")
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=90)
    at.run()
    at.sidebar.radio(key="provider").set_value("Built-in").run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Queue the next 3 days").click().run()
    assert not at.exception, at.exception
    assert any(s.value.startswith("Queued for ") for s in at.success), [s.value for s in at.success]
    assert len(ntfy["post"]) >= 2 and all(p["topic"] == "dw-test" and p["delay"] for p in ntfy["post"])
    next(b for b in at.button if b.label == "Cancel queued plans").click().run()
    assert not at.exception and len(ntfy["delete"]) == 4
