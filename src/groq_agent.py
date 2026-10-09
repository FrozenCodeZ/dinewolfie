"""Second engine: the same tools, prompt and plan checker, with a Groq-hosted model.

The Claude Agent SDK runs its own loop. Groq doesn't have one, so this file is the loop:

    send messages + tool list -> model asks for tools -> run them -> send results back
    -> repeat until the model stops asking for tools.

Every tool call goes through agent.call_tool, so the live trace, adaptations and the
final plan work exactly like the Claude engine.

Groq's free tier allows only a few thousand tokens per minute, and every request re-sends
the whole conversation. So this loop runs "lean": it skips the two bulkiest tools, sends
slimmed results, shrinks older results to one-line summaries (keeping item ids), and when
Groq says "slow down" it waits visibly instead of failing.

In fast mode (the default) the first message already carries the briefing (src/briefing.py),
so a plan usually takes one request of about 5,000 tokens instead of ~12 requests and ~40,000,
and the loop ends the moment the checker accepts the plan.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date

import config
from src import agent as A

GROQ_NOTE = ("\n\nYou run with tight token limits: use search_items (limit 8) and compare_halls, call one tool "
             "at a time, and keep notes short.")
SKIP_TOOLS = {"get_menu", "get_item_details"}  # the two biggest results; search_items covers the need
SEARCH_LIMIT = 8
KEEP_FULL_ROUNDS = 2          # the last N tool results stay in full; older ones are shrunk
MAX_RESULT_CHARS = 6_000
MAX_BAD_TOOL_RETRIES = 2
MAX_RATE_WAITS = 6
MAX_WAIT_S = 60
MAX_COMPLETION_TOKENS = 2048  # a whole fast-mode plan is ~1,000; a smaller cap may also count less against the limit
FAST_PER_HALL = 7             # candidates per place in Groq's briefing (Claude gets 10)
SHORT_WAIT_S = 8              # wait this long at most for a model to free up before giving up on the AI
WAIT_BUDGET_S = 20            # total waiting per plan before falling back to the built-in planner

# Groq's limits are per organization *and per model*, and shared by every visitor using the same
# key. So when a model is at its limit we note until when, and every visitor skips it until then.
_cooldown: dict[str, float] = {}   # model -> time.monotonic() when it may be tried again
_unavailable: set[str] = set()     # models Groq says don't exist (renamed or retired)
_models_lock = threading.Lock()


def model_chain(primary: str) -> list[str]:
    """The chosen model first, then the fallbacks, minus models Groq doesn't have."""
    chain = [primary] + [m for m in config.GROQ_FALLBACK_MODELS if m != primary]
    with _models_lock:
        return [m for m in chain if m not in _unavailable]


def next_model(primary: str, now: float) -> tuple[str | None, float]:
    """(first model that isn't cooling down, or None; seconds until the soonest one frees up)."""
    chain = model_chain(primary)
    if not chain:
        raise RuntimeError("None of the Groq models DineWolfie knows are available any more. Pick another "
                           "model, or set DINEWOLFIE_GROQ_FALLBACKS to current ones from console.groq.com.")
    with _models_lock:
        for m in chain:
            if _cooldown.get(m, 0) <= now:
                return m, 0.0
        return None, max(0.0, min((_cooldown.get(m, now) for m in chain), default=now) - now)


def _cool(model: str, seconds: float) -> None:
    with _models_lock:
        _cooldown[model] = max(_cooldown.get(model, 0), time.monotonic() + seconds)


def _missing_model(exc) -> bool:
    text = str(exc).lower()
    return getattr(exc, "status_code", None) == 404 or "model_not_found" in text or "decommissioned" in text \
        or ("model" in text and "does not exist" in text)


def _lean_schema(schema):
    """Tool schemas without nested property descriptions: the model rarely needs them, and every
    request re-sends every schema. Top-level tool descriptions stay."""
    if isinstance(schema, dict):
        return {k: _lean_schema(v) for k, v in schema.items() if k != "description"}
    if isinstance(schema, list):
        return [_lean_schema(v) for v in schema]
    return schema


def lean_specs(fast: bool = False) -> list[tuple]:
    return [spec for spec in A.active_specs(fast) if spec[0] not in SKIP_TOOLS]


