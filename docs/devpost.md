# Devpost writeup (draft)

> Copy each section into the matching Devpost field. Lines in [brackets] are for you to fill in with your own words; judges can tell when a story is real.

## Tagline (one line)

An AI agent that plans your whole day of eating at Stony Brook's East and West dining halls, around your goals, using real SBU dining menu data.

## Overview

DineWolfie is an AI agent for Stony Brook students who eat at East Side and West Side Dine-In. You tell it a goal in plain English, like "I'm vegetarian, aiming for 120 g protein, and I'll be near West at lunch." It reads your saved preferences, pulls the real menus for both halls, compares them, does the nutrition math in code, works around anything that's missing, and hands you breakfast, lunch and dinner: which hall, which items, and why. Every morning it can also run on its own and push the plan to your phone.

## Problem

Checking the dining menu properly means opening Nutrislice, picking a hall, picking a station, and scrolling, then doing it again for the other hall and for every meal. East and West each have 15+ stations and 130 to 160 items a day. If you have a protein target, a diet, or an allergy, you also have to tap into items one by one to read the nutrition. Almost nobody does that every day, so most students eat whatever is closest and hope it fits.

## Solution

* **Goal in, plan out.** Type what you want; get a full day: meal, hall, items, a reason for each pick, and totals.
* **Both halls, every station.** It checks East and West and picks the better hall for each meal.
* **Nutrition-aware.** Real per-serving numbers from SBU's menus. All math is done in code, so numbers are never made up. Items with no nutrition data are labeled, not guessed.
* **Safe by construction.** A checker rejects any item that isn't on that hall's menu that day, or that conflicts with your allergies or diet. The agent must fix the plan and resubmit.
* **Remembers you.** Diet, allergies, goals, dislikes and favorite hall live in memory, plus your recent plans so it doesn't repeat meals. Tell it "remember I'm allergic to peanuts" and it updates its memory.
* **Adapts.** No dinner posted at West? It switches halls. Filters leave nothing at one station? It checks others. Protein target out of reach? It builds the closest plan, tells you exactly how many grams short, and suggests a fix. Network down? It uses the cached menu and says so.
* **Runs on its own.** Every morning it plans your day and sends it to your phone.
* **Feels human.** Every meal comes with a swap ("if the Grill line is long"). It knows your favorites, keeps a never-eat blacklist, and works in a treat now and then. Cheat days are a setting. Tap "Love it" or "Never again" and it remembers.

## Why it's agentic

DineWolfie follows the loop **goal → decide → use tools → observe → finish**, and you can watch every step live in the app:

1. **Goal:** your sentence, plus your saved memory (`get_prefs`, `get_meal_history`).
2. **Decide / plan:** it writes a short step list first (`make_plan`) and rewrites it if it changes course.
3. **Use tools:** 11 tools that hit real data: `get_menu`, `search_items`, `compare_halls`, `get_item_details`, `sum_nutrition`, `update_prefs`, `log_adaptation`, `submit_plan`, and more.
4. **Observe:** every tool returns a status (`ok`, `no_menu_posted`, `no_matches`, `error`, `rejected`) that the agent has to react to.
5. **Adapt:** when something fails it changes course and logs why. The app highlights these moments.
6. **Finish:** `submit_plan` verifies the plan against the real menu before it counts as done.

Plus **memory** that persists across runs and **autonomy**: the scheduled morning run has no human in the loop.

## How I built it / Tools used

* **Claude Agent SDK (Python)** runs the agent loop, with my tools served from an in-process MCP server. It runs on my Claude plan.
* **Python** for the data pipeline, tools, plan checker and nutrition math.
* **SBU's Nutrislice menu data.** Permission from Campus Dining has been requested; until it is granted, live fetching is off and DineWolfie runs on saved menus.
* **Streamlit** for the web app, including the live "agent at work" ticket rail.
* **ntfy** for push notifications to my iPhone.
* **Windows Task Scheduler** for the 8 AM run.
* **pytest** for 45 offline tests (parser, tools, checker, cache, notifications).
* **GitHub** for the code.

## Why I built it

[Your story, 3 to 5 sentences. For example: which hall you usually eat at, what goal you have (protein, a diet, an allergy), what you did before (scroll the app, guess, give up), and the moment you thought "an agent should do this."]

## Impact

Every student on a meal plan who eats at East or West faces this choice two or three times a day. DineWolfie turns 10+ minutes of scrolling and tapping into one sentence, or into zero effort with the morning push. It matters most for students with allergies or dietary rules, where a wrong pick has real consequences, and for students with fitness goals, who otherwise give up on tracking.

## Challenges and what I learned

* **The data wasn't shaped like I expected.** I assumed menus were split into breakfast, lunch and dinner. SBU's are split by station, and the meal is only in each section's header ("Grill Dinner Specials"). I had to infer meals from headers and station types and merge items that appear in several sections.
* **Missing data is normal.** About 6% of items have no nutrition info. I had to make "unknown" a first-class answer instead of a zero.
* **Letting the model do math is a bad idea.** I moved every calculation into Python and added a checker so the agent can't submit an item that isn't really on the menu.
* **Being polite to someone else's server.** Caching, rate limiting and stopping on errors were part of the design from day one.
* [Something you personally learned: first agent, first API, Git, etc.]

## What's next

* More SBU locations (Roth, SAC, Jasmine), with prices and meal-swipe vs dining-dollar awareness.
* Calendar-aware planning: route lunch to the hall nearest your 12:50 class.
* Group plans: find the hall that works for a whole friend group.
* A week view with variety and budget across days.

## Try it out

* GitHub: https://github.com/FrozenCodeZ/dinewolfie
* Setup and run instructions are in the README. `python -m src.cli --sample "plan my day"` runs on saved real menus, without network access to Nutrislice.

## Gallery captions (files in docs/screenshots/)

1. `1-goal.png`: **Tell it your goal.** One sentence, or tap an example like "Cheat day" or "Comfort food".
2. `2-agent-at-work.png`: **The agent at work.** Every tool call, result and decision appears live on the ticket rail.
3. `3-adapts.png`: **It adapts.** West hadn't posted dinner, so DineWolfie moved dinner to East and said why (amber ticket).
4. `4-plan-tray.png`: **Your day on a tray.** Hall, items, reasons, a swap for every meal, a treat, and real nutrition totals.
5. `5-how-it-works.png`: **How it works.** Goal → agent loop → real tools → verified plan → phone.
6. `6-browse-menus.png`: **Real data.** The same Nutrislice menus the agent reads, with nutrition and allergens.
7. `7-teach-and-memory.png`: **It learns you.** "Love it" and "Never again" buttons, plus the memory panel.
8. (take on your phone) **It runs while you sleep.** The 8 AM plan on your iPhone lock screen.
