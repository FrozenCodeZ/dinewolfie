"""The DineWolfie agent: Claude + our tools, running the agent loop.

    Goal -> decide -> use tool(s) -> observe -> (adapt) -> finish

The Claude Agent SDK runs the loop (src/groq_agent.py is the same loop for Groq). We give it:
  * a system prompt (how to behave),
  * our tools from src/tools.py, wrapped as an in-process MCP server,
  * a callback so every step can be shown live in the terminal or the web UI.

Two modes:
  * fast (default): the code runs the standard lookups first (src/briefing.py) and the model
    usually decides and submits the plan in one call. The run stops as soon as the checker
    accepts it, and the reply is written by code from the checked plan.
  * thorough: the model drives every step itself (make_plan, searches, comparisons, ...).

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
from contextvars import ContextVar
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, TextBlock, ThinkingBlock,
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
           "tips": {**_STRS, "description": "Optional practical tips, e.g. how to close a protein gap."},
           "message": {"type": "string", "description": "One or two warm sentences to the user, no numbers "
                                                         "(the app adds the totals)."}},
          ["date", "meals", "headline"]),
     T.submit_plan, False),
    ("lookup_nutrition",
     "Search the web for typical nutrition of a dish ONLY when SBU's menu lists it without nutrition. "
     "Results are estimates for a typical version, not SBU's recipe: say so, and never add them to totals.",
     _obj({"item_name": {"type": "string"}, "details": {"type": "string", "description": "Optional: size, style"}},
          ["item_name"]),
     T.lookup_nutrition, True),
]
TOOL_NAMES = [spec[0] for spec in TOOL_SPECS]
# Fast mode: the briefing already covers memory, history, menus and hall comparisons.
FAST_TOOLS = {"search_items", "sum_nutrition", "submit_plan", "update_prefs", "lookup_nutrition"}


def active_specs(fast: bool = False) -> list[tuple]:
    """The tools this run can use: the web lookup only when a Tavily key is set."""
    return [s for s in TOOL_SPECS if (s[0] != "lookup_nutrition" or T.tavily_key())
            and (not fast or s[0] in FAST_TOOLS)]


# --- Run state + events -------------------------------------------------------------------

@dataclass
class Engine:
    """Which AI runs the agent loop.

    provider: "claude" (Claude Agent SDK) or "groq".
    api_key:  for Claude, None means "use this computer's Claude plan login";
              for Groq a key is required.
    """
    provider: str = "claude"
    api_key: str | None = None
    model: str | None = None
    mode: str = "fast"  # fast | thorough (see the top of this file)

    @property
    def fast(self) -> bool:
        return self.mode != "thorough"

    @property
    def label(self) -> str:
        if self.provider == "groq":
            return f"Groq ({self.model or config.GROQ_MODEL}, {self.mode})"
        return (f"Claude ({self.model or config.MODEL}, {self.mode}"
                f"{', API key' if self.api_key else ', Claude plan'})")


@dataclass
class AgentRun:
    goal: str
    engine: str = "claude"
    plan: dict | None = None
    reply: str = ""
    events: list[dict] = field(default_factory=list)
    session_id: str | None = None       # Claude: lets a follow-up continue the conversation
    messages: list[dict] | None = None  # Groq: the conversation so far, for follow-ups
    error: str | None = None
    num_turns: int = 0
    duration_s: float = 0.0
    cost_usd: float | None = None
    tool_calls: int = 0
    adaptations: int = 0


# One value per session/thread, so two people using the hosted app never share a run.
_current: ContextVar = ContextVar("dinewolfie_run", default=None)


def _emit(event: dict) -> None:
    state = _current.get()
    if state is None:
        return
    event.setdefault("t", round(time.monotonic(), 2))
    state["run"].events.append(event)
    callback = state["on_event"]
    if callback is not None:
        try:
            callback(event)
        except Exception:  # the UI must never crash the agent
            pass


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
    if name == "scout_meal":
        return {"halls": {h: {"status": v["status"], "fits": v["fits"], "shown": len(v["items"])}
                          for h, v in result.get("halls", {}).items()}}
    return {}


def tool_started(name: str, args: dict, auto: bool = False) -> None:
    """auto=True: the code ran this lookup itself (fast mode), not the model."""
    _emit({"type": "tool_call", "tool": name, "input": args, **({"auto": True} if auto else {})})
    state = _current.get()
    if state is not None:
        state["run"].tool_calls += 1


def tool_finished(name: str, args: dict, result: dict | None, error: str | None = None,
                  auto: bool = False) -> None:
    """Report a tool's outcome to the trace. Shared by the Claude and Groq loops."""
    state = _current.get()
    run = state["run"] if state else None
    flag = {"auto": True} if auto else {}
    if error is not None:
        _emit({"type": "tool_result", "tool": name, "ok": False, "summary": error, **flag})
        return
    status = result.get("status", "ok")
    _emit({"type": "tool_result", "tool": name, "ok": status in ("ok", "accepted"), "status": status,
           "summary": result.get("summary", ""), "data": _trace_data(name, result), **flag})
    if name == "make_plan":
        _emit({"type": "plan", "steps": result.get("steps", [])})
    if name == "log_adaptation":
        if run is not None:
            run.adaptations += 1
        _emit({"type": "adaptation", "problem": args.get("problem", ""), "change": args.get("change", ""),
               "reason": args.get("reason", "")})
    if name == "submit_plan" and status == "accepted" and run is not None:
        run.plan = result["plan"]
        if run.adaptations == 0:  # changes of course listed in the plan but never logged one by one
            for text in result["plan"].get("adaptations") or []:
                run.adaptations += 1
                _emit({"type": "adaptation", "problem": str(text), "change": "", "reason": ""})
        _emit({"type": "final_plan", "plan": result["plan"]})