def tool_schemas(fast: bool = False) -> list[dict]:
    return [{"type": "function", "function": {"name": name, "description": desc,
                                              "parameters": _lean_schema(schema) if fast else schema}}
            for name, desc, schema, _fn, _ro in lean_specs(fast)]


def model_options(model: str) -> dict:
    """gpt-oss models can be told to think briefly, which saves time and tokens."""
    if model.startswith("openai/gpt-oss") and config.GROQ_REASONING_EFFORT in ("low", "medium", "high"):
        return {"reasoning_effort": config.GROQ_REASONING_EFFORT}
    return {}


def _row(item: dict) -> dict:
    """The few fields the model needs to choose an item."""
    row = {"id": item["id"], "name": item["name"], "protein_g": item.get("protein_g"),
           "kcal": item.get("calories")}
    for key in ("hall", "station"):
        if item.get(key):
            row[key] = item[key]
    for flag in ("favorite", "treat"):
        if item.get(flag):
            row[flag] = True
    return row


def lean_result(name: str, result: dict) -> dict:
    """A slimmed copy of a tool result for Groq (the trace and checker still see the full one)."""
    out = {k: result[k] for k in ("status", "summary", "error", "problems", "note", "answer") if k in result}
    if "items" in result and name in ("search_items", "sum_nutrition"):
        out["items"] = [_row(i) if "id" in i else i for i in result["items"]]
    if name == "search_items":
        out["excluded_counts"] = result.get("excluded_counts", {})
    if name == "compare_halls":
        out["halls"] = {h: {"menu_status": v["menu_status"], "matching_items": v["matching_items"],
                            "best": v["best_protein_items"]} for h, v in result.get("halls", {}).items()}
    if name == "sum_nutrition":
        out["totals"] = result.get("totals")
    if name == "get_prefs":
        out["prefs"] = {k: v for k, v in result.get("prefs", {}).items() if v not in (None, [], "")}
    if name == "get_meal_history":
        out["recent_plans"] = result.get("recent_plans", [])
    if name == "submit_plan" and result.get("status") == "accepted":
        plan = result["plan"]
        out.update(totals=plan["totals"], protein_gap_g=plan.get("protein_gap_g"))
    return out


def _shrunk(content: str) -> str:
    """Older results: keep the summary and the item ids/names, drop everything else."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content[:300]
    small = {k: data[k] for k in ("status", "summary") if k in data}
    if data.get("items"):
        small["items"] = [f"{i.get('id')} {i.get('name')} ({i.get('protein_g')} g)" for i in data["items"]
                          if isinstance(i, dict)]
    return json.dumps(small)


def shrink_history(messages: list[dict], keep: int = KEEP_FULL_ROUNDS) -> None:
    """Shrink all but the last `keep` tool results, in place."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-keep] if keep else tool_idx:
        if not messages[i].get("_shrunk"):
            messages[i]["content"] = _shrunk(messages[i]["content"])
            messages[i]["_shrunk"] = True


