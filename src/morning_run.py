"""The scheduled morning run: plan today with no human in the loop, then push it to your phone.

    python -m src.morning_run            # run once (skips if today's push already went out)
    python -m src.morning_run --dry-run  # print the notification instead of sending it
    python -m src.morning_run --force    # run even if today's plan was already sent

Windows Task Scheduler runs this every morning (see scripts/install_morning_task.ps1).
Each run writes a log to data/logs/ so you can see what the agent did while you slept.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

import config
from src import notify
from src.agent import default_goal, run_agent
from src.tools import read_prefs

LOG_DIR = config.DATA_DIR / "logs"
SENT_MARKER = config.DATA_DIR / "last_push.txt"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    today = date.today().isoformat()
    if not args.force and not args.dry_run and SENT_MARKER.exists() \
            and SENT_MARKER.read_text().strip() == today:
        print(f"Already sent today's plan ({today}). Use --force to send again.")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"morning-{today}.jsonl"
    with log_path.open("a", encoding="utf-8") as log:
        def on_event(event: dict) -> None:
            log.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **event},
                                 default=str) + "\n")
            log.flush()

        goal = default_goal(read_prefs())
        run = run_agent(goal, on_event=on_event)

    if run.plan:
        title, body = notify.plan_message(run.plan)
        if args.dry_run:
            print(f"{title}\n{body}")
        else:
            notify.send(title, body)
            SENT_MARKER.write_text(today)
            print(f"Sent: {title}\n{body}")
        return 0

    reason = run.error or "The agent couldn't build a plan from today's menus."
    if args.dry_run:
        print(f"[no plan] {reason}")
    else:
        try:
            notify.send_failure(f"{reason}\nOpen the app to try again.")
        except Exception as exc:  # no topic or no network: nothing else we can do
            print(f"Couldn't send the failure notice either: {exc}", file=sys.stderr)
    print(f"No plan: {reason}  (log: {log_path})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
