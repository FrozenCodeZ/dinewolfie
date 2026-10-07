# Devpost submission: copy-paste fields

Everything Devpost asks for, ready to paste. Lines in [brackets] are for you to write in your own words.

---

## Source code URL

```
https://github.com/FrozenCodeZ/dinewolfie
```

---

## Built with (tags)

Type each one into the "Built with" box and press Enter:

```
python
claude
anthropic
claude-agent-sdk
model-context-protocol
streamlit
nutrislice
ntfy
pytest
git
github
windows-task-scheduler
```

---

## How to run your project

```markdown
DineWolfie runs locally with your own Claude login (the agent uses the Claude Agent SDK, which signs in through Claude Code).

**Requirements:** Python 3.11+, Git, and Claude Code signed in to a Claude plan (Pro or Max).

**1. Install and sign in to Claude Code** (once)
- Mac: `curl -fsSL https://claude.ai/install.sh | bash`, then `claude auth login`
- Windows: install from https://code.claude.com, then `claude auth login` in PowerShell

**2. Get the code**
    git clone https://github.com/FrozenCodeZ/dinewolfie.git
    cd dinewolfie

**3. Set up Python**
- Mac: `python3 -m venv .venv && source .venv/bin/activate`
- Windows: `python -m venv .venv` then `.venv\Scripts\Activate.ps1`

Then: `pip install -r requirements.txt` and copy `.env.example` to `.env`.

**4. Run it**
- Web app: `streamlit run app.py`, then open http://localhost:8501, type a goal, press **Plan my day**
- Terminal: `python -m src.cli "I'm vegetarian, aiming for 120 g protein, near West at lunch"`
- See it adapt: turn on **Demo: break things on purpose → West dinner not posted** in the sidebar, or run `python -m src.cli --simulate missing:West:dinner "plan my day"`
- Tests (offline, no Claude calls): `python -m pytest`

By default DineWolfie uses real SBU menus saved in the repo (`data/sample/`), so it works without contacting Nutrislice. Full details are in the README.
```

---

## About the project (Markdown)

```markdown
## Inspiration
Every day, Stony Brook students open the dining menu, scroll East, scroll West, and tap into items one by one to check protein or allergens, then do it again for lunch and dinner. East and West each have 15+ stations and 130 to 160 items a day, so most of us give up and eat whatever is closest. [Add 1 to 2 sentences of your own: which hall you eat at, what goal or allergy you have.]

## What it does
**DineWolfie** is an AI agent that plans your whole day of eating at East and West around your goals. Tell it something like *"I'm vegetarian, aiming for 120 g protein, and I'll be near West at lunch."* It:

- **Remembers you:** diet, allergies, goals, a never-eat list, favorites, treats and cheat days, plus your recent plans so it doesn't repeat meals
- **Plans**, then **uses tools** on real SBU Nutrislice menu data: opens menus, searches, and compares the two halls
- **Does the math in code**, so nutrition numbers are never made up; items with no data are labeled, not guessed
- **Adapts** when something goes wrong (no dinner posted at West → moves dinner to East and says why; protein goal out of reach → tells you exactly how many grams short and how to fix it)
- **Checks itself:** a plan checker rejects any item that isn't really on that hall's menu, or that breaks your allergies, diet or never-eat list; the agent fixes it and resubmits
- **Feels human:** a swap for every meal ("if the Grill line is long"), a treat now and then, and one-tap **Love it** / **Never again** buttons that update its memory
- **Runs on its own:** every morning at 8 it plans your day and pushes it to your phone

You can watch every step live: each tool call, result and change of course lands on a "ticket rail" next to the finished plan.

## Why it's agentic
It follows the loop **goal → decide → use tools → observe → finish**:
1. **Goal:** your sentence plus saved memory (`get_prefs`, `get_meal_history`)
2. **Decide:** it writes a step list first (`make_plan`) and rewrites it when it changes course
3. **Use tools:** 11 tools, including `get_menu`, `search_items`, `compare_halls`, `sum_nutrition`, `update_prefs`, `log_adaptation`, `submit_plan`
4. **Observe:** every tool returns a status (`ok`, `no_menu_posted`, `no_matches`, `error`, `rejected`) that it must react to
5. **Finish:** `submit_plan` verifies the plan against the real menu before it counts

Plus **memory** that persists across runs and **autonomy** in the scheduled morning run.

## How we built it
- **Claude Agent SDK (Python)** runs the agent loop; our 11 tools are plain Python functions served from an in-process MCP server
- **Python** for the menu parser, tools, plan checker and nutrition math
- **SBU's Nutrislice menu data**, parsed from its JSON API into a clean schema (meal, station, nutrition, allergens, diet tags)
- **Streamlit** for the web app and live agent trace
- **ntfy** for iPhone push notifications, **Windows Task Scheduler** for the 8 AM run
- **pytest:** 45 offline tests covering the parser, tools, plan checker, cache rules and notifications

## Challenges we ran into
- **The data wasn't shaped like we expected.** SBU's menus are split by station, not by meal; the meal only appears in section headers like "Grill Dinner Specials". We infer meals from headers and station types and merge items that show up in several sections.
- **Missing data is normal.** About 6% of items have no nutrition info, so "unknown" had to be a real answer, not a zero.
- **Models shouldn't do arithmetic.** We moved every calculation into code and added a checker so the agent can't submit an item that isn't on the menu.
- **Being a good guest.** We asked SBU Campus Dining for permission to fetch menus automatically. Until they answer, live fetching is off and DineWolfie runs on saved real menus; when it's on, it caches, rate-limits, and stops at the first refusal.

## Accomplishments that we're proud of
- The agent **caught a bug in our own code** during testing: a tool rejected its input, and it logged the problem as an adaptation and told us instead of pretending it worked.
- Every number on screen is computed, and every item is verified against the real menu.
- [Add your own: first agent you built, first deployed app, etc.]

## What we learned
[Your own words: e.g. how agent loops and tool calling work, why you keep math out of the model, Git and GitHub, working with real messy data.]

## What's next for DineWolfie
- More SBU locations (Roth, SAC, Jasmine), with prices and meal-swipe vs dining-dollar awareness
- Accounts so everyone has their own memory on the hosted web app
- Calendar-aware planning: send lunch to the hall nearest your 12:50 class
- Group plans and a week view with variety across days
```

---

## Notes before you submit

- **Screenshots:** `docs/screenshots/` has 7 captioned shots (captions in `docs/devpost.md`). Add an 8th from your phone: the 8 AM notification.
- **Hosted link:** don't list the Streamlit Cloud URL as "try it out" until the agent works there. Right now it shows "Claude Code isn't signed in", because the cloud server has no Claude login.
