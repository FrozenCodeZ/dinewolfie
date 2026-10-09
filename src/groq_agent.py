"""Second engine: the same tools, prompt and plan checker, with a Groq-hosted model.

The Claude Agent SDK runs its own loop. Groq doesn't have one, so this file is the loop:

    send messages + tool list -> model asks for tools -> run them -> send results back
    -> repeat until the model stops asking for tools.

Every tool call goes through agent.call_tool, so the live trace, adaptations and the
final plan work exactly like the Claude engine.
"""
from __future__ import annotations

import json
from datetime import date

import config
from src import agent as A

# Groq's free tier has tight per-minute token limits, so steer the model toward small results.
GROQ_NOTE = ("\n\nYou run with tight token limits: prefer search_items (limit 8 to 12) and compare_halls over "
             "get_menu, and call one tool at a time.")
MAX_RESULT_CHARS = 15_000
MAX_BAD_TOOL_RETRIES = 2


def tool_schemas() -> list[dict]:
    return [{"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
            for name, desc, schema, _fn, _ro in A.active_specs()]


def _assistant_message(msg) -> dict:
    out = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [{"id": tc.id, "type": "function",
                              "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                             for tc in msg.tool_calls]
    return out


def _result_text(result: dict) -> str:
    text = json.dumps(result, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    # Too big for the model's limits: say so instead of sending a cut-off JSON blob.
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
        return {"status": "error", "error": f"There is no tool called {name!r}."}
    result, _ = A.call_tool(name, args)
    return result


def run_groq(run, goal: str, plan_date: date, history: list[dict] | None, engine, client=None) -> None:
    """Fill in `run` (plan, reply, messages) by driving a Groq model through our tools."""
    if not engine.api_key:
        raise RuntimeError("Groq needs an API key. Paste one in the sidebar, or add GROQ_API_KEY to the "
                           "app's secrets.")
    if client is None:
        from groq import Groq
        client = Groq(api_key=engine.api_key, max_retries=4, timeout=90)
    model = engine.model or config.GROQ_MODEL
    allowed = {spec[0] for spec in A.active_specs()}

    if history:
        messages = list(history) + [{"role": "user", "content": goal}]
    else:
        messages = [{"role": "system", "content": A.system_prompt(plan_date) + GROQ_NOTE},
                    {"role": "user", "content": f"Plan date: {plan_date.isoformat()}.\nMy goal: {goal}"}]

    nudged, bad_tool_retries = False, 0
    for _ in range(config.MAX_TURNS):
        try:
            resp = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas(),
                                                  tool_choice="auto", temperature=0.3,
                                                  max_completion_tokens=4096)
        except Exception as exc:
            # Groq rejects a reply whose tool call it couldn't parse; asking again usually works.
            if "tool_use_failed" in str(exc) and bad_tool_retries < MAX_BAD_TOOL_RETRIES:
                bad_tool_retries += 1
                continue
            raise
        run.num_turns += 1
        msg = resp.choices[0].message
        messages.append(_assistant_message(msg))
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
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": _result_text(result)})
    else:
        run.error = "The agent ran out of steps before finishing."
    run.messages = messages
