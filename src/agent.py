"""The DineWolfie agent: Claude + our tools, running the agent loop.

    Goal -> decide -> use tool(s) -> observe -> (adapt) -> finish

The Claude Agent SDK runs the loop. We give it:
  * a system prompt (how to behave),
  * our tools from src/tools.py, wrapped as an in-process MCP server,
  * a callback so every step can be shown live in the terminal or the web UI.

run_agent(goal) returns an AgentRun with the final plan, the reply text and
the full trace of steps.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ThinkingBlock,
    ToolAnnotations, ToolUseBlock, create_sdk_mcp_server, query, tool,
)

import config
from src import tools as T

SERVER = "dinewolfie"

# --- Tool definitions ---------------------------------------------------------------
# (name, description, JSON schema, python function, read-only?)
_DATE = {"type": "string", "description": "YYYY-MM-DD, 'today' or 'tomorrow'. Defaults to today."}
_MEAL = {"type": "string", "enum": ["breakfast", "brunch", "lunch", "dinner", "late_night"]}
_HALL = {"type": "string", "enum": ["East", "West"]}
_STRS = {"type": "array", "items": {"type": "string"}}
_ITEMS = {"type": "array", "items": {
    "type": "object",
    "properties": {"id": {"type": "string"}, "servings": {"type": "number", "minimum": 0.25, "maximum": 4}},
    "required": ["id"]}}
_PLAN_ITEMS = {"type": "array", "items": {
    "type": "object",
    "properties": {"id": {"type": "string"}, "servings": {"type": "number", "minimum": 0.25, "maximum": 4},
                   "treat": {"type": "boolean", "description": "true if this is the day's treat"}},
    "required": ["id"]}}
_ALTERNATIVES = {"type": "array", "description": "1-2 swaps for this meal with similar protein.", "items": {
    "type": "object",
    "properties": {"hall": _HALL, "items": _PLAN_ITEMS,
                   "note": {"type": "string", "description": "When to pick it, e.g. 'if the Grill line is long'"}},
    "required": ["items", "note"]}}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


TOOL_SPECS = [
    ("make_plan",
     "Record your step-by-step plan before you start using other tools. Call it again with the "
     "updated steps if you change course. Shown to the user as a checklist.",
     _obj({"steps": {**_STRS, "description": "Short imperative steps, in order."}}, ["steps"]),
     T.make_plan, True),
    ("get_prefs",
     "Read the user's saved preferences (memory): diet, allergies, protein/calorie goals, favorite hall, "
     "dislikes, which meals to plan, notes. Always call this first.",
     _obj({}), T.get_prefs, True),
    ("update_prefs",
     "Save a LASTING preference the user clearly stated (e.g. 'I'm vegan now', 'remember I'm allergic to "
     "peanuts', 'my protein goal is 140 g'). Not for one-off wishes about today.",
     _obj({"changes": {"type": "object", "description": "Keys from get_prefs with their new values."},
           "reason": {"type": "string"}}, ["changes"]),
     T.update_prefs, False),
    ("get_meal_history",
     "Recent plans DineWolfie made for this user, so you can add variety instead of repeating meals.",
     _obj({"days": {"type": "integer", "minimum": 1, "maximum": 14}}), T.get_meal_history, True),
    ("get_menu",
     "Every item one hall serves at one meal on a date, grouped by station, with nutrition per serving. "
     "status is 'ok', 'no_menu_posted' or 'error' - always check it.",
     _obj({"hall": _HALL, "meal": _MEAL, "date": _DATE}, ["hall", "meal"]), T.get_menu, True),
    ("search_items",
     "Find menu items that fit constraints, across one or both halls. Automatically applies the user's saved "
     "diet, allergies and dislikes (apply_my_prefs=true). Reports how many items each filter removed. "
     "sort_by: protein | protein_per_calorie | calories_low | calories_high.",
     _obj({"meal": _MEAL, "hall": {"type": "string", "enum": ["East", "West", "any"]}, "date": _DATE,
           "min_protein_g": {"type": "number"}, "max_calories": {"type": "number"},
           "exclude_allergens": _STRS, "require_tags": {**_STRS, "description": "e.g. vegan, vegetarian, halal"},
           "exclude_keywords": _STRS, "station": {"type": "string"},
           "sort_by": {"type": "string", "enum": list(T.SORTS)},
           "limit": {"type": "integer", "minimum": 1, "maximum": 40},
           "apply_my_prefs": {"type": "boolean"}}, ["meal"]),
     T.search_items, True),
    ("compare_halls",
     "Side-by-side: for one meal, how many items fit the user's constraints at East vs West and the best "
     "protein options at each. Use it to decide which hall to send the user to.",
     _obj({"meal": _MEAL, "date": _DATE, "min_protein_g": {"type": "number"},
           "max_calories": {"type": "number"}}, ["meal"]),
     T.compare_halls, True),
    ("get_item_details",
     "Full details (description, ingredients, all nutrients, allergens) for specific item ids.",
     _obj({"ids": _STRS, "date": _DATE}, ["ids"]), T.get_item_details, True),
    ("sum_nutrition",
     "Add up calories/protein/carbs/fat for chosen items (id + servings). Use this for ALL arithmetic.",
     _obj({"items": _ITEMS, "date": _DATE}, ["items"]), T.sum_nutrition, True),
    ("log_adaptation",
     "Call this the moment you change course because something failed or didn't fit (menu missing, "
     "fetch error, nothing matches, goal unreachable). The user sees it highlighted.",
     _obj({"problem": {"type": "string"}, "change": {"type": "string"}, "reason": {"type": "string"}},
          ["problem", "change"]),
     T.log_adaptation, True),
    ("submit_plan",
     "Submit the final day plan. A checker verifies every item id (main picks and swaps) against the real "
     "menu for that hall/meal and the user's allergies, diet and never-eat list, then computes totals. If "
     "status is 'rejected', fix the problems and submit again.",
     _obj({"date": {"type": "string"},
           "goal": {"type": "string", "description": "The user's goal in a few words."},
           "headline": {"type": "string", "description": "One short line summarizing the day."},
           "meals": {"type": "array", "items": _obj({
               "meal": _MEAL, "hall": _HALL, "items": _PLAN_ITEMS,
               "reason": {"type": "string", "description": "One line: why this hall and these items."},
               "alternatives": _ALTERNATIVES},
               ["meal", "hall", "items", "reason"])},
           "adaptations": {**_STRS, "description": "Each change you made because something failed/didn't fit."},
           "tips": {**_STRS, "description": "Optional practical tips, e.g. how to close a protein gap."}},
          ["date", "meals", "headline"]),
     T.submit_plan, False),
]
TOOL_NAMES = [spec[0] for spec in TOOL_SPECS]


# --- Run state + events -------------------------------------------------------------------

@dataclass
class AgentRun:
    goal: str
    plan: dict | None = None
    reply: str = ""
    events: list[dict] = field(default_factory=list)
    session_id: str | None = None
    error: str | None = None
    num_turns: int = 0
    duration_s: float = 0.0
    cost_usd: float | None = None
    tool_calls: int = 0
    adaptations: int = 0


_current: dict[str, Any] = {"run": None, "on_event": None}


def _emit(event: dict) -> None:
    event.setdefault("t", round(time.monotonic(), 2))
    run: AgentRun | None = _current["run"]
    if run is not None:
        run.events.append(event)
    callback = _current["on_event"]
    if callback is not None:
        try:
            callback(event)
        except Exception:  # the UI must never crash the agent
            pass


def _wrap(name: str, description: str, schema: dict, fn: Callable, read_only: bool):
    """Turn a plain function into an SDK tool that also reports to the trace."""

    @tool(name, description, schema,
          annotations=ToolAnnotations(readOnlyHint=read_only, maxResultSizeChars=200_000))
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        _emit({"type": "tool_call", "tool": name, "input": args})
        run: AgentRun | None = _current["run"]
        if run is not None:
            run.tool_calls += 1
        try:
            result = await asyncio.to_thread(fn, **args)
        except Exception as exc:  # report the failure to Claude so it can adapt
            message = f"{type(exc).__name__}: {exc}"
            _emit({"type": "tool_result", "tool": name, "ok": False, "summary": message})
            return {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": message})}],
                    "is_error": True}
        status = result.get("status", "ok")
        ok = status in ("ok", "accepted")
        _emit({"type": "tool_result", "tool": name, "ok": ok, "status": status,
               "summary": result.get("summary", ""), "data": _trace_data(name, result)})
        if name == "make_plan":
            _emit({"type": "plan", "steps": result.get("steps", [])})
        if name == "log_adaptation":
            if run is not None:
                run.adaptations += 1
            _emit({"type": "adaptation", "problem": args.get("problem", ""), "change": args.get("change", ""),
                   "reason": args.get("reason", "")})
        if name == "submit_plan" and status == "accepted" and run is not None:
            run.plan = result["plan"]
            _emit({"type": "final_plan", "plan": result["plan"]})
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    return handler


def _trace_data(name: str, result: dict) -> dict:
    """A small slice of each result for the UI (not the whole menu)."""
    if name in ("search_items",):
        return {"items": result.get("items", [])[:6], "excluded": result.get("excluded_counts", {})}
    if name == "compare_halls":
        return {"halls": result.get("halls", {})}
    if name == "sum_nutrition":
        return {"totals": result.get("totals", {})}
    if name == "submit_plan" and result.get("status") == "rejected":
        return {"problems": result.get("problems", [])}
    if name == "get_menu" and result.get("status") == "ok":
        return {"stations": {s: len(v) for s, v in result.get("stations", {}).items()}}
    if name == "get_prefs":
        return {"prefs": result.get("prefs", {})}
    return {}


def build_server():
    return create_sdk_mcp_server(name=SERVER, version="1.0.0",
                                 tools=[_wrap(*spec) for spec in TOOL_SPECS])


# --- Prompt ---------------------------------------------------------------------------------

SYSTEM_PROMPT = """You are DineWolfie, a meal-planning agent for Stony Brook University's two all-you-care-to-eat \
dining halls: East Side Dine-In ("East") and West Side Dine-In ("West"). A student tells you a goal; you plan \
their day of eating from the real menus.

