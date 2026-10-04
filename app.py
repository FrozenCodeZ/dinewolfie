"""DineWolfie web app.   Run:  streamlit run app.py

Left: your goal and the agent's live work, one ticket per step.
Right: the finished plan, laid out like a dining-hall tray.
"""
from __future__ import annotations

import html
import json
from datetime import date, timedelta

import streamlit as st

import config
from src import notify
from src import tools as T
from src.agent import default_goal, run_agent

st.set_page_config(page_title="DineWolfie", page_icon="🐺", layout="wide")

# ---------------------------------------------------------------------------------------
# Look and feel
# ---------------------------------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Atkinson+Hyperlegible:wght@400;700&family=JetBrains+Mono:wght@500&display=swap');
:root {
  --red: #990000; --navy: #1D3557; --ink: #1F2430; --muted: #5B6472;
  --steel: #EEF1F4; --tray: #D5DCE4; --well: #F8FAFC; --paper: #FFFFFF;
  --green: #2E7D4F; --amber: #9A6200; --amber-bg: #FFF3D1; --amber-line: #E0A100;
}
html, body, .stMarkdown, p, li, label, input, textarea, button, [data-testid="stWidgetLabel"] {
  font-family: 'Atkinson Hyperlegible', system-ui, sans-serif;
}
[data-testid="stIconMaterial"], .material-symbols-rounded { font-family: 'Material Symbols Rounded' !important; }
h1, h2, h3, .dw-word { font-family: 'Bricolage Grotesque', sans-serif; letter-spacing: -0.01em; }
.block-container { padding-top: 3.2rem; max-width: 1500px; }

.dw-word { font-size: 3rem; font-weight: 800; color: var(--ink); line-height: 1; margin: 0; }
.dw-word span { color: var(--red); }
.dw-tag { color: var(--muted); font-size: 1.05rem; margin: .4rem 0 1.2rem; max-width: 60ch; }

/* ticket rail: the agent's live work */
.rail { position: relative; padding-left: 22px; margin-top: .4rem; }
.rail::before { content: ""; position: absolute; left: 7px; top: 4px; bottom: 4px; width: 3px;
  background: repeating-linear-gradient(var(--tray) 0 6px, transparent 6px 10px); border-radius: 2px; }
.tk { position: relative; background: var(--paper); border-radius: 4px 4px 10px 10px;
  border-top: 2px dashed var(--tray); padding: .55rem .8rem .6rem; margin: 0 0 .55rem;
  box-shadow: 0 1px 0 rgba(31,36,48,.08), 0 6px 14px -10px rgba(31,36,48,.35); }
.tk::before { content: ""; position: absolute; left: -19px; top: .85rem; width: 9px; height: 9px;
  border-radius: 50%; background: var(--paper); border: 2px solid var(--muted); }