def call_tool(name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool synchronously and report it. Returns (result, is_error)."""
    fn = next(spec[3] for spec in TOOL_SPECS if spec[0] == name)
    tool_started(name, args)
    try:
        result = fn(**args)
    except Exception as exc:  # report the failure to the model so it can adapt
        message = f"{type(exc).__name__}: {exc}"
        tool_finished(name, args, None, message)
        return {"status": "error", "error": message}, True
    tool_finished(name, args, result)
    return result, False


def _wrap(name: str, description: str, schema: dict, fn: Callable, read_only: bool):
    """Turn a plain function into an SDK tool that also reports to the trace."""

    @tool(name, description, schema,
          annotations=ToolAnnotations(readOnlyHint=read_only, maxResultSizeChars=200_000))
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        tool_started(name, args)
        try:
            result = await asyncio.to_thread(fn, **args)
        except Exception as exc:  # report the failure to Claude so it can adapt
            message = f"{type(exc).__name__}: {exc}"
            tool_finished(name, args, None, message)
            return {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": message})}],
                    "is_error": True}
        tool_finished(name, args, result)
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    return handler


def build_server(fast: bool = False):
    return create_sdk_mcp_server(name=SERVER, version="1.0.0",
                                 tools=[_wrap(*spec) for spec in active_specs(fast)])


# --- Prompt ---------------------------------------------------------------------------------

SYSTEM_PROMPT = """You are DineWolfie, a meal-planning agent for Stony Brook University's two all-you-care-to-eat \
dining halls: East Side Dine-In ("East") and West Side Dine-In ("West"). A student tells you a goal; you plan \
their day of eating from the real menus.

Today is {weekday}, {today}; local time {now}. The plan date is {plan_date} ({plan_weekday}) unless the user \
says otherwise.

{how}

Be a good friend about food, not a calculator:
- {name_line}
- Give each meal 1 swap in "alternatives" (similar protein, can be at the other hall) with a short note on \
when to pick it ("if the Grill line is long", "if you want something warm").
- If one of the user's favorites is on today's menu (marked "favorite"), use it and say so.
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
- If the goal can't be reached, build the closest plan and give one concrete fix in "tips" (for example "add a \
second Greek yogurt at breakfast").
- Weekends may have brunch instead of separate breakfast and lunch; breakfast/lunch searches include brunch items.
{lookup_line}"""

HOW_THOROUGH = """How you work: goal -> decide -> use tools -> observe -> adapt -> finish.
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
words): each meal with its hall and items, the totals, and any gap or caveat."""

HOW_FAST = """How you work (fast mode): the user's message includes a BRIEFING the app gathered just now: their \
memory, their recent plans, and the best candidate items at each hall for each meal, already filtered for their \
allergies, diet, never-eat list and dislikes. Trust it and don't fetch it again.
1. Decide from the briefing: 2-4 items per meal, ids exactly as listed. Add variety compared with recent plans.
2. Only if the briefing lacks something the goal needs (a specific dish, a lighter option, more choices), call \
search_items for that meal. Don't call tools just to double-check: the checker computes every total itself.
3. If the briefing forces a change of course (a hall has no menu posted, nothing fits, the goal can't be \
reached), adapt: use the other hall, combine items or servings, or relax the least important wish. Never relax \
an allergy, the diet or the never-eat list. Write each such change into "adaptations"; leave it empty when \
nothing went wrong (ordinary choices are not adaptations).
4. Call submit_plan once with every meal: items, a one-line reason, one swap in "alternatives", "adaptations", \
optional "tips", and "message": one or two warm sentences to the user with no numbers (the app adds the \
totals). If it is rejected, fix the listed problems and submit again. Once it is accepted you are done."""


def system_prompt(plan_date: date, fast: bool = False) -> str:
    now = datetime.now()
    prefs = T.read_prefs()
    name_line = (f"The user's name is {prefs['name']}; use it once, naturally." if prefs.get("name")
                 else "You don't know the user's name; don't make one up.")
    cheat_line = (f"{plan_date.strftime('%A')} is one of the user's cheat days: relax calorie limits, pick the "
                  "most enjoyable options that still respect allergies, diet and never_eat, include a treat, "
                  "and mention it's a cheat day." if T.is_cheat_day(plan_date, prefs)
                  else "The plan date is not a cheat day.")
    lookup_line = ("- If an item you want to recommend has no nutrition listed, you may call lookup_nutrition once "
                   "for it. Present the result as 'about X g protein (web estimate)' and keep it out of totals.\n"
                   if T.tavily_key() else "")
    return SYSTEM_PROMPT.format(how=HOW_FAST if fast else HOW_THOROUGH,
                                weekday=now.strftime("%A"), today=now.date().isoformat(),
                                now=now.strftime("%H:%M"), plan_date=plan_date.isoformat(),
                                plan_weekday=plan_date.strftime("%A"), name_line=name_line,
                                treats=prefs.get("treats") or "sometimes", cheat_line=cheat_line,
                                lookup_line=lookup_line)


def _stop_when_accepted(run: AgentRun):
    """Fast mode: end the loop the moment the checker accepts a plan (no extra model turn for a
    summary; the reply is written by code from the checked plan)."""

    async def hook(hook_input, tool_use_id, context):
        if run.plan is not None:
            return {"continue_": False, "stopReason": "Plan accepted."}
        return {}

    return hook


def build_options(plan_date: date, resume: str | None = None, engine: Engine | None = None,
                  run: AgentRun | None = None) -> ClaudeAgentOptions:
    engine = engine or Engine()
    env = {"ENABLE_TOOL_SEARCH": "false"}  # load all tool schemas up front
    if engine.api_key:
        env["ANTHROPIC_API_KEY"] = engine.api_key  # bill this key instead of a Claude plan login
    hooks = None
    if engine.fast and run is not None:
        hooks = {"PostToolUse": [HookMatcher(matcher=f"mcp__{SERVER}__submit_plan",
                                             hooks=[_stop_when_accepted(run)])]}
    return ClaudeAgentOptions(
        system_prompt=system_prompt(plan_date, engine.fast),
        mcp_servers={SERVER: build_server(engine.fast)},
        allowed_tools=[f"mcp__{SERVER}__{spec[0]}" for spec in active_specs(engine.fast)],
        tools=[],                      # no built-in Claude Code tools (no shell, no files): only ours
        setting_sources=[],            # ignore the user's Claude Code settings/plugins
        model=engine.model or config.MODEL,
        effort=config.FAST_EFFORT if engine.fast else config.EFFORT,
        thinking={"type": "adaptive", "display": "summarized"},
        max_turns=config.FAST_MAX_TURNS if engine.fast else config.MAX_TURNS,
        cwd=str(config.ROOT),
        resume=resume,
        env=env,
        hooks=hooks,
    )


def _short_name(tool_name: str) -> str:
    return tool_name.split("__")[-1]


async def _run_claude(run: AgentRun, goal: str, plan_date: date, resume: str | None, engine: Engine) -> None:
    if os.getenv("ANTHROPIC_API_KEY") and not engine.api_key:
        _emit({"type": "warning", "text": "ANTHROPIC_API_KEY is set, so this run bills your API account "
                                          "instead of your Claude plan."})
    prompt = goal if resume else f"Plan date: {plan_date.isoformat()}.\nMy goal: {goal}"
    if engine.fast and not resume:
        from src.briefing import gather  # imported here: briefing imports this module
        prompt += "\n\n" + gather(plan_date, goal)
    async for message in query(prompt=prompt, options=build_options(plan_date, resume, engine, run)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingBlock) and block.thinking.strip():
                    _emit({"type": "thinking", "text": block.thinking.strip()})
                elif isinstance(block, TextBlock) and block.text.strip():
                    _emit({"type": "text", "text": block.text.strip()})
        elif isinstance(message, ResultMessage):
            run.session_id = message.session_id
            run.num_turns = message.num_turns
            run.cost_usd = message.total_cost_usd
            if engine.fast and run.plan is not None:
                run.reply = plan_reply(run.plan)  # stopped on purpose right after the checker accepted
            elif message.is_error:
                run.error = message.result or message.subtype
            else:
                run.reply = message.result or ""


async def run_agent_async(goal: str, plan_date: date | None = None,
                          on_event: Callable[[dict], None] | None = None,
                          resume: str | list | None = None, engine: Engine | None = None) -> AgentRun:
    """Run the agent once. `resume` continues a conversation: a Claude session id, or Groq messages."""
    engine = engine or Engine()
    plan_date = plan_date or date.today()
    run = AgentRun(goal=goal, engine=engine.provider)
    token = _current.set({"run": run, "on_event": on_event})
    start = time.monotonic()
    _emit({"type": "goal", "text": goal, "date": plan_date.isoformat(), "followup": bool(resume),
           "engine": engine.label})
    try:
        if engine.provider == "groq":
            from src.groq_agent import run_groq  # only imported when used
            run_groq(run, goal, plan_date, resume if isinstance(resume, list) else None, engine)
        else:
            await _run_claude(run, goal, plan_date, resume if isinstance(resume, str) else None, engine)
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
        _current.reset(token)
    return run


def _explain_error(exc: Exception) -> str:
    text = str(exc)
    if "Not logged in" in text or "/login" in text:
        return ("Claude Code isn't signed in where this app is running. On your own computer, open a terminal "
                "(PowerShell on Windows, Terminal on Mac) and run:  claude auth login  "
                "(sign in with your Claude plan), then try again.")
    if "not found" in text.lower() and "claude" in text.lower():
        return "Claude Code isn't installed. See README > Setup."
    low = text.lower()
    if "invalid x-api-key" in low or "authentication_error" in low or "invalid api key" in low:
        return "That API key was rejected. Check it in the sidebar (no extra spaces) and try again."
    if "rate limit" in low or "rate_limit" in low:
        return "The AI service is rate-limiting us. Wait a minute and try again, or switch engines in the sidebar."
    return f"{type(exc).__name__}: {text}"


def run_agent(goal: str, plan_date: date | None = None, on_event: Callable[[dict], None] | None = None,
              resume: str | list | None = None, engine: Engine | None = None) -> AgentRun:
    """Synchronous wrapper (for scripts and Streamlit)."""
    return asyncio.run(run_agent_async(goal, plan_date, on_event, resume, engine))


def engine_from_env() -> Engine:
    """For the terminal and the morning run: DINEWOLFIE_ENGINE=claude|groq plus the matching key,
    DINEWOLFIE_MODE=fast|thorough."""
    if os.getenv("DINEWOLFIE_ENGINE", "claude").strip().lower() == "groq":
        return Engine("groq", os.getenv("GROQ_API_KEY"), config.GROQ_MODEL, config.MODE)
    return Engine("claude", None, None, config.MODE)  # the SDK picks up ANTHROPIC_API_KEY itself if set


MEAL_NAMES = {"breakfast": "Breakfast", "brunch": "Brunch", "lunch": "Lunch", "dinner": "Dinner",
              "late_night": "Late night"}


def plan_reply(plan: dict) -> str:
    """The short reply for a checked plan, written by code so every number is exact (fast mode)."""
    lines = [plan["message"], ""] if plan.get("message") else []
    for meal in plan["meals"]:
        names = ", ".join((f"{i['servings']:g}× " if i["servings"] != 1 else "") + i["name"] for i in meal["items"])
        protein = (meal.get("totals") or {}).get("protein_g", 0)
        lines.append(f"- **{MEAL_NAMES.get(meal['meal'], meal['meal'])} at {meal['hall']}:** {names} "
                     f"({protein} g protein)")
    t = plan["totals"]
    total = f"**Total:** {t['protein_g']} g protein, {t['calories']} kcal"
    if plan.get("protein_gap_g"):
        total += f" ({plan['protein_gap_g']} g short of your {plan['protein_goal_g']} g goal)"
    lines += ["", total + "."]
    if plan.get("tips"):
        lines.append(f"Tip: {plan['tips'][0]}")
    return "\n".join(lines)


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