Today is {weekday}, {today}; local time {now}. The plan date is {plan_date} ({plan_weekday}) unless the user \
says otherwise.

How you work: goal -> decide -> use tools -> observe -> adapt -> finish.
1. Call get_prefs first (the user's saved memory). Call get_meal_history so you can avoid repeating recent meals.
2. Call make_plan with a short list of the steps you will take. If you change course, call make_plan again.
3. Gather facts with tools. Check both halls unless the user fixed a hall for a meal. compare_halls and \
search_items find good candidates quickly; get_menu shows everything at one hall. Don't fetch the same thing twice.
4. Look at every result's status. When something goes wrong - no menu posted, a fetch error, filters leave \
nothing, or the goal can't be met - adapt: try the other hall, another station, a different meal slot, or relax \
the least important constraint. Never relax an allergy or the user's diet. Each time you change course, call \
log_adaptation right then with the problem, the change, and why.
5. Use sum_nutrition for every number you report. Never do arithmetic yourself.
6. Finish by calling submit_plan with the meals in the user's meals_to_plan (or the meals they asked for). It \
checks every item against the real menu and the user's allergies and diet. If it is rejected, fix the problems \
and submit again. Put every change you made into "adaptations".
7. After submit_plan is accepted, reply with a short, friendly summary that reads well on a phone (under 120 \
words): each meal with its hall and items, the totals, and any gap or caveat.

Be a good friend about food, not a calculator:
- {name_line}
- Give each meal 1 swap in "alternatives" (similar protein, can be at the other hall) with a short note on \
when to pick it ("if the Grill line is long", "if you want something warm").
- If one of the user's favorites is on today's menu (search results mark it "favorite": true), use it and say so.
- Treats setting: {treats}. never = no treats. sometimes = one small treat on days the goal still fits. \
often = a treat most days. Mark it with "treat": true. Skip treats when the user is cutting or asks for light.
- {cheat_line}
- Talk like a friend who knows the dining halls: warm, short, a little playful. No lectures, no food guilt.

Rules:
- Never invent menu items, ids or nutrition numbers. Only use what the tools return. If an item has no \
nutrition data, say "nutrition not listed" and don't guess.
- Allergies, diet and the never_eat list are hard rules. Dislikes are soft: avoid them unless nothing else \
works, and say so.
- Only call update_prefs when the user clearly states a lasting preference ("I'm vegan now", "remember I'm \
allergic to peanuts", "never give me olives again" -> never_eat, "I love the tofu scramble" -> favorites). \
A one-off wish ("I want pizza today") is not a preference.
- A meal is usually 2-4 items. You may use up to 3 servings of an item if that's realistic (two Greek yogurts).
- If the goal can't be reached, build the closest plan, say how many grams short it is, and give one concrete \
fix (for example "add a second Greek yogurt at breakfast").
- Weekends may have brunch instead of separate breakfast and lunch; breakfast/lunch searches include brunch items.
"""


def build_options(plan_date: date, resume: str | None = None) -> ClaudeAgentOptions:
    now = datetime.now()
    prefs = T.read_prefs()
    name_line = (f"The user's name is {prefs['name']}; use it once, naturally." if prefs.get("name")
                 else "You don't know the user's name; don't make one up.")
    cheat_line = (f"{plan_date.strftime('%A')} is one of the user's cheat days: relax calorie limits, pick the "
                  "most enjoyable options that still respect allergies, diet and never_eat, include a treat, "
                  "and mention it's a cheat day." if T.is_cheat_day(plan_date, prefs)
                  else "The plan date is not a cheat day.")
    system = SYSTEM_PROMPT.format(weekday=now.strftime("%A"), today=now.date().isoformat(),
                                  now=now.strftime("%H:%M"), plan_date=plan_date.isoformat(),
                                  plan_weekday=plan_date.strftime("%A"), name_line=name_line,
                                  treats=prefs.get("treats") or "sometimes", cheat_line=cheat_line)
    return ClaudeAgentOptions(
        system_prompt=system,
        mcp_servers={SERVER: build_server()},
        allowed_tools=[f"mcp__{SERVER}__{name}" for name in TOOL_NAMES],
        tools=[],                      # no built-in Claude Code tools (no shell, no files): only ours
        setting_sources=[],            # ignore the user's Claude Code settings/plugins
        model=config.MODEL,
        effort=config.EFFORT,
        thinking={"type": "adaptive", "display": "summarized"},
        max_turns=config.MAX_TURNS,
        cwd=str(config.ROOT),
        resume=resume,
        env={"ENABLE_TOOL_SEARCH": "false"},  # load all 11 tool schemas up front
    )


def _short_name(tool_name: str) -> str:
    return tool_name.split("__")[-1]


async def run_agent_async(goal: str, plan_date: date | None = None,
                          on_event: Callable[[dict], None] | None = None,
                          resume: str | None = None) -> AgentRun:
    plan_date = plan_date or date.today()
    run = AgentRun(goal=goal)
    _current.update(run=run, on_event=on_event)
    start = time.monotonic()
    if os.getenv("ANTHROPIC_API_KEY"):
        _emit({"type": "warning", "text": "ANTHROPIC_API_KEY is set, so this run bills your API account "
                                          "instead of your Claude plan."})
    _emit({"type": "goal", "text": goal, "date": plan_date.isoformat(), "followup": bool(resume)})
    prompt = goal if resume else f"Plan date: {plan_date.isoformat()}.\nMy goal: {goal}"
    try:
        async for message in query(prompt=prompt, options=build_options(plan_date, resume)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ThinkingBlock) and block.thinking.strip():
                        _emit({"type": "thinking", "text": block.thinking.strip()})
                    elif isinstance(block, TextBlock) and block.text.strip():
                        _emit({"type": "text", "text": block.text.strip()})
                    elif isinstance(block, ToolUseBlock):
                        pass  # reported by the tool wrapper with its result
            elif isinstance(message, ResultMessage):
                run.session_id = message.session_id
                run.num_turns = message.num_turns
                run.cost_usd = message.total_cost_usd
                if message.is_error:
                    run.error = message.result or message.subtype
                else:
                    run.reply = message.result or ""
    except Exception as exc:
        run.error = _explain_error(exc)
    finally:
        run.duration_s = round(time.monotonic() - start, 1)
        if run.error:
            _emit({"type": "error", "text": run.error})
        elif run.plan is None:
            _emit({"type": "warning", "text": "The agent finished without an accepted plan."})
        _emit({"type": "done", "seconds": run.duration_s, "tool_calls": run.tool_calls,
               "turns": run.num_turns, "adaptations": run.adaptations})
        _current.update(run=None, on_event=None)
    return run


def _explain_error(exc: Exception) -> str:
    text = str(exc)
    if "Not logged in" in text or "/login" in text:
        return ("Claude Code isn't signed in where this app is running. On your own computer, open a terminal "
                "(PowerShell on Windows, Terminal on Mac) and run:  claude auth login  "
                "(sign in with your Claude plan), then try again.")
    if "not found" in text.lower() and "claude" in text.lower():
        return "Claude Code isn't installed. See README > Setup."
    return f"{type(exc).__name__}: {text}"


def run_agent(goal: str, plan_date: date | None = None,
              on_event: Callable[[dict], None] | None = None, resume: str | None = None) -> AgentRun:
    """Synchronous wrapper (for scripts and Streamlit)."""
    return asyncio.run(run_agent_async(goal, plan_date, on_event, resume))


def default_goal(prefs: dict | None = None) -> str:
    """The goal used by the morning run, built from saved preferences."""
    prefs = prefs or T.read_prefs()
    bits = ["Plan my breakfast, lunch and dinner"]
    if prefs.get("diet"):
        bits.append(f"I'm {' and '.join(prefs['diet'])}")
    if prefs.get("daily_protein_goal_g"):
        bits.append(f"aim for {prefs['daily_protein_goal_g']} g protein")
    if prefs.get("daily_calorie_goal"):
        bits.append(f"about {prefs['daily_calorie_goal']} kcal total")
    if prefs.get("favorite_hall"):
        bits.append(f"I slightly prefer {prefs['favorite_hall']}")
    return ", ".join(bits) + "."
