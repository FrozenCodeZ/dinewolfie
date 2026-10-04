"""Terminal mode: watch the agent think, step by step.

    python -m src.cli "I'm vegetarian, aiming for 120g protein, near West at lunch"
    python -m src.cli --date tomorrow "high protein, under 2000 kcal"
    python -m src.cli --simulate missing:West:dinner "plan my day"      # demo: West dinner not posted
    python -m src.cli --simulate offline "plan my day"                  # demo: network down, use cache
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import config
from src import tools as T
from src.agent import default_goal, run_agent

if os.name == "nt":
    os.system("")  # turn on ANSI colors in the Windows console
    sys.stdout.reconfigure(encoding="utf-8")

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED, CYAN, MAGENTA = "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[35m"


def _short(value, limit=140) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_event(event: dict) -> None:
    kind = event["type"]
    if kind == "goal":
        print(f"\n{BOLD}🎯 Goal{RESET} ({event['date']}): {event['text']}")
    elif kind == "plan":
        print(f"{CYAN}🗺️  Plan:{RESET}")
        for n, step in enumerate(event["steps"], 1):
            print(f"   {n}. {step}")
    elif kind == "thinking":
        print(f"{DIM}🧠 {_short(event['text'], 300)}{RESET}")
    elif kind == "text":
        pass  # the final reply is printed at the end
    elif kind == "tool_call":
        if event["tool"] not in ("make_plan", "log_adaptation"):
            print(f"{MAGENTA}🔧 {event['tool']}{RESET}{DIM}({_short(event['input'])}){RESET}")
    elif kind == "tool_result":
        if event["tool"] in ("make_plan", "log_adaptation"):
            return
        color = GREEN if event["ok"] else YELLOW
        mark = "📥" if event["ok"] else "⚠️ "
        print(f"   {color}{mark} {event['summary']}{RESET}")
        for problem in event.get("data", {}).get("problems", []):
            print(f"      {YELLOW}- {problem}{RESET}")
    elif kind == "adaptation":
        print(f"{YELLOW}{BOLD}🔄 ADAPT:{RESET}{YELLOW} {event['problem']} -> {event['change']}"
              f"{(' (' + event['reason'] + ')') if event['reason'] else ''}{RESET}")
    elif kind == "warning":
        print(f"{YELLOW}⚠️  {event['text']}{RESET}")
    elif kind == "error":
        print(f"{RED}❌ {event['text']}{RESET}")
    elif kind == "done":
        print(f"{DIM}✅ Done in {event['seconds']} s: {event['tool_calls']} tool calls, "
              f"{event['adaptations']} adaptation(s).{RESET}")


def format_plan(plan: dict) -> str:
    lines = [f"{BOLD}🍽️  {plan.get('headline') or 'Your plan'}{RESET}  ({plan['date']})"]
    for meal in plan["meals"]:
        t = meal["totals"]
        lines.append(f"\n{BOLD}{meal['meal'].title()} @ {meal['hall']}{RESET}  "
                     f"{DIM}{t.get('protein_g', 0)} g protein · {t.get('calories', 0)} kcal{RESET}")
        for item in meal["items"]:
            serv = f"{item['servings']:g}× " if item["servings"] != 1 else ""
            nut = ("nutrition not listed" if item["calories"] is None
                   else f"{item['protein_g']} g P · {item['calories']} kcal per serving")
            lines.append(f"  • {serv}{item['name']}  {DIM}({item['station']}; {nut}){RESET}")
        if meal.get("reason"):
            lines.append(f"    {DIM}↳ {meal['reason']}{RESET}")
    t = plan["totals"]
    lines.append(f"\n{BOLD}Totals:{RESET} {t['protein_g']} g protein · {t['calories']} kcal · "
                 f"{t['carbs_g']} g carbs · {t['fat_g']} g fat")
    if plan.get("protein_gap_g"):
        lines.append(f"{YELLOW}Short of the {plan['protein_goal_g']} g protein goal by "
                     f"{plan['protein_gap_g']} g.{RESET}")
    if plan.get("items_missing_nutrition"):
        lines.append(f"{DIM}No nutrition listed for: {', '.join(plan['items_missing_nutrition'])}{RESET}")
    for a in plan.get("adaptations", []):
        lines.append(f"{YELLOW}🔄 {a}{RESET}")
    for tip in plan.get("tips", []):
        lines.append(f"💡 {tip}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="DineWolfie: plan your day at SBU dining.")
    parser.add_argument("goal", nargs="*", help="What you're aiming for today (default: from prefs.json)")
    parser.add_argument("--date", default="today", help="YYYY-MM-DD, today or tomorrow")
    parser.add_argument("--simulate", default="", help="missing:West:dinner and/or offline (comma separated)")
    parser.add_argument("--sample", action="store_true", help="use the committed sample menus (offline)")
    args = parser.parse_args()

    if args.sample:
        config.DATA_MODE = "sample"
    if args.simulate:
        os.environ["DINEWOLFIE_SIMULATE"] = args.simulate
        T._load_simulation_from_env()
    goal = " ".join(args.goal) or default_goal()
    run = run_agent(goal, plan_date=T.resolve_date(args.date), on_event=print_event)
    if run.plan:
        print("\n" + format_plan(run.plan))
    if run.reply:
        print(f"\n{BOLD}💬 DineWolfie:{RESET} {run.reply}")
    return 0 if run.plan else 1


if __name__ == "__main__":
    raise SystemExit(main())
