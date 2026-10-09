"""Fast mode: the code scouts first, the model decides in about one call, and the loop stops on acceptance."""
import json
from types import SimpleNamespace

import config
from src import agent as A
from src import briefing
from src import tools as T
from tests.conftest import SAMPLE_DATE
from tests.test_hosting import FakeGroq, _tool_call


# --- scouting ---------------------------------------------------------------------------------

def test_scout_meal_respects_memory_and_stays_small(prefs):
    prefs(diet=["vegetarian"], allergies=["peanuts"], never_eat=["mushroom"])
    res = T.scout_meal("dinner", SAMPLE_DATE)
    assert res["status"] == "ok"
    for hall, info in res["halls"].items():
        assert 0 < len(info["items"]) <= T.SCOUT_PER_HALL + 2  # + up to two treats
        assert len({r["name"].lower() for r in info["items"]}) == len(info["items"])  # no duplicates
        for row in info["items"]:
            item = T.find_item(row["id"], T.resolve_date(SAMPLE_DATE))
            assert "vegetarian" in item["tags"]
            assert "peanuts" not in item["allergens"]
            assert "mushroom" not in item["name"].lower()


def test_scout_meal_puts_favorites_first_and_reports_missing_menus(prefs):
    top = T.search_items("lunch", hall="West", date=SAMPLE_DATE, sort_by="calories_low", limit=1)["items"][0]
    prefs(favorites=[top["name"]])
    T.set_simulation(missing=[("East", "lunch")])
    res = T.scout_meal("lunch", SAMPLE_DATE)
    assert res["halls"]["East"]["status"] == "no_menu_posted"
    assert res["halls"]["West"]["items"][0]["favorite"] is True
    assert "East no menu posted" in res["summary"]


def test_briefing_text_has_ids_memory_and_missing_menus(prefs):
    prefs(diet=["vegan"], daily_protein_goal_g=110, never_eat=["olives"])
    T.set_simulation(missing=[("West", "dinner")])
    scouts = [T.scout_meal(m, SAMPLE_DATE) for m in ("breakfast", "dinner")]
    text = briefing.render(T.read_prefs(), [], scouts)
    assert "diet: vegan" in text and "protein goal 110 g/day" in text and "never eat: olives" in text
    assert "DINNER at West: NO MENU POSTED." in text
    first_id = scouts[0]["halls"]["East"]["items"][0]["id"]
    assert f"{first_id} | " in text


def test_briefing_scouts_extra_meals_the_goal_names():
    prefs = {"meals_to_plan": ["breakfast", "lunch", "dinner"]}
    assert briefing.meals_for("late night snack too please", prefs)[-1] == "late_night"
    assert briefing.meals_for("plan my day", prefs) == ["breakfast", "lunch", "dinner"]


# --- the fast Groq loop ----------------------------------------------------------------------------

def _run_fast_groq(script, goal="plan my day", model="openai/gpt-oss-120b"):
    from src.groq_agent import run_groq
    fake = FakeGroq(script)
    run = A.AgentRun(goal=goal, engine="groq")
    events = []
    token = A._current.set({"run": run, "on_event": events.append})
    try:
        run_groq(run, goal, T.resolve_date(SAMPLE_DATE), None, A.Engine("groq", "gsk-test", model), client=fake)
    finally:
        A._current.reset(token)
    return run, fake, events


def _day_plan(**extra):
    meals = []
    for meal in ("breakfast", "lunch", "dinner"):
        pick = T.scout_meal(meal, SAMPLE_DATE)["halls"]["East"]["items"][0]
        meals.append({"meal": meal, "hall": "East", "items": [{"id": pick["id"]}], "reason": "top pick"})
    return {"date": SAMPLE_DATE, "headline": "A good day", "meals": meals,
            "message": "Here's your day, enjoy!", **extra}


