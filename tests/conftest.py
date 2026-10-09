"""Shared test setup: run everything offline against the committed sample data,
with throwaway prefs/history files so tests never touch your real memory."""
import json

import pytest

import config
from src import groq_agent, locations, storage
from src import tools as T

SAMPLE_DATE = "2026-10-01"


@pytest.fixture(autouse=True)
def offline_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_MODE", "sample")
    monkeypatch.setattr(config, "LIVE_FETCH", False)          # tests never touch the network
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "menus")  # nor your real menu cache
    locations._memo.clear()
    groq_agent._cooldown.clear()      # rate-limit state is shared per process; start each test clean
    groq_agent._unavailable.clear()
    monkeypatch.setattr(config, "PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "history.json")
    T.clear_cache()
    T.set_simulation()
    T.configure(tavily_key=None, data_mode=None)
    storage.use(None)
    yield
    T.set_simulation()
    T.configure(tavily_key=None, data_mode=None)
    storage.use(None)
    T.clear_cache()


@pytest.fixture
def prefs():
    """Write prefs for a test: prefs(diet=["vegan"], allergies=["peanuts"])."""
    def _write(**values):
        data = dict(T.DEFAULT_PREFS)
        data.update(values)
        config.PREFS_FILE.write_text(json.dumps(data), encoding="utf-8")
        return data
    return _write
