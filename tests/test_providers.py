"""Gemini and Cerebras engines: same loop as Groq, through their OpenAI-compatible APIs (no network)."""
import json
from types import SimpleNamespace

import config
from src import agent as A
from src import groq_agent as G
from src import tools as T
from tests.conftest import SAMPLE_DATE
from tests.test_hosting import _tool_call

DAY = T.resolve_date(SAMPLE_DATE)
GEMINI_IDS = ["models/gemini-2.5-flash", "models/gemini-3.8-flash", "models/gemini-3.8-flash-lite",
              "models/gemini-3.8-flash-preview-tts", "models/gemini-2.5-pro", "models/text-embedding-004",
              "models/gemini-3.9-flash-preview-0901"]


class FakeCompat:
    """An OpenAI-style client: a model list, and scripted chat replies (or exceptions)."""

    def __init__(self, script, ids=()):
        self.script, self.requests = list(script), []
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(id=i) for i in ids])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        msg = SimpleNamespace(content=step.get("content"), tool_calls=step.get("tool_calls"))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _submit(meal="dinner"):
    pick = T.scout_meal(meal, SAMPLE_DATE)["halls"]["East"]["items"][0]
    plan = {"date": SAMPLE_DATE, "headline": "x", "message": "Enjoy!",
            "meals": [{"meal": meal, "hall": "East", "items": [{"id": pick["id"]}], "reason": "r"}]}
    return {"tool_calls": [_tool_call(1, "submit_plan", plan)]}


def _run(provider, fake, model=None):
    run = A.AgentRun(goal="x", engine=provider)
    events = []
    token = A._current.set({"run": run, "on_event": events.append})
    try:
        G.run_groq(run, "just dinner", DAY, None, A.Engine(provider, "key-test", model), client=fake)
    finally:
        A._current.reset(token)
    return run, events


def test_gemini_ranking_prefers_the_newest_stable_flash():
    assert G.rank_models("gemini", GEMINI_IDS) == ["gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-2.5-flash"]


def test_cerebras_ranking_puts_gpt_oss_first():
    assert G.rank_models("cerebras", ["llama-4-scout", "gpt-oss-120b", "qwen-3-32b"])[0] == "gpt-oss-120b"


def test_gemini_auto_picks_a_listed_model_and_uses_gemini_settings(prefs):
    prefs()
    fake = FakeCompat([_submit()], GEMINI_IDS)
    run, _ = _run("gemini", fake)  # model None -> config.GEMINI_MODEL == "auto"
    sent = fake.requests[0]
    assert sent["model"] == "gemini-3.8-flash"
    assert sent["max_tokens"] == G.MAX_COMPLETION_TOKENS and "max_completion_tokens" not in sent
    assert sent["reasoning_effort"] == config.GROQ_REASONING_EFFORT
    assert run.plan is not None and run.reply.startswith("Enjoy!")


def test_gemini_falls_back_to_the_known_list_when_listing_fails(prefs, monkeypatch):
    prefs()
    fake = FakeCompat([_submit()])
    fake.models = SimpleNamespace(list=lambda: (_ for _ in ()).throw(RuntimeError("no list")))
    _run("gemini", fake)
    assert fake.requests[0]["model"] == config.GEMINI_MODELS[0]


def test_cerebras_drops_reasoning_effort_if_the_model_refuses_it(prefs):
    prefs()
    refused = type("BadRequest", (Exception,), {"status_code": 400})("Unsupported parameter: reasoning_effort")
    fake = FakeCompat([refused, _submit()], ["gpt-oss-120b"])
    run, _ = _run("cerebras", fake)
    assert fake.requests[0]["model"] == "gpt-oss-120b" and "reasoning_effort" in fake.requests[0]
    assert "reasoning_effort" not in fake.requests[1]
    assert fake.requests[1]["max_completion_tokens"] == G.MAX_COMPLETION_TOKENS
    assert run.plan is not None


def test_rate_limits_are_tracked_per_service(prefs):
    prefs()
    limited = type("RL", (Exception,), {"status_code": 429,
                                        "response": SimpleNamespace(headers={"retry-after": "30"})})("rate limit")
    fake = FakeCompat([limited, _submit()], ["gpt-oss-120b", "llama-4-scout"])
    run, events = _run("cerebras", fake)
    assert [r["model"] for r in fake.requests] == ["gpt-oss-120b", "llama-4-scout"]
    assert "cerebras:gpt-oss-120b" in G._cooldown and "groq:gpt-oss-120b" not in G._cooldown
    assert any("Cerebras's free-tier limit" in e.get("text", "") for e in events)


def test_clients_point_at_the_right_service(monkeypatch):
    import openai
    made = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: made.append(kw) or SimpleNamespace())
    G.make_client("gemini", "g-key")
    G.make_client("cerebras", "c-key")
    assert made[0]["base_url"].startswith("https://generativelanguage.googleapis.com/")
    assert made[1]["base_url"] == "https://api.cerebras.ai/v1" and made[1]["max_retries"] == 0


def test_missing_key_says_where_to_put_it(prefs):
    import pytest
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        G.run_groq(A.AgentRun(goal="x"), "x", DAY, None, A.Engine("gemini", None))


def test_app_offers_gemini_and_cerebras_with_optional_keys(monkeypatch):
    from streamlit.testing.v1 import AppTest
    for var in ("GEMINI_API_KEY", "CEREBRAS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60)
    at.secrets["hosted"] = True
    at.run()
    at.sidebar.radio(key="provider").set_value("Gemini").run()
    assert not at.exception
    assert "No Gemini key yet" in " ".join(c.value for c in at.caption)
    assert at.selectbox(key="model_gemini").value == "auto"
    at.sidebar.radio(key="provider").set_value("Cerebras").run()
    assert "No Cerebras key yet" in " ".join(c.value for c in at.caption)
