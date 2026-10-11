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


def test_gemini_ranking_puts_the_lightest_stable_flash_first():
    assert G.rank_models("gemini", GEMINI_IDS) == ["gemini-3.8-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"]
    older_lite = ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-2.5-flash-lite", "gemini-3.8-flash-lite"]
    assert G.rank_models("gemini", older_lite) == ["gemini-3.8-flash-lite", "gemini-2.5-flash-lite",
                                                   "gemini-3.8-flash", "gemini-2.5-flash"]


def test_model_size_is_read_from_the_name():
    assert G._size_b("llama3.1-8b") == 8 and G._size_b("gpt-oss-120b") == 120
    assert G._size_b("qwen-3-235b-a22b-instruct-2507") == 235  # total size, not the "a22b" active part
    assert G._size_b("llama-4-scout-17b-16e-instruct") == 17 and G._size_b("zai-glm-4.7") is None


def test_cerebras_ranking_puts_the_smallest_first_and_unknown_sizes_last():
    ids = ["zai-glm-4.7", "gpt-oss-120b", "llama3.1-8b", "qwen-3-235b-a22b-instruct-2507"]
    assert G.rank_models("cerebras", ids) == ["llama3.1-8b", "gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507",
                                              "zai-glm-4.7"]


def test_gemini_auto_picks_a_listed_model_and_uses_gemini_settings(prefs):
    prefs()
    fake = FakeCompat([_submit()], GEMINI_IDS)
    run, _ = _run("gemini", fake)  # model None -> config.GEMINI_MODEL == "auto"
    sent = fake.requests[0]
    assert sent["model"] == "gemini-3.8-flash-lite"
    assert sent["max_tokens"] == G.MAX_COMPLETION_TOKENS and "max_completion_tokens" not in sent
    assert sent["reasoning_effort"] == config.GROQ_REASONING_EFFORT
    assert run.plan is not None and run.reply.startswith("Enjoy!")


def test_gemini_falls_back_to_the_known_list_when_listing_fails(prefs, monkeypatch):
    prefs()
    fake = FakeCompat([_submit()])
    fake.models = SimpleNamespace(list=lambda: (_ for _ in ()).throw(RuntimeError("no list")))
    _run("gemini", fake)
    assert fake.requests[0]["model"] == G.lightest_first("gemini", config.GEMINI_MODELS)[0]
    assert "lite" in fake.requests[0]["model"]


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
    assert G._slot("cerebras", "gpt-oss-120b", "key-test") in G._cooldown
    assert not any(k.startswith("groq") for k in G._cooldown)
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


def _signed_call(i, name, args, signature):
    """A tool call exactly as the OpenAI SDK parses one from Gemini, signature included."""
    from openai.types.chat import ChatCompletionMessageFunctionToolCall
    return ChatCompletionMessageFunctionToolCall.model_validate(
        {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
         "extra_content": {"google": {"thought_signature": signature}}})


