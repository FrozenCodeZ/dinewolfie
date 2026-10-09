# DineWolfie 🐺

**An AI agent that plans your day of eating at Stony Brook's East Side and West Side dining halls around your goals, so you never have to scroll the menu app again.**

Tell it something like *"I'm vegetarian, aiming for 120 g protein today, and I'll be near West at lunch."* DineWolfie reads your saved preferences, pulls today's real menus for both halls, compares them, adds up the nutrition in code, works around anything that's missing, and hands you breakfast, lunch and dinner: which hall, which items, and why. Every morning it can also do this on its own and push the plan to your phone.

![DineWolfie planning a day: live agent steps on the left, the plan tray on the right](docs/screenshots/4-plan-tray.png)

| The agent changes course when West hasn't posted dinner | How it works |
|---|---|
| ![Adaptation](docs/screenshots/3-adapts.png) | ![Architecture](docs/architecture.svg) |

Built for the AI Community @ SBU Internal Competition 2026 (Agentic AI).

---

## Why it's an agent, not a chatbot

| Agentic quality | What DineWolfie does |
|---|---|
| **Goal-directed** | Works toward your goal for the whole day (protein, calories, diet, location), not one question. |
| **Plans** | Writes a step list (`make_plan`) before acting and rewrites it when it changes course. |
| **Uses real tools** | 11 tools: real SBU Nutrislice menus (saved copies), search, hall comparison, nutrition math, memory, plan checker. |
| **Observes** | Every tool returns a `status` (`ok`, `no_menu_posted`, `no_matches`, `error`, `rejected`) that it must read. |
| **Adapts** | Missing menu → other hall. Filter leaves nothing → other station. Goal unreachable → closest plan plus the exact gap and a fix. Network down → cached menus. Each change is logged (`log_adaptation`) and shown highlighted. |
| **Verifies itself** | `submit_plan` rejects any item that isn't on that hall's menu that day or that breaks your allergies/diet. The agent has to fix the plan and resubmit. |
| **Remembers** | `prefs.json` (diet, allergies, goals, never-eat list, favorites, treats, cheat days) and `history.json` (past plans, for variety). It updates memory when you state a lasting preference, or when you tap **Love it** / **Never again**. |
| **Autonomous** | A scheduled morning run plans your day and pushes it to your phone with no human in the loop. |

## Feels like a friend, not a calculator

* **Swaps for every meal.** Each meal comes with an alternative with similar protein ("if the Grill line is long"), sometimes at the other hall. Swaps go through the same checker.
* **Treats and cheat days.** Set treats to never / sometimes / often and pick cheat days. When it fits, DineWolfie adds a treat (tagged on the tray). On a cheat day it relaxes calories but never your allergies, diet or blacklist.
* **Never-eat blacklist vs. dislikes.** Dislikes are avoided when possible; never-eat foods are blocked as strictly as allergies.
* **Favorites.** If something you love is on the menu, it's picked first. If it isn't, the agent tells you and finds a stand-in.
* **One-tap feedback.** Under each plan: **Love it** adds to favorites, **Never again** blacklists the item.
* **Follow-ups.** "Make dinner lighter" or "remember I'm allergic to soy": the agent continues the same conversation, updates memory if needed, and re-plans.

The model decides; the code calculates. Every number you see comes from Python, never from the model, so it can't make up nutrition facts.

---

## Requirements

* Windows 11 or macOS, **Python 3.11+** (tested on 3.14)
* **Claude Code** installed and logged in with a Claude plan (Pro/Max). The agent runs on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), which uses your Claude Code login. Usage counts against your plan's limits.
* Optional: the free **ntfy** app on your phone for the morning push.

> Don't set `ANTHROPIC_API_KEY` unless you mean to: an API key takes priority over your plan login and bills your API account instead.

## Setup (Windows PowerShell)

```powershell
git clone https://github.com/FrozenCodeZ/dinewolfie.git
cd dinewolfie
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

### Setup on a Mac (Terminal)

macOS's built-in `python3` is often 3.9, which is too old. Check with `python3 --version`; if it's below 3.11, install Python from https://www.python.org/downloads/ (or `brew install python@3.13`).

```bash
curl -fsSL https://claude.ai/install.sh | bash      # installs Claude Code
claude auth login                                    # sign in with your Claude plan
git clone https://github.com/FrozenCodeZ/dinewolfie.git
cd dinewolfie
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
streamlit run app.py
```

The 8 AM scheduler script is Windows-only. On a Mac, send a plan to your phone on demand with `python -m src.morning_run --force`.

If PowerShell refuses to run `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

Log Claude Code in once (opens a browser; choose your Claude subscription):

```powershell
claude auth login
```

If `claude` isn't found, install Claude Code from https://code.claude.com, or run `& "$env:USERPROFILE\.local\bin\claude.exe" auth login`.