def _result_text(name: str, result: dict) -> str:
    text = json.dumps(lean_result(name, result), default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return json.dumps({"status": "too_large", "summary": result.get("summary", ""),
                       "hint": "That result was too long. Use search_items with filters and a small limit."})


def _execute(name: str, raw_args: str | None, allowed: set[str]) -> dict:
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return {"status": "error", "error": f"Tool arguments were not valid JSON ({exc}). Try again."}
    if name not in allowed:
        return {"status": "error", "error": f"There is no tool called {name!r} here. Use search_items."}
    if name == "search_items":
        args["limit"] = min(int(args.get("limit") or SEARCH_LIMIT), SEARCH_LIMIT)
    result, _ = A.call_tool(name, args)
    return result


def _retry_after(exc) -> float | None:
    """Seconds to wait if Groq rate-limited us (429) or the request was over the minute budget (413)."""
    status = getattr(exc, "status_code", None)
    if status not in (413, 429):
        return None
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    try:
        return min(MAX_WAIT_S, float(headers.get("retry-after") or 10))
    except (TypeError, ValueError):
        return 10.0


def _wire(messages: list[dict]) -> list[dict]:
    """Messages as Groq expects them (without our private bookkeeping keys)."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def run_groq(run, goal: str, plan_date: date, history: list[dict] | None, engine, client=None,
             sleep=time.sleep) -> None:
    """Fill in `run` (plan, reply, messages) by driving a Groq model through our tools."""
    if not engine.api_key:
        raise RuntimeError("Groq needs an API key. Paste one in the sidebar, or add GROQ_API_KEY to the "
                           "app's secrets.")
    if client is None:
        from groq import Groq
        client = Groq(api_key=engine.api_key, max_retries=0, timeout=90)  # we handle waits ourselves
    primary = engine.model or config.GROQ_MODEL
    fast = engine.fast
    allowed = {spec[0] for spec in lean_specs(fast)}
    tools = tool_schemas(fast)

    if history:
        messages = list(history) + [{"role": "user", "content": goal}]
    else:
        first = f"Plan date: {plan_date.isoformat()}.\nMy goal: {goal}"
        if fast:
            from src.briefing import gather
            first += "\n\n" + gather(plan_date, goal, per_hall=FAST_PER_HALL)
        messages = [{"role": "system", "content": A.system_prompt(plan_date, fast) + ("" if fast else GROQ_NOTE)},
                    {"role": "user", "content": first}]

    nudged, bad_tool_retries, waits, waited = False, 0, 0, 0.0
    turns, model = 0, primary
    while turns < config.MAX_TURNS:
        shrink_history(messages)
        model, free_in = next_model(primary, time.monotonic())
        if model is None:  # every model is at its limit
            if config.FALLBACK_TO_BUILTIN and (free_in > SHORT_WAIT_S or waited + free_in > WAIT_BUDGET_S):
                raise A.AIBusy(f"Every Groq model is at its free-tier limit for about {free_in:.0f} more seconds.")
            if waits >= MAX_RATE_WAITS:
                raise A.AIBusy("Groq kept rate-limiting us.")
            waits += 1
            waited += free_in
            A._emit({"type": "wait", "text": f"Waiting {free_in:.0f} s for Groq's free-tier limit…"})
            sleep(free_in)
            continue
        try:
            resp = client.chat.completions.create(model=model, messages=_wire(messages), tools=tools,
                                                  tool_choice="auto", temperature=0.3,
                                                  max_completion_tokens=MAX_COMPLETION_TOKENS,
                                                  **model_options(model))
        except Exception as exc:
            wait = _retry_after(exc)
            if wait is not None:
                _cool(model, wait)
                if getattr(exc, "status_code", None) == 413:
                    shrink_history(messages, keep=0)  # request too big for the minute: shrink everything
                following, _ = next_model(primary, time.monotonic())
                if following:
                    A._emit({"type": "wait", "text": f"{model} is at Groq's free-tier limit; switching to "
                                                     f"{following}."})
                continue
            if _missing_model(exc):
                with _models_lock:
                    _unavailable.add(model)
                continue
            # Groq rejects a reply whose tool call it couldn't parse; asking again usually works.
            if "tool_use_failed" in str(exc) and bad_tool_retries < MAX_BAD_TOOL_RETRIES:
                bad_tool_retries += 1
                continue
            raise
        turns += 1
        run.num_turns += 1
        msg = resp.choices[0].message
        assistant = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant["tool_calls"] = [{"id": tc.id, "type": "function",
                                        "function": {"name": tc.function.name,
                                                     "arguments": tc.function.arguments or "{}"}}
                                       for tc in msg.tool_calls]
        messages.append(assistant)
        reasoning = (getattr(msg, "reasoning", None) or "").strip()
        if reasoning:
            A._emit({"type": "thinking", "text": reasoning[:600]})

        if not msg.tool_calls:
            if run.plan is None and not nudged:  # one reminder to finish properly
                nudged = True
                messages.append({"role": "user", "content": "Please finish: call submit_plan with the plan, "
                                                            "then give me the short summary."})
                continue
            run.reply = (msg.content or "").strip()
            break
        if msg.content and msg.content.strip():
            A._emit({"type": "text", "text": msg.content.strip()})
        for tc in msg.tool_calls:
            result = _execute(tc.function.name, tc.function.arguments, allowed)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": _result_text(tc.function.name, result)})
        if fast and run.plan is not None:
            # Accepted: no extra request for a summary. Close the turn so follow-ups stay well-formed.
            run.reply = A.plan_reply(run.plan)
            messages.append({"role": "assistant", "content": "Plan accepted and shown to the user."})
            break
    else:
        run.error = "The agent ran out of steps before finishing."
    run.messages = _wire(messages)