.tk-head { display: flex; justify-content: space-between; gap: .6rem; align-items: baseline; }
.tk-name { font-weight: 700; color: var(--ink); }
.tk-tool { font-family: 'JetBrains Mono', monospace; font-size: .74rem; color: var(--muted); }
.tk-time { font-family: 'JetBrains Mono', monospace; font-size: .72rem; color: var(--muted); white-space: nowrap; }
.tk-in { font-size: .82rem; color: var(--muted); margin-top: .15rem; overflow-wrap: anywhere; }
.tk-out { font-size: .9rem; margin-top: .3rem; color: var(--ink); }
.tk-out.ok::before { content: "✓ "; color: var(--green); font-weight: 700; }
.tk-out.warn::before { content: "! "; color: var(--amber); font-weight: 700; }
.tk-out.wait { color: var(--muted); font-style: italic; }
.tk ul { margin: .3rem 0 0 1.1rem; padding: 0; font-size: .88rem; }
.tk-goal { border-top-color: var(--red); }
.tk-goal::before { border-color: var(--red); background: var(--red); }
.tk-plan::before { border-color: var(--navy); }
.tk-adapt { background: var(--amber-bg); border-top: 2px solid var(--amber-line); }
.tk-adapt::before { border-color: var(--amber-line); background: var(--amber-line); }
.tk-adapt .tk-name { color: var(--amber); }
.tk-error { background: #FDECEC; border-top: 2px solid var(--red); }
.tk-error::before { border-color: var(--red); }
.tk-think { background: transparent; box-shadow: none; border-top: none; padding: .1rem .2rem .4rem;
  color: var(--muted); font-size: .86rem; font-style: italic; }
.tk-think::before { width: 7px; height: 7px; left: -18px; top: .45rem; border-color: var(--tray); }
.tk-done { background: transparent; box-shadow: none; border-top: none; color: var(--muted);
  font-size: .85rem; padding: .1rem .2rem; }
.tk-done::before { border-color: var(--green); background: var(--green); }
.chip { display: inline-block; font-size: .78rem; padding: .05rem .45rem; border-radius: 999px;
  background: var(--steel); color: var(--ink); margin: .15rem .2rem 0 0; }

/* the tray: the finished plan */
.tray { background: var(--tray); border-radius: 22px; padding: 14px; box-shadow: inset 0 2px 0 rgba(255,255,255,.6);
  container-type: inline-size; }
.tray-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap;
  gap: .4rem; padding: .2rem .5rem .7rem; }
.tray-title { margin: 0; font-family: 'Bricolage Grotesque', sans-serif; font-weight: 800;
  font-size: 1.35rem; color: var(--ink); }
.tray-date { color: var(--muted); font-size: .9rem; }
.wells { display: grid; grid-template-columns: 1fr; gap: 12px; }
@container (min-width: 700px) { .wells { grid-template-columns: repeat(var(--n, 3), 1fr); } }
.well { background: var(--well); border-radius: 14px; padding: .8rem .9rem .9rem;
  box-shadow: inset 0 2px 6px rgba(31,36,48,.12); }
.well-title { margin: 0 0 .35rem; font-family: 'Bricolage Grotesque', sans-serif; font-weight: 800;
  font-size: 1.15rem; color: var(--ink); display: flex; justify-content: space-between; align-items: center; }
