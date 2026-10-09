"""Hosted-site features: memory stores, key sharing rules, Groq engine, Tavily lookup, isolation."""
import json
import threading
from types import SimpleNamespace

import pytest

import config
from src import access, storage
from src import tools as T
from tests.conftest import SAMPLE_DATE


# --- memory stores --------------------------------------------------------------------------

class FakeWorksheet:
    """Just enough of gspread's Worksheet for SheetStore."""

    def __init__(self):
        self.rows = [list(storage.SHEET_HEADER)]

    def find(self, value, in_column=1):
        for i, row in enumerate(self.rows, start=1):
            if row and row[in_column - 1] == value:
                return SimpleNamespace(row=i)
        return None

    def row_values(self, row):
        return list(self.rows[row - 1])

    def append_row(self, values, value_input_option=None):
        self.rows.append(list(values))

    def update(self, values, range_name, value_input_option=None):
        row = int(range_name.split(":")[0][1:])
        self.rows[row - 1] = list(values[0])


def test_sheet_store_round_trip_per_user():
    ws = FakeWorksheet()
    alice = storage.SheetStore(ws, "Alice@stonybrook.edu", "Alice")
    assert alice.load_prefs() is None
    alice.save_prefs({"diet": ["vegan"]})
    alice.save_history([{"date": "2026-10-01", "plan": {}}])
    bob = storage.SheetStore(ws, "bob@stonybrook.edu")
    bob.save_prefs({"diet": []})
    again = storage.SheetStore(ws, "alice@stonybrook.edu")  # new session, same person
    assert again.load_prefs() == {"diet": ["vegan"]}
    assert len(again.load_history()) == 1
    assert len(ws.rows) == 3  # header + one row per user, no duplicates


def test_sheet_store_trims_history_to_fit_a_cell():
    ws = FakeWorksheet()
    store = storage.SheetStore(ws, "a@x.edu")
    big = [{"date": f"d{i}", "plan": {"note": "x" * 2000}} for i in range(40)]
    store.save_history(big)
    assert len(ws.rows[1][3]) <= storage.SHEET_CELL_LIMIT
    assert store.load_history()[-1]["date"] == "d39"  # newest kept, oldest dropped


def test_tools_use_the_current_store(prefs):
    prefs(name="file person")
    mem = storage.MemoryStore({"name": "guest"})
    storage.use(mem)
    try:
        assert T.read_prefs()["name"] == "guest"
        T.update_prefs({"allergies": ["peanuts"]})
        assert mem.load_prefs()["allergies"] == ["peanuts"]
    finally:
        storage.use(None)
    assert T.read_prefs()["name"] == "file person"  # the file was never touched


def test_two_visitors_never_share_memory_or_switches():
    results = {}

    def visitor(name, missing):
        storage.use(storage.MemoryStore({"name": name}))
        T.set_simulation(missing=missing)
        results[name] = (T.read_prefs()["name"], T.get_menu("West", "dinner", SAMPLE_DATE)["status"])

    a = threading.Thread(target=visitor, args=("ana", [("West", "dinner")]))
    b = threading.Thread(target=visitor, args=("ben", []))
    a.start(); a.join(); b.start(); b.join()
    assert results["ana"] == ("ana", "no_menu_posted")
    assert results["ben"] == ("ben", "ok")


# --- who may use which key --------------------------------------------------------------------

POLICY = dict(hosted=True, auth_enabled=True, signed_in=True, email="me@stonybrook.edu", allowed_emails=[])


def test_pasted_key_always_wins():
    assert access.choose_key(" sk-mine ", "sk-app", **POLICY) == ("sk-mine", access.YOURS)


def test_app_key_needs_sign_in_when_hosted():
    assert access.choose_key("", "sk-app", **{**POLICY, "signed_in": False}) == (None, access.SIGN_IN)
    assert access.choose_key("", "sk-app", **{**POLICY, "auth_enabled": False}) == (None, access.SIGN_IN)
    assert access.choose_key("", "sk-app", **POLICY) == ("sk-app", access.APP)


def test_allow_list_and_local_runs():
    listed = {**POLICY, "allowed_emails": ["friend@stonybrook.edu"]}
    assert access.choose_key("", "sk-app", **listed) == (None, access.NOT_ALLOWED)
    assert access.choose_key("", "sk-app", **{**listed, "email": "Friend@stonybrook.edu"})[1] == access.APP
    assert access.choose_key("", "sk-app", **{**POLICY, "hosted": False, "signed_in": False})[1] == access.APP
    assert access.choose_key("", None, **POLICY) == (None, access.NONE)


# --- Tavily -------------------------------------------------------------------------------------

def test_lookup_nutrition_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert T.lookup_nutrition("Hominy Stew")["status"] == "error"