def _assistant_calls(request):
    return [m["tool_calls"] for m in request["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]


def test_gemini_thought_signatures_are_sent_back(prefs):
    """Gemini 3 rejects a conversation whose earlier tool calls come back unsigned (HTTP 400)."""
    prefs()
    search = _signed_call(1, "search_items", {"meal": "dinner", "hall": "East", "limit": 3}, "SIG-A")
    fake = FakeCompat([{"tool_calls": [search]}, _submit()])
    run, _ = _run("gemini", fake, model="gemini-3.8-flash")
    assert run.plan is not None
    calls = _assistant_calls(fake.requests[1])[0]
    assert calls[0]["extra_content"] == {"google": {"thought_signature": "SIG-A"}}
    assert all(not k.startswith("_") for m in fake.requests[1]["messages"] for k in m)


def test_gemini_calls_from_another_model_get_the_placeholder_signature():
    calls = [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"},
              "extra_content": {"google": {"thought_signature": "SIG-A"}}},
             {"id": "c2", "type": "function", "function": {"name": "y", "arguments": "{}"}}]
    messages = [{"role": "assistant", "content": None, "tool_calls": calls, "_model": "gemini-a"}]
    same = G._wire(messages, "gemini", "gemini-a")[0]["tool_calls"]
    assert same[0]["extra_content"]["google"]["thought_signature"] == "SIG-A" and "extra_content" not in same[1]
    other = G._wire(messages, "gemini", "gemini-b")[0]["tool_calls"]
    assert other[0]["extra_content"]["google"]["thought_signature"] == G.PLACEHOLDER_SIGNATURE
    assert "extra_content" not in other[1]  # only the first of parallel calls is signed
    unsigned_history = [{"role": "assistant", "content": None, "tool_calls": calls[1:]}]  # e.g. older history
    assert G._wire(unsigned_history, "gemini", "gemini-a")[0]["tool_calls"][0]["extra_content"]
    assert G._wire(messages, "groq", "gemini-b")[0]["tool_calls"] == calls  # other services: untouched


def test_a_rejected_signature_is_retried_once_with_the_placeholder(prefs):
    prefs()
    search = _signed_call(1, "search_items", {"meal": "dinner", "hall": "East", "limit": 3}, "SIG-A")
    bad = type("BadRequestError", (Exception,), {"status_code": 400})(
        "Function call is missing a thought_signature in functionCall parts.")
    fake = FakeCompat([{"tool_calls": [search]}, bad, _submit()])
    run, _ = _run("gemini", fake, model="gemini-3.8-flash")
    assert run.plan is not None and len(fake.requests) == 3
    retried = _assistant_calls(fake.requests[2])[0]
    assert retried[0]["extra_content"]["google"]["thought_signature"] == G.PLACEHOLDER_SIGNATURE


# --- "auto" starts light and steps up when the light model struggles -----------------------------

LIGHT_AND_FULL = ["models/gemini-3.8-flash-lite", "models/gemini-3.8-flash", "models/gemini-2.5-flash"]


def _bad_submit():
    plan = {"date": SAMPLE_DATE, "headline": "x", "message": "x",
            "meals": [{"meal": "dinner", "hall": "East", "items": [{"id": "not-on-the-menu"}], "reason": "r"}]}
    return {"tool_calls": [_tool_call(9, "submit_plan", plan)]}


def _refused(text="Request too long for this model's context window"):
    return type("BadRequest", (Exception,), {"status_code": 400})(text)


def test_auto_hands_the_plan_to_a_bigger_model_after_two_rejections(prefs):
    prefs()
    fake = FakeCompat([_bad_submit(), _bad_submit(), _submit()], LIGHT_AND_FULL)
    run, events = _run("gemini", fake)
    assert [r["model"] for r in fake.requests] == ["gemini-3.8-flash-lite"] * 2 + ["gemini-3.8-flash"]
    assert run.plan is not None and not run.error
    assert any("handing the plan to gemini-3.8-flash" in e.get("text", "") for e in events)


def test_a_model_the_student_chose_is_never_swapped_for_a_bigger_one(prefs):
    prefs()
    fake = FakeCompat([_bad_submit(), _bad_submit(), _submit()], LIGHT_AND_FULL)
    run, _ = _run("gemini", fake, model="gemini-3.8-flash-lite")
    assert {r["model"] for r in fake.requests} == {"gemini-3.8-flash-lite"} and run.plan is not None


def test_auto_steps_up_when_the_light_model_cant_take_the_request(prefs):
    prefs()
    fake = FakeCompat([_refused(), _submit()], LIGHT_AND_FULL)
    run, _ = _run("gemini", fake)
    assert [r["model"] for r in fake.requests] == ["gemini-3.8-flash-lite", "gemini-3.8-flash"]
    assert run.plan is not None


def test_a_refused_request_on_a_chosen_model_is_reported_not_retried(prefs):
    import pytest
    prefs()
    fake = FakeCompat([_refused(), _submit()], LIGHT_AND_FULL)
    with pytest.raises(Exception, match="context window"):
        _run("gemini", fake, model="gemini-3.8-flash-lite")
    assert len(fake.requests) == 1


def test_auto_steps_up_at_most_twice(prefs):
    import pytest
    prefs()
    ids = LIGHT_AND_FULL + ["models/gemini-2.0-flash"]
    fake = FakeCompat([_refused(), _refused(), _refused(), _submit()], ids)
    with pytest.raises(Exception, match="context window"):
        _run("gemini", fake)
    assert len(fake.requests) == 3  # the light model, then two steps up, then it gives the error


def test_auto_steps_up_when_the_light_model_never_submits_a_plan(prefs):
    prefs()
    fake = FakeCompat([{"content": "Sounds good!"}, {"content": "Here you go."}, _submit()], LIGHT_AND_FULL)
    run, _ = _run("gemini", fake)
    assert [r["model"] for r in fake.requests] == ["gemini-3.8-flash-lite"] * 2 + ["gemini-3.8-flash"]
    assert run.plan is not None
    assert fake.requests[2]["messages"][-1]["content"].startswith("Please finish")


def test_the_last_model_in_the_list_keeps_going_without_stepping_up(prefs):
    prefs()
    fake = FakeCompat([_bad_submit(), _bad_submit(), _submit()], ["models/gemini-3.8-flash-lite"])
    run, _ = _run("gemini", fake)
    assert {r["model"] for r in fake.requests} == {"gemini-3.8-flash-lite"} and run.plan is not None