def test_fast_groq_plans_in_one_request_and_stops_on_acceptance(prefs):
    prefs(daily_protein_goal_g=100)
    plan = _day_plan()
    run, fake, events = _run_fast_groq([{"tool_calls": [_tool_call(1, "submit_plan", plan)]}])

    assert len(fake.requests) == 1  # one model call for the whole day
    assert run.plan is not None and run.error is None
    first = fake.requests[0]
    assert "BRIEFING" in first["messages"][1]["content"]
    assert first["reasoning_effort"] == "low"
    names = {t["function"]["name"] for t in first["tools"]}
    assert "submit_plan" in names and "search_items" in names
    assert not names & {"get_prefs", "get_menu", "compare_halls", "make_plan"}  # the briefing covers these

    auto = [e["tool"] for e in events if e["type"] == "tool_call" and e.get("auto")]
    assert auto == ["get_prefs", "get_meal_history", "scout_meal", "scout_meal", "scout_meal"]
    model_calls = [e["tool"] for e in events if e["type"] == "tool_call" and not e.get("auto")]
    assert model_calls == ["submit_plan"]

    # the reply is written by code from the checked plan, so its numbers match the totals exactly
    assert run.reply.startswith("Here's your day, enjoy!")
    assert f"{run.plan['totals']['protein_g']} g protein, {run.plan['totals']['calories']} kcal" in run.reply
    assert run.messages[-1]["role"] == "assistant"  # follow-ups continue from a closed turn


def test_fast_groq_first_request_is_small(prefs):
    prefs(diet=["vegetarian"], daily_protein_goal_g=120)
    run, fake, _ = _run_fast_groq([{"tool_calls": [_tool_call(1, "submit_plan", _day_plan())]}])
    sent = json.dumps({"messages": fake.requests[0]["messages"], "tools": fake.requests[0]["tools"]})
    assert len(sent) / 4 < 6_000  # roughly tokens; Groq's free tier allows 8,000 per minute


def test_fast_groq_fixes_a_rejected_plan(prefs):
    prefs()
    bad = _day_plan()
    bad["meals"][0]["items"] = [{"id": "E000000"}]
    run, fake, _ = _run_fast_groq([{"tool_calls": [_tool_call(1, "submit_plan", bad)]},
                                   {"tool_calls": [_tool_call(2, "submit_plan", _day_plan())]}])
    assert len(fake.requests) == 2
    rejected = next(m for m in fake.requests[1]["messages"] if m["role"] == "tool")
    assert "rejected" in rejected["content"] and "E000000" in rejected["content"]
    assert run.plan is not None


def test_plan_adaptations_show_up_as_changes_of_course(prefs):
    prefs()
    plan = _day_plan(adaptations=["West has no dinner menu, so dinner is at East."])
    run, _, events = _run_fast_groq([{"tool_calls": [_tool_call(1, "submit_plan", plan)]}])
    adapt = [e for e in events if e["type"] == "adaptation"]
    assert run.adaptations == 1 and adapt[0]["problem"].startswith("West has no dinner")


def test_reasoning_effort_only_goes_to_gpt_oss_models():
    from src.groq_agent import model_options
    assert model_options("openai/gpt-oss-20b") == {"reasoning_effort": config.GROQ_REASONING_EFFORT}
    assert model_options("llama-3.3-70b-versatile") == {}


def test_plan_reply_reports_the_gap(prefs):
    prefs(daily_protein_goal_g=300)
    result = T.submit_plan(_day_plan(tips=["Add a second Greek yogurt."]))
    reply = A.plan_reply(result["plan"])
    assert "short of your 300 g goal" in reply and "Tip: Add a second Greek yogurt." in reply


# --- the Claude side (options only; no model calls) -------------------------------------------------

def test_claude_fast_options_use_the_light_model_and_stop_hook():
    run = A.AgentRun(goal="x")
    fast = A.build_options(T.resolve_date(SAMPLE_DATE), engine=A.Engine("claude"), run=run)
    assert fast.model == config.MODEL == "claude-haiku-5-5"
    assert fast.effort == config.FAST_EFFORT and fast.max_turns == config.FAST_MAX_TURNS
    assert "mcp__dinewolfie__submit_plan" in fast.allowed_tools
    assert "mcp__dinewolfie__get_menu" not in fast.allowed_tools
    assert "fast mode" in fast.system_prompt
    assert fast.hooks["PostToolUse"][0].matcher == "mcp__dinewolfie__submit_plan"

    thorough = A.build_options(T.resolve_date(SAMPLE_DATE), engine=A.Engine("claude", mode="thorough"), run=run)
    assert thorough.hooks is None and thorough.effort == config.EFFORT
    assert "mcp__dinewolfie__make_plan" in thorough.allowed_tools


def test_stop_hook_only_stops_after_an_accepted_plan():
    import asyncio
    run = A.AgentRun(goal="x")
    hook = A._stop_when_accepted(run)
    assert asyncio.run(hook({}, None, None)) == {}
    run.plan = {"meals": []}
    assert asyncio.run(hook({}, None, None))["continue_"] is False