.hall { font-family: 'Atkinson Hyperlegible', sans-serif; font-size: .78rem; font-weight: 700;
  color: #fff; border-radius: 999px; padding: .1rem .6rem; }
.hall-East { background: var(--navy); } .hall-West { background: var(--red); }
.food { margin: .45rem 0; }
.food-name { font-weight: 700; color: var(--ink); line-height: 1.25; }
.food-meta { font-size: .8rem; color: var(--muted); }
.badge { font-size: .74rem; font-weight: 700; border-radius: 999px; padding: 0 .45rem; margin-left: .3rem;
  vertical-align: 1px; white-space: nowrap; }
.badge-treat { background: #FBE3EC; color: #8A1C44; }
.badge-fav { background: #E3F1E8; color: var(--green); }
.badge-cheat { background: #FBE3EC; color: #8A1C44; font-size: .82rem; padding: .1rem .6rem; }
.swap { font-size: .82rem; color: var(--ink); background: var(--steel); border-radius: 10px;
  padding: .4rem .55rem; margin-top: .45rem; }
.swap b { font-weight: 700; }
.swap .food-meta { margin-top: .1rem; }
.why { font-size: .85rem; color: var(--ink); border-top: 1px dashed var(--tray); margin-top: .55rem;
  padding-top: .45rem; }
.meal-tot { font-size: .8rem; color: var(--muted); margin-top: .35rem; }
.totals { background: var(--well); border-radius: 14px; margin-top: 12px; padding: .8rem 1rem; }
.bar { height: 12px; background: var(--tray); border-radius: 999px; overflow: hidden; margin: .4rem 0; }
.bar > div { height: 100%; background: var(--green); border-radius: 999px; }
.bar.short > div { background: var(--amber-line); }
.tot-line { display: flex; flex-wrap: wrap; gap: 1.2rem; font-size: .92rem; color: var(--ink); }
.tot-line b { font-family: 'Bricolage Grotesque', sans-serif; font-size: 1.2rem; }
.note { font-size: .88rem; margin-top: .45rem; }
.note-adapt { color: var(--amber); }
.empty { border: 2px dashed var(--tray); border-radius: 22px; padding: 2.2rem 1.4rem; color: var(--muted);
  text-align: center; }
.st-key-reply { background: var(--paper); border-radius: 14px; padding: .8rem 1rem .2rem;
  margin-top: 12px; border-left: 4px solid var(--red); }
@media (prefers-reduced-motion: no-preference) {
  .tk-new { animation: drop .35s ease-out; }
  @keyframes drop { from { transform: translateY(-6px); opacity: 0; } to { transform: none; opacity: 1; } }
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

TOOL_LABELS = {
    "get_prefs": "Read your memory",
    "update_prefs": "Updated your memory",
    "get_meal_history": "Checked recent plans",
    "get_menu": "Opened a menu",
    "search_items": "Searched the menus",
    "compare_halls": "Compared East and West",
    "get_item_details": "Read item details",
    "sum_nutrition": "Added up nutrition",
    "submit_plan": "Sent the plan to the checker",
}
MEAL_LABEL = {"breakfast": "Breakfast", "brunch": "Brunch", "lunch": "Lunch", "dinner": "Dinner",
              "late_night": "Late night"}

for key, default in {"events": [], "plan": None, "reply": "", "session_id": None, "error": None,
                     "stats": None, "goal": ""}.items():
    st.session_state.setdefault(key, default)

e = html.escape


# ---------------------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------------------

def _fmt_input(args: dict) -> str:
    parts = []
    for k, v in args.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, list):
            if v and isinstance(v[0], dict):
                v = f"{len(v)} item(s)"
            else:
                v = ", ".join(map(str, v))
        elif isinstance(v, dict):
            v = json.dumps(v)
        parts.append(f"{k}: {v}")
    text = "; ".join(parts)
    return text if len(text) < 220 else text[:217] + "..."


def rail_html(events: list[dict], live: bool) -> str:
    if not events:
        return ""
    t0 = events[0].get("t", 0)
    tickets, pending = [], {}
    final_text = st.session_state.get("reply", "")
    for ev in events:
        kind, dt = ev["type"], f"+{ev.get('t', t0) - t0:.1f}s"
        if kind == "goal":
            label = "Follow-up" if ev.get("followup") else "Goal"
            tickets.append(f'<div class="tk tk-goal"><div class="tk-head"><span class="tk-name">{label}'
                           f'</span><span class="tk-time">{e(ev["date"])}</span></div>'
                           f'<div class="tk-out">{e(ev["text"])}</div></div>')
        elif kind == "plan":
            steps = "".join(f"<li>{e(s)}</li>" for s in ev["steps"])
            tickets.append(f'<div class="tk tk-plan"><div class="tk-head"><span class="tk-name">Made a plan'
                           f'</span><span class="tk-time">{dt}</span></div><ol style="margin:.3rem 0 0 1.1rem;'
                           f'padding:0;font-size:.88rem">{steps}</ol></div>')
        elif kind == "thinking" or (kind == "text" and ev["text"] != final_text):
            text = ev["text"] if len(ev["text"]) < 320 else ev["text"][:317] + "..."
            tickets.append(f'<div class="tk tk-think">{e(text)}</div>')
        elif kind == "tool_call":
            if ev["tool"] in ("make_plan", "log_adaptation"):
                continue
            idx = len(tickets)
            pending.setdefault(ev["tool"], []).append(idx)
            tickets.append({"tool": ev["tool"], "input": ev["input"], "dt": dt, "result": None})
        elif kind == "tool_result":
            if ev["tool"] in ("make_plan", "log_adaptation"):
                continue
            queue = pending.get(ev["tool"]) or []
            if queue:
                tickets[queue.pop(0)]["result"] = ev
        elif kind == "adaptation":
            why = f'<div class="tk-in">{e(ev["reason"])}</div>' if ev.get("reason") else ""
            tickets.append(f'<div class="tk tk-adapt"><div class="tk-head"><span class="tk-name">Changed course'
                           f'</span><span class="tk-time">{dt}</span></div><div class="tk-out"><b>'
                           f'{e(ev["problem"])}</b><br>{e(ev["change"])}</div>{why}</div>')
        elif kind in ("warning", "error"):
            tickets.append(f'<div class="tk tk-error"><div class="tk-out">{e(ev["text"])}</div></div>')
        elif kind == "done":
            tickets.append(f'<div class="tk tk-done">Finished in {ev["seconds"]} s with {ev["tool_calls"]} tool '
                           f'calls and {ev["adaptations"]} change(s) of course.</div>')

    out = []
    for i, t in enumerate(tickets):
        if isinstance(t, dict):
            res = t["result"]
            if res is None:
                body = '<div class="tk-out wait">working…</div>'
            else:
                cls = "ok" if res["ok"] else "warn"
                body = f'<div class="tk-out {cls}">{e(res["summary"])}</div>'
                for p in res.get("data", {}).get("problems", [])[:4]:
                    body += f'<div class="tk-in">• {e(p)}</div>'
            label = TOOL_LABELS.get(t["tool"], t["tool"])
            t = (f'<div class="tk"><div class="tk-head"><span class="tk-name">{e(label)} '
                 f'<span class="tk-tool">{e(t["tool"])}</span></span><span class="tk-time">{t["dt"]}</span>'
                 f'</div><div class="tk-in">{e(_fmt_input(t["input"]))}</div>{body}</div>')
        if live and i == len(tickets) - 1:
            t = t.replace('class="tk', 'class="tk-new tk', 1)
        out.append(t)
    return '<div class="rail">' + "".join(out) + "</div>"


def tray_html(plan: dict) -> str:
    day = date.fromisoformat(plan["date"])
    wells = []
    for meal in plan["meals"]:
        foods = []
        for item in meal["items"]:
            serv = f"{item['servings']:g}× " if item["servings"] != 1 else ""
            if item["calories"] is None:
                nut = "nutrition not listed"
            else:
                nut = (f"{round((item['protein_g'] or 0) * item['servings'])} g protein, "
                       f"{round(item['calories'] * item['servings'])} kcal")
            badges = ""
            if item.get("treat"):
                badges += '<span class="badge badge-treat">treat</span>'
            if item.get("favorite"):
                badges += '<span class="badge badge-fav">♥ favorite</span>'
            foods.append(f'<div class="food"><div class="food-name">{e(serv + item["name"])}{badges}</div>'
                         f'<div class="food-meta">{e(item["station"])} · {nut}</div></div>')
        swaps = []
        for alt in meal.get("alternatives") or []:
            names = " + ".join(e((f"{i['servings']:g}× " if i["servings"] != 1 else "") + i["name"])
                               for i in alt["items"])
            where = f" at {e(alt['hall'])}" if alt["hall"] != meal["hall"] else ""
            at = alt.get("totals") or {}
            swaps.append(f'<div class="swap">Or swap{where}: <b>{names}</b>'
                         f'<div class="food-meta">{e(alt.get("note", ""))} · {at.get("protein_g", 0)} g protein, '
                         f'{at.get("calories", 0)} kcal</div></div>')
        t = meal.get("totals") or {}
        wells.append(
            f'<div class="well"><div class="well-title">{e(MEAL_LABEL.get(meal["meal"], meal["meal"]))}'
            f'<span class="hall hall-{e(meal["hall"])}">{e(meal["hall"])}</span></div>{"".join(foods)}'
            f'<div class="meal-tot">{t.get("protein_g", 0)} g protein · {t.get("calories", 0)} kcal</div>'
            f'<div class="why">{e(meal.get("reason", ""))}</div>{"".join(swaps)}</div>')

    cheat = '<span class="badge badge-cheat">cheat day</span> ' if plan.get("cheat_day") else ""
    tot = plan["totals"]
    goal = plan.get("protein_goal_g")
    if goal:
        pct = min(100, round(tot["protein_g"] / goal * 100))
        short = " short" if plan.get("protein_gap_g") else ""
        bar = (f'<div class="bar{short}"><div style="width:{pct}%"></div></div>'
               f'<div class="food-meta">{tot["protein_g"]} of {goal} g protein goal</div>')
    else:
        bar = ""
    notes = []
    if plan.get("protein_gap_g"):
        notes.append(f'<div class="note note-adapt">{plan["protein_gap_g"]} g short of your protein goal.</div>')
    if plan.get("items_missing_nutrition"):
        notes.append(f'<div class="note">No nutrition listed for {e(", ".join(plan["items_missing_nutrition"]))},'
                     f' so real totals are a bit higher.</div>')
    for a in plan.get("adaptations", []):
        notes.append(f'<div class="note note-adapt">🔄 {e(a)}</div>')
    for tip in plan.get("tips", []):
        notes.append(f'<div class="note">💡 {e(tip)}</div>')
    return (f'<div class="tray"><div class="tray-head"><div class="tray-title">{e(plan.get("headline") or "Your day")}</div>'
            f'<span class="tray-date">{cheat}{day.strftime("%A, %B")} {day.day}</span></div>'
            f'<div class="wells" style="--n:{max(1, len(wells))}">{"".join(wells)}</div>'
            f'<div class="totals"><div class="tot-line"><span><b>{tot["protein_g"]}</b> g protein</span>'
            f'<span><b>{tot["calories"]}</b> kcal</span><span><b>{tot["carbs_g"]}</b> g carbs</span>'
            f'<span><b>{tot["fat_g"]}</b> g fat</span></div>{bar}{"".join(notes)}</div></div>')


# ---------------------------------------------------------------------------------------
# Sidebar: memory, data source, demo switches
# ---------------------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Your memory")
    st.caption("Saved in prefs.json. The agent reads it before every plan, and updates it when you "
               "tell it something lasting, like a new allergy.")
    prefs = T.read_prefs()
    with st.form("prefs"):
        name = st.text_input("Name", prefs.get("name") or "")
        diet = st.multiselect("Diet", ["vegetarian", "vegan", "halal", "pescatarian", "gluten_free"],
                              [d for d in prefs.get("diet", []) if d in
                               ["vegetarian", "vegan", "halal", "pescatarian", "gluten_free"]])
        allergy_opts = ["milk", "egg", "peanuts", "tree_nuts", "soy", "wheat", "gluten", "fish",
                        "shellfish", "sesame"]
        allergies = st.multiselect("Allergies (never served)", allergy_opts,
                                   [a for a in prefs.get("allergies", []) if a in allergy_opts])
        protein = st.number_input("Daily protein goal (g, 0 = none)", 0, 400,
                                  int(prefs.get("daily_protein_goal_g") or 0), step=5)
        calories = st.number_input("Daily calorie goal (0 = none)", 0, 6000,
                                   int(prefs.get("daily_calorie_goal") or 0), step=50)
        halls = ["No preference", "East", "West"]
        fav = st.selectbox("Favorite hall", halls, halls.index(prefs.get("favorite_hall") or "No preference"))
        never_eat = st.text_input("Never eat (blacklist)", ", ".join(prefs.get("never_eat") or []),
                                  help="Comma separated. Strict, like an allergy: the checker rejects any plan "
                                       "that includes these.")
        dislikes = st.text_input("Dislikes (avoid if possible)", ", ".join(prefs.get("dislikes", [])))
        favorites = st.text_input("Favorites", ", ".join(prefs.get("favorites") or []),
                                  help="Comma separated. Picked first when they're on the menu.")
        treats = st.select_slider("Treats", options=list(T.TREAT_LEVELS),
                                  value=prefs.get("treats") if prefs.get("treats") in T.TREAT_LEVELS
                                  else "sometimes")
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        cheat_days = st.multiselect("Cheat days", weekdays,
                                    [d for d in prefs.get("cheat_days") or [] if d in weekdays])
        meals = st.multiselect("Meals to plan", ["breakfast", "lunch", "dinner", "late_night"],
                               prefs.get("meals_to_plan") or ["breakfast", "lunch", "dinner"])
        if st.form_submit_button("Save memory"):
            prefs.update({"name": name, "diet": diet, "allergies": allergies,
                          "daily_protein_goal_g": protein or None, "daily_calorie_goal": calories or None,
                          "favorite_hall": None if fav == "No preference" else fav,
                          "dislikes": [d.strip() for d in dislikes.split(",") if d.strip()],
                          "never_eat": [d.strip() for d in never_eat.split(",") if d.strip()],
                          "favorites": [d.strip() for d in favorites.split(",") if d.strip()],
                          "treats": treats, "cheat_days": cheat_days,
                          "meals_to_plan": meals or ["breakfast", "lunch", "dinner"]})
            T.write_prefs(prefs)
            st.toast("Memory saved")
    if prefs.get("notes"):
        st.caption("Notes the agent saved: " + "; ".join(map(str, prefs["notes"])))

    st.markdown("### Menu data")
    mode = st.radio("Source", ["live", "sample"], index=0 if config.DATA_MODE == "live" else 1,
                    horizontal=True, help="live: today's Nutrislice menus, cached on disk and fetched at most "
                                          "once per station per day. sample: a saved real day, works offline.")
    if mode != config.DATA_MODE:
        config.DATA_MODE = mode
        T.clear_cache()

    with st.expander("Demo: break things on purpose"):
        st.caption("Shows how the agent copes when things go wrong. The agent isn't told these are "
                   "simulated; it just sees a missing menu or a dead network.")
        miss_west = st.toggle("West dinner not posted")
        miss_east = st.toggle("East lunch not posted")
        offline = st.toggle("Network down (use cached menus)")
        missing = ([("West", "dinner")] if miss_west else []) + ([("East", "lunch")] if miss_east else [])
        T.set_simulation(missing, offline)


# ---------------------------------------------------------------------------------------
# Header + tabs
# ---------------------------------------------------------------------------------------
st.markdown('<div class="dw-word">Dine<span>Wolfie</span></div>'
            '<div class="dw-tag">Tell it what you\'re aiming for. It reads today\'s East and West menus, '
            'checks the nutrition, and plans your whole day of eating.</div>', unsafe_allow_html=True)

tab_plan, tab_browse, tab_history, tab_how = st.tabs(["Plan my day", "Browse menus", "Past plans", "How it works"])

EXAMPLES = {
    "Vegetarian, 120 g protein": "I'm vegetarian, aiming for 120 g protein today, and I'll be near West at lunch.",
    "Bulking at East": "Bulking: as much protein as possible today, East only.",
    "Cutting under 1,800 kcal": "Cutting: under 1,800 kcal total today with at least 100 g protein.",
    "Light vegan dinner": "Just plan dinner tonight: something light and vegan.",
    "Cheat day": "It's my cheat day! Plan something fun, but keep it vegetarian.",
    "Comfort food": "Rough day. I want warm comfort food today, still around 100 g protein.",
}


def remember(key: str, value: str) -> None:
    current = T.read_prefs()
    values = list(current.get(key) or [])
    if value.lower() not in (v.lower() for v in values):
        values.append(value)
    T.update_prefs({key: values}, reason="feedback button")


def use_example() -> None:
    if st.session_state.get("example"):
        st.session_state.goal_box = EXAMPLES[st.session_state.example]


def start_run(goal: str, plan_date: date, resume: str | None, rail_slot, tray_slot) -> None:
    if not resume:
        st.session_state.update(events=[], plan=None, reply="", session_id=None, error=None, stats=None)
    events = st.session_state.events

    def on_event(ev: dict) -> None:
        events.append(ev)
        rail_slot.markdown(rail_html(events, live=True), unsafe_allow_html=True)
        if ev["type"] == "final_plan":
            tray_slot.markdown(tray_html(ev["plan"]), unsafe_allow_html=True)

    run = run_agent(goal, plan_date=plan_date, on_event=on_event, resume=resume)
    st.session_state.update(plan=run.plan or st.session_state.plan, reply=run.reply,
                            session_id=run.session_id or st.session_state.session_id, error=run.error,
                            stats={"seconds": run.duration_s, "tools": run.tool_calls,
                                   "adaptations": run.adaptations, "turns": run.num_turns})
    st.rerun()


with tab_plan:
    g1, g2 = st.columns([8, 3], gap="large", vertical_alignment="bottom")
    with g1:
        if "goal_box" not in st.session_state:
            st.session_state.goal_box = default_goal(prefs)
        st.text_area("What are you aiming for today?", key="goal_box", height=80)
    with g2:
        day_choice = st.radio("Plan for", ["Today", "Tomorrow"], horizontal=True)
        plan_date = date.today() + timedelta(days=1 if day_choice == "Tomorrow" else 0)
        go = st.button("Plan my day", type="primary", use_container_width=True)
    st.pills("Or try one", list(EXAMPLES), key="example", on_change=use_example)

    left, right = st.columns([4, 6], gap="large")
    with left:
        st.markdown("#### Agent at work")
        rail_slot = st.empty()
    with right:
        st.markdown("#### Your day")
        tray_slot = st.empty()
        after_slot = st.container()

    if st.session_state.events:
        rail_slot.markdown(rail_html(st.session_state.events, live=False), unsafe_allow_html=True)
    else:
        rail_slot.caption("Each step the agent takes shows up here as it happens: what it looked up, "
                          "what it found, and when it changed course.")
    if st.session_state.plan:
        tray_slot.markdown(tray_html(st.session_state.plan), unsafe_allow_html=True)
    else:
        tray_slot.markdown('<div class="empty">Your plan will land on this tray: breakfast, lunch and '
                           'dinner, which hall for each, and why.</div>', unsafe_allow_html=True)

    with after_slot:
        if st.session_state.error:
            st.error(st.session_state.error)
        if st.session_state.reply:
            with st.container(key="reply"):
                st.markdown(st.session_state.reply)
        if st.session_state.stats and st.session_state.plan:
            s = st.session_state.stats
            m1, m2, m3 = st.columns(3)
            m1.metric("Tool calls", s["tools"])
            m2.metric("Changes of course", s["adaptations"])
            m3.metric("Seconds", s["seconds"])
        if st.session_state.plan:
            c1, c2 = st.columns([1, 2])
            if c1.button("Send to my phone", use_container_width=True):
                try:
                    notify.send_plan(st.session_state.plan)
                    st.toast("Sent to your phone")
                except Exception as exc:
                    st.error(f"Couldn't send: {exc}")
            with c2.popover("Preview the notification", use_container_width=True):
                title, body = notify.plan_message(st.session_state.plan)
                st.markdown(f"**{title}**")
                st.text(body)
            with st.expander("Teach DineWolfie: love it or never again"):
                st.caption("Saved to memory. Favorites get picked first; never-again items are blocked for good.")
                seen = set()
                for meal in st.session_state.plan["meals"]:
                    for item in meal["items"]:
                        if item["name"] in seen:
                            continue
                        seen.add(item["name"])
                        n1, n2, n3 = st.columns([6, 2, 3])
                        n1.markdown(f"{item['name']}")
                        if n2.button("Love it", key=f"love-{item['id']}", use_container_width=True):
                            remember("favorites", item["name"])
                            st.toast(f"Added {item['name']} to favorites")
                        if n3.button("Never again", key=f"ban-{item['id']}", use_container_width=True):
                            remember("never_eat", item["name"])
                            st.toast(f"{item['name']} is blocked from now on")
            if st.session_state.session_id:
                with st.form("followup", clear_on_submit=True):
                    follow = st.text_input("Change something", placeholder="e.g. swap dinner to East, "
                                                                          "or I don't eat eggs")
                    if st.form_submit_button("Update the plan") and follow.strip():
                        st.session_state.pending_followup = follow.strip()
                        st.rerun()

    if go and st.session_state.goal_box.strip():
        start_run(st.session_state.goal_box.strip(), plan_date, None, rail_slot, tray_slot)
    if st.session_state.get("pending_followup"):
        follow = st.session_state.pop("pending_followup")
        start_run(follow, plan_date, st.session_state.session_id, rail_slot, tray_slot)


# ---------------------------------------------------------------------------------------
# Browse menus: the raw data the agent works from
# ---------------------------------------------------------------------------------------
with tab_browse:
    st.caption("The same data the agent sees, straight from Nutrislice. Nutrition is per serving.")
    b1, b2, b3 = st.columns(3)
    b_date = b1.date_input("Date", date.today())
    b_hall = b2.selectbox("Hall", list(config.HALLS))
    b_meal = b3.selectbox("Meal", ["breakfast", "lunch", "dinner", "late_night"],
                          format_func=lambda m: MEAL_LABEL[m])
    with st.spinner("Loading menu…"):
        menu = T.get_menu(b_hall, b_meal, b_date.isoformat())
    if menu["status"] != "ok":
        st.info(menu["summary"])
    else:
        st.caption(menu["summary"])
        rows = [{"Station": station, "Item": i["name"], "Serving": i["serving"], "kcal": i["calories"],
                 "Protein (g)": i["protein_g"], "Carbs (g)": i["carbs_g"], "Fat (g)": i["fat_g"],
                 "Contains": ", ".join(i["allergens"]), "Tags": ", ".join(i["tags"])}
                for station, items in menu["stations"].items() for i in items]
        st.dataframe(rows, use_container_width=True, hide_index=True, height=520)


# ---------------------------------------------------------------------------------------
# History: the agent's memory of past plans
# ---------------------------------------------------------------------------------------
with tab_history:
    history = list(reversed(T.read_history()))
    if not history:
        st.info("No plans yet. Make one on the Plan my day tab and it will show up here; the agent "
                "also uses this to avoid repeating meals.")
    for h in history[:20]:
        p = h["plan"]
        t = p["totals"]
        with st.expander(f"{p['date']}: {p.get('headline') or h.get('goal', '')} "
                         f"({t['protein_g']} g protein, {t['calories']} kcal)"):
            st.markdown(tray_html(p), unsafe_allow_html=True)


# ---------------------------------------------------------------------------------------
# How it works
# ---------------------------------------------------------------------------------------
with tab_how:
    svg_path = config.ROOT / "docs" / "architecture.svg"
    if svg_path.exists():
        st.image(str(svg_path), use_container_width=True)
    st.markdown("""
**The loop.** You give a goal. Claude (through the Claude Agent SDK) reads your memory, writes a short plan,
then calls tools: it opens menus, searches and compares the two halls, and adds up nutrition. It reads every
result, and when something fails (a hall hasn't posted dinner, the network is down, nothing fits your filters,
the goal is out of reach) it changes course and tells you what it changed. It finishes by sending the plan to
a checker that confirms every item is really on that hall's menu and safe for your allergies and diet.

**The code calculates, the model decides.** Every number on the tray comes from Python, never from the model.

**It runs on its own.** Every morning, Windows Task Scheduler runs the same agent with your saved goal and
pushes the plan to your phone with ntfy.
""")
