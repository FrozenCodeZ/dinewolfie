"""Download menus from Nutrislice and cache them on disk.

Politeness rules (see README "Data"):
  * One request returns a whole week for one station, and we cache it.
  * A cached week is re-used all day; we refresh it at most once per day.
  * Weeks that are fully in the past are never fetched again.
  * If the network fails we fall back to the cached copy and say so.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import config


class FetchError(Exception):
    """Raised when a menu can't be loaded from the network or the cache."""


@dataclass
class Station:
    hall: str          # "East" / "West"
    menu_type_id: int
    slug: str
    name: str          # human name, e.g. "Rooted"


_last_request_at = 0.0


def _get_json(url: str) -> dict | list:
    """GET a URL politely (rate-limited, identified User-Agent)."""
    global _last_request_at
    wait = config.POLITE_DELAY_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(url, headers={"User-Agent": config.USER_AGENT},
                            timeout=config.REQUEST_TIMEOUT_S)
    finally:
        _last_request_at = time.monotonic()
    if resp.status_code in (401, 403, 429):
        # Being blocked or rate-limited: stop, don't retry or work around it.
        raise FetchError(f"Nutrislice refused the request (HTTP {resp.status_code}). "
                         "Stopping instead of retrying.")
    resp.raise_for_status()
    return resp.json()


def week_start(d: date) -> date:
    """Nutrislice weeks run Sunday to Saturday."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _read_cache(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _write_cache(path: Path, url: str, data) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapped = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "url": url, "data": data}
    path.write_text(json.dumps(wrapped), encoding="utf-8")
    return wrapped


def _fresh(wrapped: dict, week_last_day: date) -> bool:
    """A cached file is good enough if it was fetched today, or the week is over."""
    fetched = datetime.fromisoformat(wrapped["fetched_at"]).date()
    return fetched >= date.today() or week_last_day < fetched


# --- stations ---------------------------------------------------------------

def list_stations(hall: str, offline: bool = False) -> list[Station]:
    """All active stations (Nutrislice "menu types") for a hall."""
    if hall not in config.HALLS:
        raise FetchError(f"Unknown hall {hall!r}; expected one of {list(config.HALLS)}")
    path = config.CACHE_DIR / "schools.json"
    wrapped = _read_cache(path)
    # The list of locations rarely changes, so a week-old copy is fine.
    stale = wrapped is not None and (
        date.today() - datetime.fromisoformat(wrapped["fetched_at"]).date()).days >= 7
    if (wrapped is None or stale) and not offline:
        try:
            wrapped = _write_cache(path, config.SCHOOLS_URL, _get_json(config.SCHOOLS_URL))
        except (requests.RequestException, FetchError) as exc:
            if wrapped is None:
                raise FetchError(f"Couldn't load the list of dining locations: {exc}") from exc
    if wrapped is None:
        raise FetchError("No cached list of dining locations and we're offline.")

    school_id = config.HALLS[hall]["school_id"]
    for school in wrapped["data"]:
        if school["id"] == school_id:
            return [
                Station(hall, mt["id"], mt["slug"], mt["name"])
                for mt in school.get("active_menu_types", [])
                if mt["slug"] not in config.SKIP_MENU_TYPE_SLUGS
            ]
    raise FetchError(f"{config.HALLS[hall]['name']} isn't listed on Nutrislice right now.")


# --- menus --------------------------------------------------------------------

def station_cache_path(station: Station, start: date) -> Path:
    return config.CACHE_DIR / start.isoformat() / station.hall / f"{station.menu_type_id}-{station.slug}.json"


def fetch_station_week(station: Station, day: date, offline: bool = False) -> tuple[dict, str]:
    """Return (raw week JSON, source) where source is 'cache', 'network' or 'stale-cache'."""
    start = week_start(day)
    path = station_cache_path(station, start)
    wrapped = _read_cache(path)
    if wrapped is not None and _fresh(wrapped, start + timedelta(days=6)):
        return wrapped["data"], "cache"
    if offline:
        if wrapped is not None:
            return wrapped["data"], "stale-cache"
        raise FetchError(f"No cached menu for {station.hall} {station.name} (offline).")

    url = config.WEEK_URL.format(school_id=config.HALLS[station.hall]["school_id"],
                                 menu_type_id=station.menu_type_id,
                                 y=start.year, m=start.month, d=start.day)
    try:
        return _write_cache(path, url, _get_json(url))["data"], "network"
    except (requests.RequestException, ValueError, FetchError) as exc:
        if wrapped is not None:
            return wrapped["data"], "stale-cache"
        raise FetchError(f"Couldn't load {station.hall} {station.name}: {exc}") from exc


def fetch_hall_day(hall: str, day: date, offline: bool = False) -> dict:
    """Raw data for every station in a hall for one day.

    Returns {"stations": [(Station, raw_day_dict), ...], "sources": {...}, "errors": [...]}.
    A single broken station doesn't sink the whole hall; it's reported in errors.
    """
    out = {"stations": [], "sources": {}, "errors": []}
    for station in list_stations(hall, offline=offline):
        try:
            week, source = fetch_station_week(station, day, offline=offline)
        except FetchError as exc:
            out["errors"].append(str(exc))
            continue
        out["sources"][station.name] = source
        for raw_day in week.get("days", []):
            if raw_day.get("date") == day.isoformat():
                out["stations"].append((station, raw_day))
    return out


if __name__ == "__main__":
    # Quick manual check:  python -m src.fetch_menu
    today = date.today()
    for hall in config.HALLS:
        result = fetch_hall_day(hall, today)
        n = sum(len(d.get("menu_items", [])) for _, d in result["stations"])
        print(f"{hall}: {len(result['stations'])} stations, {n} raw rows, "
              f"sources={set(result['sources'].values())}, errors={result['errors']}")
