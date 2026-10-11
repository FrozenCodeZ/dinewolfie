"""Fetch cache rules, notification text and agent wiring (all offline)."""
import json
from datetime import date, timedelta

import pytest

import config
from src import fetch_menu, notify
from src.fetch_menu import FetchError, Station, fetch_station_week, week_start

ROOTED = Station("West", 2377, "dine-in-vegan-delights", "Rooted")


def _cache(tmp_path, monkeypatch, fetched_at, data):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    path = fetch_menu.station_cache_path(ROOTED, week_start(config.today()))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"fetched_at": fetched_at.isoformat(), "url": "x", "data": data}))


def test_week_starts_on_sunday():
    assert week_start(date(2026, 10, 3)) == date(2026, 9, 27)   # Saturday -> previous Sunday
    assert week_start(date(2026, 10, 4)) == date(2026, 10, 4)   # Sunday -> itself


def test_fresh_cache_is_used_without_network(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, config.now(), {"days": []})
    monkeypatch.setattr(fetch_menu, "_get_json", lambda url: pytest.fail("should not hit the network"))
    _, source = fetch_station_week(ROOTED, config.today())
    assert source == "cache"


def test_stale_cache_is_used_when_network_fails(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, config.now() - timedelta(days=2), {"days": ["old"]})

    def boom(url):
        raise FetchError("network down")
    monkeypatch.setattr(fetch_menu, "_get_json", boom)
    data, source = fetch_station_week(ROOTED, config.today())
    assert source == "stale-cache" and data == {"days": ["old"]}


def test_no_cache_and_no_network_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    with pytest.raises(FetchError):
        fetch_station_week(ROOTED, config.today(), offline=True)


PLAN = {
    "date": "2026-10-01", "headline": "Vegetarian day near West", "protein_gap_g": 12, "protein_goal_g": 120,
    "totals": {"protein_g": 108, "calories": 2100, "carbs_g": 200, "fat_g": 70},
    "meals": [
        {"meal": "breakfast", "hall": "West", "totals": {"protein_g": 41, "calories": 390},
         "items": [{"name": "Greek Yogurt", "servings": 2}, {"name": "Egg Whites"},
                   {"name": "Oatmeal", "favorite": True}],
         "alternatives": [{"hall": "East", "items": [{"name": "Tofu Scramble"}], "note": "x"}]},
        {"meal": "dinner", "hall": "East", "totals": {"protein_g": 67, "calories": 1710},
         "items": [{"name": "Tofu Bowl"}, {"name": "Brownie", "treat": True}]},
    ],
    "adaptations": ["West had no dinner posted, so dinner is at East."], "tips": ["Add a yogurt."],
    "message": "Have a great day!",
}


def test_notification_is_organized_and_complete():
    title, body = notify.plan_message(PLAN)
    assert title == "Thu, Oct 1 · 108 g protein · 2,100 kcal"  # the lock screen shows the day at a glance
    lines = body.splitlines()
    assert lines[:2] == ["Vegetarian day near West", "📍 West → East"]
    assert "🥣 BREAKFAST · West · 41 g · 390 kcal" in lines
    assert "• 2× Greek Yogurt" in lines and "• Oatmeal ♥" in lines and "• Brownie 🍪" in lines
    assert "↪ Or swap at East: Tofu Scramble" in lines
    assert "🎯 Protein 108 / 120 g  (12 g short)" in lines and "🟨" * 9 + "⬜" in lines
    assert "🔄 West had no dinner posted, so dinner is at East." in lines and "💡 Add a yogurt." in lines
    assert lines[-1] == "💬 Have a great day!"
    assert "**" not in body and "#" not in body  # the iPhone app shows Markdown as raw symbols


def test_notification_handles_a_single_meal_without_goal():
    plan = {**PLAN, "protein_goal_g": None, "protein_gap_g": None, "meals": PLAN["meals"][1:], "message": "",
            "headline": "", "adaptations": [], "tips": []}
    _, body = notify.plan_message(plan)
    assert body.splitlines()[0] == "📍 East" and "🎯 Protein 108 g" in body
    assert "🟩" not in body and "🟨" not in body  # no goal, no bar


def test_send_plan_publishes_json_that_opens_the_app(monkeypatch):
    sent = []

    class Ok:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: sent.append((url, kw)) or Ok())
    monkeypatch.setattr(config, "APP_URL", "https://example.test/")
    notify.send_plan({**PLAN, "cheat_day": True}, topic="dw-test")
    url, kw = sent[0]
    body = kw["json"]
    assert url == config.NTFY_SERVER and body["topic"] == "dw-test"
    assert body["title"].startswith("Thu, Oct 1") and body["message"].startswith("Vegetarian day")
    assert body["tags"] == ["wolf", "tada"] and body["priority"] == 3
    assert body["click"] == "https://example.test/"
    assert body["actions"] == [{"action": "view", "label": "Open DineWolfie", "url": "https://example.test/"}]


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
