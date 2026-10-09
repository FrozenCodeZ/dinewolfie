"""The built-in planner (no AI) and the automatic fallback when the AI is rate-limited."""
from types import SimpleNamespace

import pytest

import config
from src import agent as A
from src import tools as T
from tests.conftest import SAMPLE_DATE

DAY = T.resolve_date(SAMPLE_DATE)


def _plan(goal, **engine):
    events = []
    run = A.run_agent(goal, plan_date=DAY, on_event=events.append, engine=A.Engine("builtin", **engine))
    return run, events


def test_builtin_plans_a_checked_day_without_any_ai(prefs):
    prefs(diet=["vegetarian"], daily_protein_goal_g=120)
    run, events = _plan("plan my day")
    assert run.error is None and run.plan is not None
    assert [m["meal"] for m in run.plan["meals"]] == ["breakfast", "lunch", "dinner"]
    for meal in run.plan["meals"]:
        for item in meal["items"]:
            assert "vegetarian" in T.find_item(item["id"], DAY)["tags"]
    assert "built-in planner" in run.reply
    assert run.num_turns == 0  # no model call at all
    assert any(e["type"] == "tool_result" and e["tool"] == "submit_plan" and e["ok"] for e in events)


def test_builtin_reads_hall_meal_and_calorie_wishes_from_the_goal(prefs):
    prefs()
    run, _ = _plan("Just dinner tonight, East only")
    assert [(m["meal"], m["hall"]) for m in run.plan["meals"]] == [("dinner", "East")]
    run, _ = _plan("Cutting: under 1,800 kcal total today with at least 100 g protein.")
    assert run.plan["totals"]["calories"] <= 1800


def test_builtin_says_when_the_favorite_hall_is_missing_a_meal(prefs):
    prefs(favorite_hall="West")
    T.set_simulation(missing=[("West", "dinner")])
    run, events = _plan("plan my day")
    dinner = next(m for m in run.plan["meals"] if m["meal"] == "dinner")
    assert dinner["hall"] == "East"
    assert any(e["type"] == "adaptation" and "West has no dinner" in e["problem"] for e in events)


def test_rate_limited_ai_falls_back_to_the_builtin_planner(prefs, monkeypatch):
    prefs(daily_protein_goal_g=100)

    class AlwaysLimited:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            raise type("RateLimit", (Exception,), {
                "status_code": 429, "response": SimpleNamespace(headers={"retry-after": "50"})})("rate limit")

    import groq
    monkeypatch.setattr(groq, "Groq", AlwaysLimited)
    events = []
    run = A.run_agent("plan my day", plan_date=DAY, on_event=events.append, engine=A.Engine("groq", "gsk-test"))
    assert run.error is None and run.plan is not None
    adapt = [e for e in events if e["type"] == "adaptation"]
    assert adapt and "free-tier limit" in adapt[0]["problem"] and "built-in planner" in adapt[0]["change"]
    assert "built-in planner" in run.reply and run.messages is None


def test_fallback_can_be_switched_off(prefs, monkeypatch):
    prefs()
    monkeypatch.setattr(config, "FALLBACK_TO_BUILTIN", False)
    monkeypatch.setattr(config, "GROQ_FALLBACK_MODELS", [])

    class AlwaysLimited:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            raise type("RateLimit", (Exception,), {
                "status_code": 429, "response": SimpleNamespace(headers={"retry-after": "1"})})("rate limit")

    import groq
    from src import groq_agent
    monkeypatch.setattr(groq, "Groq", AlwaysLimited)
    monkeypatch.setattr(groq_agent.time, "sleep", lambda s: groq_agent._cooldown.clear())
    run = A.run_agent("plan my day", plan_date=DAY, engine=A.Engine("groq", "gsk-test"))
    assert run.plan is None and run.error


@pytest.mark.parametrize("goal,protein,calories", [
    ("aiming for 120 g protein today", 120, None),
    ("under 1,800 kcal with 100g of protein", 100, 1800),
    ("Bulking: as much protein as possible", 180, None),
])
def test_targets_come_from_the_goal_text(goal, protein, calories):
    from src.planner import targets
    assert targets(goal, {}) == (protein, calories)
