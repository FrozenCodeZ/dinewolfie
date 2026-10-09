"""Where each person's memory (preferences + past plans) is kept.

Four kinds of store, all with the same four methods:

  FileStore      prefs.json + history.json on this computer (running locally, one person)
  MemoryStore    kept only for one browser session (guests on the hosted site; resets on reload)
  SupabaseStore  one row per account in a Supabase table (email accounts on the hosted site)
  SheetStore     one row per signed-in user in a Google Sheet (Google sign-in on the hosted site)

The web app picks a store for each visitor and calls use(store). Everything else
(tools.py) just calls current(), so the agent never needs to know where memory lives.
A ContextVar keeps visitors apart: each Streamlit session runs in its own thread.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import config

HISTORY_LIMIT = 60
SHEET_CELL_LIMIT = 45_000  # Google Sheets allows 50,000 characters per cell; keep a margin
SHEET_HEADER = ["user_id", "name", "prefs_json", "history_json", "updated_at"]


class FileStore:
    kind = "file"

    def __init__(self, prefs_path: Path, history_path: Path):
        self.prefs_path, self.history_path = Path(prefs_path), Path(history_path)

    def load_prefs(self) -> dict | None:
        if self.prefs_path.exists():
            return json.loads(self.prefs_path.read_text(encoding="utf-8"))
        return None

    def save_prefs(self, prefs: dict) -> None:
        self.prefs_path.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")

    def load_history(self) -> list[dict]:
        if self.history_path.exists():
            try:
                return json.loads(self.history_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []

    def save_history(self, history: list[dict]) -> None:
        self.history_path.write_text(json.dumps(history[-HISTORY_LIMIT:], indent=2), encoding="utf-8")


class MemoryStore:
    kind = "session"

    def __init__(self, prefs: dict | None = None):
        self.prefs, self.history = (dict(prefs) if prefs else None), []

    def load_prefs(self) -> dict | None:
        return dict(self.prefs) if self.prefs is not None else None

    def save_prefs(self, prefs: dict) -> None:
        self.prefs = dict(prefs)

    def load_history(self) -> list[dict]:
        return list(self.history)

    def save_history(self, history: list[dict]) -> None:
        self.history = list(history[-HISTORY_LIMIT:])


class SheetStore:
    """One row per user: user_id | name | prefs_json | history_json | updated_at.

    `worksheet` is a gspread Worksheet (or anything with the same few methods, for tests).
    The row is read once and cached; every save writes the whole row back.
    """
    kind = "sheet"

    def __init__(self, worksheet, user_id: str, name: str = ""):
        self.ws, self.user_id, self.name = worksheet, user_id.strip().lower(), name
        self._row: int | None = None
        self._prefs: dict | None = None
        self._history: list[dict] = []
        self._load()

    def _load(self) -> None:
        cell = self.ws.find(self.user_id, in_column=1)
        if cell is None:
            return
        self._row = cell.row
        values = self.ws.row_values(cell.row) + [""] * len(SHEET_HEADER)
        self._prefs = json.loads(values[2]) if values[2] else None
        self._history = json.loads(values[3]) if values[3] else []

    def _write(self) -> None:
        history = self._history[-HISTORY_LIMIT:]
        history_json = json.dumps(history)
        while len(history_json) > SHEET_CELL_LIMIT and history:  # drop oldest plans until it fits
            history = history[1:]
            history_json = json.dumps(history)
        self._history = history
        row = [self.user_id, self.name, json.dumps(self._prefs or {}), history_json,
               datetime.now().isoformat(timespec="seconds")]
        if self._row is None:
            self.ws.append_row(row, value_input_option="RAW")
            cell = self.ws.find(self.user_id, in_column=1)
            self._row = cell.row if cell else None
        else:
            self.ws.update([row], f"A{self._row}:E{self._row}", value_input_option="RAW")

    def load_prefs(self) -> dict | None:
        return dict(self._prefs) if self._prefs is not None else None

    def save_prefs(self, prefs: dict) -> None:
        self._prefs = dict(prefs)
        self._write()

    def load_history(self) -> list[dict]:
        return list(self._history)

    def save_history(self, history: list[dict]) -> None:
        self._history = list(history)
        self._write()


class SupabaseStore:
    """One row per account in public.dinewolfie_memory (see supabase/schema.sql).

    `client` is that person's signed-in Supabase client, so Row Level Security only ever lets it
    touch their own row. The row is read once and cached; every save writes the whole row back.
    """
    kind = "supabase"
    TABLE = "dinewolfie_memory"

    def __init__(self, client, user_id: str):
        self.client, self.user_id = client, str(user_id)
        self._prefs: dict | None = None
        self._history: list[dict] = []
        self._load()

    def _fresh(self):
        self.client.auth.get_session()  # renews the sign-in if it has expired (tokens last an hour)
        return self.client.table(self.TABLE)

    def _load(self) -> None:
        rows = self._fresh().select("prefs,history").eq("user_id", self.user_id).limit(1).execute().data
        if rows:
            self._prefs = rows[0].get("prefs") or None
            self._history = rows[0].get("history") or []

    def _write(self) -> None:
        self._history = self._history[-HISTORY_LIMIT:]
        row = {"user_id": self.user_id, "prefs": self._prefs or {}, "history": self._history,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            self._fresh().upsert(row, on_conflict="user_id").execute()
        except Exception as exc:
            raise RuntimeError(f"Couldn't save to your account ({exc}).") from exc

    def load_prefs(self) -> dict | None:
        return dict(self._prefs) if self._prefs is not None else None

    def save_prefs(self, prefs: dict) -> None:
        self._prefs = dict(prefs)
        self._write()

    def load_history(self) -> list[dict]:
        return list(self._history)

    def save_history(self, history: list[dict]) -> None:
        self._history = list(history)
        self._write()


def open_worksheet(service_account_info: dict, sheet_id: str, tab: str = "users"):
    """Open (or create) the users tab of the DineWolfie Google Sheet."""
    import gspread  # only needed on the hosted site

    client = gspread.service_account_from_dict(dict(service_account_info))
    sheet = client.open_by_key(sheet_id)
    try:
        ws = sheet.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=tab, rows=200, cols=len(SHEET_HEADER))
    if ws.row_values(1) != SHEET_HEADER:
        ws.update([SHEET_HEADER], "A1:E1")
    return ws


_current: ContextVar = ContextVar("dinewolfie_store", default=None)


def use(store) -> None:
    """Make `store` the memory for everything running in this session/thread."""
    _current.set(store)


def current():
    """The store for this session; by default the local prefs.json / history.json."""
    store = _current.get()
    return store if store is not None else FileStore(config.PREFS_FILE, config.HISTORY_FILE)