Your memory lives in `prefs.json`, which is created the first time you save it in the sidebar and is never committed (it is in `.gitignore`). To start from an example, copy `prefs.example.json` to `prefs.json`; its values are made up.

## Run it

**Web app** (the live "agent at work" view):

```powershell
streamlit run app.py
```

Opens http://localhost:8501. Type a goal, press **Plan my day**, and watch each step land on the ticket rail.

**Terminal:**

```powershell
python -m src.cli "I'm vegetarian, aiming for 120 g protein, near West at lunch"
python -m src.cli --date tomorrow "high protein, under 2000 kcal"
```

**Show it adapting** (simulated failures; the agent isn't told they're simulated):

```powershell
python -m src.cli --simulate missing:West:dinner "plan my day, I prefer West"
python -m src.cli --simulate offline "plan my day"
```

In the web app the same switches are in the sidebar under **Demo: break things on purpose**.

**Menu data mode:** by default (`DINEWOLFIE_DATA_MODE=sample`, or `--sample`) DineWolfie uses the real menus saved in `data/sample/`, so it works without network access to Nutrislice. Contacting Nutrislice is switched off (see Data below).

**Tests** (offline, no Claude calls):

```powershell
python -m pytest
```

## Phone notifications (morning push)

1. Install **ntfy** on your iPhone (App Store) and tap **+** to subscribe to a topic. Make the name long and random, like `dinewolfie-7f3k9q2m`. Anyone who knows it can read your messages.
2. Put the same name in `.env`: `NTFY_TOPIC=dinewolfie-7f3k9q2m`
3. Send a test: `python -m src.notify`
4. Try the morning run without sending: `python -m src.morning_run --dry-run`
5. Schedule it for 8:00 every day:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_morning_task.ps1
Start-ScheduledTask -TaskName "DineWolfie Morning Plan"    # test it now
```

The PC has to be on (or asleep with wake timers allowed) at that time; if it was off, the task runs as soon as it's back. Each run logs every agent step to `data/logs/`. Remove the task with `scripts\uninstall_morning_task.ps1`.

## How it works

```
You ──goal──▶ Claude (Agent SDK loop) ──tools──▶ Nutrislice menus (cached) · memory · nutrition math
                    ▲          │
                    └─observe──┘   adapt when something fails
                               │
                         submit_plan ──checker──▶ plan tray (web) · phone push (ntfy)
```

| File | What it does |
|---|---|
| `config.py` | Nutrislice URLs, hall ids, meal rules, allergen/tag mapping, settings from `.env` |
| `src/fetch_menu.py` | Downloads one station-week at a time, caches to `data/menus/`, falls back to cache on failure |
| `src/parse_menu.py` | Raw Nutrislice JSON → clean items (name, station, meals, nutrition, allergens, tags) |
| `src/tools.py` | The agent's tools as plain, tested Python functions |
| `src/agent.py` | System prompt + tools wired into the Claude Agent SDK; streams every step as an event |
| `app.py` | Streamlit UI: goal, live ticket rail, plan tray, memory editor, menu browser, history |
| `src/cli.py` | Terminal mode with the same live trace |
| `src/morning_run.py`, `src/notify.py` | Scheduled run and ntfy push |
| `docs/data-notes.md` | The real Nutrislice endpoints and field names |

## Hosted website

The public site can't use your Claude login, so it lets you pick **Claude** (Anthropic API key) or **Groq** (free tier) in the sidebar. Keys come from the visitor or from your Streamlit secrets (only signed-in users can use yours). People **sign in with Google**, and each person's memory is saved to a **Google Sheet**. Guests get memory that lasts only for their browser tab. An optional **Tavily** key lets the agent look up nutrition for items the menu leaves blank; those numbers are labeled as web estimates and never counted in totals.

Step-by-step setup: [docs/hosting.md](docs/hosting.md). All settings: [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example).

In the terminal, `DINEWOLFIE_ENGINE=groq` with `GROQ_API_KEY` in `.env` runs the CLI and the morning run on Groq.

## Data

Menu data comes from Stony Brook University's Nutrislice menus (`stonybrook.api.nutrislice.com`). We have asked SBU Campus Dining for permission to fetch them automatically and have not heard back yet, so **live fetching is off by default** (`DINEWOLFIE_LIVE_FETCH=off`): DineWolfie never contacts Nutrislice and runs on saved menus. If permission is granted and it is switched on, DineWolfie is polite about it: one request covers a whole week for one station, results are cached on disk, a station is fetched at most once a day, requests are spaced out and identify the project, and if the server refuses or rate-limits any request, all fetching stops at once instead of retrying. Nutrition values are Nutrislice's per-serving numbers. When an item has none, DineWolfie says "nutrition not listed" instead of guessing.

## Notes

* Anyone running DineWolfie needs their own Claude login (or an API key).
* DineWolfie isn't medical or dietary advice. Always confirm allergens with dining staff.
