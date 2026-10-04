"""Fetch cache rules, notification text and agent wiring (all offline)."""
import json
from datetime import date, datetime, timedelta

import pytest

import config
from src import fetch_menu, notify
from src.fetch_menu import FetchError, Station, fetch_station_week, week_start

ROOTED = Station("West", 2377, "dine-in-vegan-delights", "Rooted")


def _cache(tmp_path, monkeypatch, fetched_at, data):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    path = fetch_menu.station_cache_path(ROOTED, week_start(date.today()))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"fetched_at": fetched_at.isoformat(), "url": "x", "data": data}))


def test_week_starts_on_sunday():
    assert week_start(date(2026, 10, 3)) == date(2026, 9, 27)   # Saturday -> previous Sunday
    assert week_start(date(2026, 10, 4)) == date(2026, 10, 4)   # Sunday -> itself


def test_fresh_cache_is_used_without_network(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, datetime.now(), {"days": []})
    monkeypatch.setattr(fetch_menu, "_get_json", lambda url: pytest.fail("should not hit the network"))
    _, source = fetch_station_week(ROOTED, date.today())
    assert source == "cache"


def test_stale_cache_is_used_when_network_fails(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, datetime.now() - timedelta(days=2), {"days": ["old"]})

    def boom(url):
        raise FetchError("network down")
    monkeypatch.setattr(fetch_menu, "_get_json", boom)
    data, source = fetch_station_week(ROOTED, date.today())
    assert source == "stale-cache" and data == {"days": ["old"]}


def test_no_cache_and_no_network_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    with pytest.raises(FetchError):
        fetch_station_week(ROOTED, date.today(), offline=True)


PLAN = {
    "date": "2026-10-01", "headline": "h", "protein_gap_g": 12, "protein_goal_g": 120,
    "totals": {"protein_g": 108, "calories": 2100, "carbs_g": 200, "fat_g": 70},
    "meals": [
        {"meal": "breakfast", "hall": "West",
         "items": [{"name": "Greek Yogurt"}, {"name": "Egg Whites"}, {"name": "Oatmeal"}]},
        {"meal": "dinner", "hall": "East", "items": [{"name": "Tofu Bowl"}]},
    ],
    "adaptations": ["West had no dinner posted, so dinner is at East."], "tips": ["Add a yogurt."],
}


def test_notification_is_short_and_complete():
    title, body = notify.plan_message(PLAN)
    assert "Thu Oct 1" in title
    lines = body.splitlines()
    assert lines[0] == "🥣 Breakfast @ West: Greek Yogurt + Egg Whites +1"
    assert "12 g short" in body and "🔄" in body


def test_notify_without_topic_explains_the_fix(monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    with pytest.raises(RuntimeError, match="NTFY_TOPIC"):
        notify.send("t", "b")


def test_agent_tools_are_wired():
    from src import agent
    names = [s[0] for s in agent.TOOL_SPECS]
    assert names[0] == "make_plan" and "submit_plan" in names and len(set(names)) == len(names)
    server = agent.build_server()
    assert server["name"] == agent.SERVER
    opts = agent.build_options(date(2026, 10, 1))
    assert opts.tools == [] and all(t.startswith("mcp__dinewolfie__") for t in opts.allowed_tools)
    assert "2026-10-01" in opts.system_prompt


def test_default_goal_uses_prefs():
    from src.agent import default_goal
    goal = default_goal({"diet": ["vegan"], "daily_protein_goal_g": 100, "favorite_hall": "East"})
    assert "vegan" in goal and "100 g protein" in goal and "East" in goal