def test_lookup_nutrition_calls_tavily_and_labels_estimates(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, auth=headers["Authorization"])
        return SimpleNamespace(status_code=200, json=lambda: {
            "answer": "About 250 kcal and 9 g protein per cup.",
            "results": [{"title": "Stew", "url": "https://example.com", "content": "Nutrition facts..."}]})

    monkeypatch.setattr(T.requests, "post", fake_post)
    T.configure(tavily_key="tvly-test")
    res = T.lookup_nutrition("Hominy Stew")
    assert res["status"] == "ok" and "9 g protein" in res["answer"]
    assert "estimate" in res["note"] and "don't add" in res["note"]
    assert seen["url"] == "https://api.tavily.com/search" and seen["auth"] == "Bearer tvly-test"


def test_web_lookup_tool_only_exists_with_a_key(monkeypatch):
    from src import agent
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert "lookup_nutrition" not in [s[0] for s in agent.active_specs()]
    T.configure(tavily_key="tvly-test")
    assert "lookup_nutrition" in [s[0] for s in agent.active_specs()]


# --- Groq engine (with a fake model, no network) -----------------------------------------------

def _tool_call(i, name, args):
    return SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class FakeGroq:
    """Plays a short scripted conversation: read prefs, search, submit, then answer."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.script.pop(0)
        if callable(step):
            step = step()
        msg = SimpleNamespace(content=step.get("content"), tool_calls=step.get("tool_calls"), reasoning=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_groq_loop_runs_tools_and_gets_a_checked_plan(prefs):
    from src.agent import AgentRun, Engine, _current
    from src.groq_agent import run_groq

    prefs(diet=["vegetarian"])
    pick = T.search_items("dinner", hall="East", date=SAMPLE_DATE, sort_by="protein", limit=1)["items"][0]
    plan = {"date": SAMPLE_DATE, "headline": "Dinner at East",
            "meals": [{"meal": "dinner", "hall": "East", "items": [{"id": pick["id"]}], "reason": "top protein"}]}
    fake = FakeGroq([
        {"tool_calls": [_tool_call(1, "get_prefs", {})]},
        {"tool_calls": [_tool_call(2, "search_items", {"meal": "dinner", "hall": "East", "date": SAMPLE_DATE})]},
        {"tool_calls": [_tool_call(3, "submit_plan", plan)]},
        {"content": "Dinner at East is set!"},
    ])
    run = AgentRun(goal="plan dinner", engine="groq")
    events = []
    token = _current.set({"run": run, "on_event": events.append})
    try:
        run_groq(run, "plan dinner", T.resolve_date(SAMPLE_DATE), None, Engine("groq", "gsk-test"), client=fake)
    finally:
        _current.reset(token)
    assert run.plan is not None and run.plan["meals"][0]["hall"] == "East"
    assert run.reply == "Dinner at East is set!"
    assert run.tool_calls == 3
    assert [e["tool"] for e in events if e["type"] == "tool_call"] == ["get_prefs", "search_items", "submit_plan"]
    # every tool result went back to the model in the next request
    assert sum(m["role"] == "tool" for m in fake.requests[-1]["messages"]) == 3
    assert {t["function"]["name"] for t in fake.requests[0]["tools"]} >= {"submit_plan", "search_items"}


def test_groq_loop_reports_bad_tool_arguments_and_nudges_to_finish(prefs):
    from src.agent import AgentRun, Engine, _current
    from src.groq_agent import run_groq

    prefs()
    bad = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_menu", arguments="{not json"))
    fake = FakeGroq([{"tool_calls": [bad]}, {"content": "Done?"}, {"content": "Sorry, no plan."}])
    run = AgentRun(goal="x", engine="groq")
    token = _current.set({"run": run, "on_event": None})
    try:
        run_groq(run, "x", T.resolve_date(SAMPLE_DATE), None, Engine("groq", "gsk-test"), client=fake)
    finally:
        _current.reset(token)
    tool_msg = next(m for m in fake.requests[1]["messages"] if m["role"] == "tool")
    assert "not valid JSON" in tool_msg["content"]
    assert any("call submit_plan" in (m.get("content") or "") for m in fake.requests[2]["messages"])
    assert run.plan is None and run.reply == "Sorry, no plan."


def test_groq_needs_a_key():
    from src.agent import AgentRun, Engine
    from src.groq_agent import run_groq
    with pytest.raises(RuntimeError, match="API key"):
        run_groq(AgentRun(goal="x"), "x", T.resolve_date(SAMPLE_DATE), None, Engine("groq", None))


# --- the hosted web app (guest, no keys) ----------------------------------------------------------

def test_hosted_guest_gets_private_memory_and_no_shared_key(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60)
    at.secrets["hosted"] = True
    at.secrets["ANTHROPIC_API_KEY"] = "sk-owner"  # the owner's key must not be usable by a guest
    at.run()
    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "this browser tab only" in captions
    assert "Sign in with Google to use the app's Anthropic key" in captions
    plan_button = next(b for b in at.button if b.label == "Plan my day")
    plan_button.click().run()
    assert any("Sign in with Google" in w.value for w in at.warning)
