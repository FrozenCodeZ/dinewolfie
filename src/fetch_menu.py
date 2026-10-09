"""Download menus from Nutrislice and cache them on disk.

SBU Campus Dining has given permission, so live fetching is on by default (config.LIVE_FETCH).
Set DINEWOLFIE_LIVE_FETCH=off to use only saved menus.

Politeness rules (see README "Data"):
  * Any refusal or rate limit (HTTP 401/403/429 or a Retry-After header) stops ALL
    fetching for the rest of the run; nothing is retried.
  * One request returns a whole week for one station, and we cache it.
  * A cached week is re-used all day; we refresh it at most once per day.
  * Weeks that are fully in the past are never fetched again.
  * If the network fails we fall back to the cached copy and say so.
"""
from __future__ import annotations

import json
import re
import threading
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
    hall: str          # the location's key: "East", "West", "Roth Food Court", ...
    menu_type_id: int
    slug: str
    name: str          # human name, e.g. "Rooted"
    school_id: int | None = None  # Nutrislice location id (looked up from config.HALLS if missing)


# One network fetch at a time across all visitors and the background pre-load, so the same
# station is never requested twice at once (the second caller then finds it in the cache).
_fetch_lock = threading.RLock()


_last_request_at = 0.0
# Set the first time Nutrislice refuses or rate-limits us. After that, no request is sent
# again until the program restarts.
_stopped_reason: str | None = None


def _get_json(url: str) -> dict | list:
    """GET a URL politely (rate-limited, identified User-Agent).

    This is the ONLY place that contacts Nutrislice. It refuses unless live fetching is
    switched on (config.LIVE_FETCH), and it stops for good on any refusal or rate limit.
    """
    global _last_request_at, _stopped_reason
    if not config.LIVE_FETCH:
        raise FetchError("Live fetching from Nutrislice is turned off (DINEWOLFIE_LIVE_FETCH is not 'on'), "
                         "so only saved menus are used.")
    if _stopped_reason:
        raise FetchError(f"Not contacting Nutrislice again: {_stopped_reason}")
    wait = config.POLITE_DELAY_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(url, headers={"User-Agent": config.USER_AGENT},
                            timeout=config.REQUEST_TIMEOUT_S)
    finally:
        _last_request_at = time.monotonic()
    if resp.status_code in (401, 403, 429) or resp.headers.get("Retry-After"):
        # Being blocked or rate-limited: stop everything, don't retry or work around it.
        _stopped_reason = f"it refused or rate-limited a request (HTTP {resp.status_code})."
        raise FetchError(f"Nutrislice refused the request (HTTP {resp.status_code}). "
                         "Stopped all fetching instead of retrying.")
    resp.raise_for_status()
    return resp.json()


def fetching_stopped() -> bool:
    return _stopped_reason is not None


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

def load_schools(offline: bool = False) -> list[dict]:
    """Nutrislice's list of every SBU location and its stations (cached on disk for a week)."""
    path = config.CACHE_DIR / "schools.json"
    with _fetch_lock:
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
        raise FetchError("No saved list of dining locations, and live fetching is off or offline.")
    return wrapped["data"]


def list_stations(hall: str, offline: bool = False) -> list[Station]:
    """All active stations (Nutrislice "menu types") for a location ("East", "Roth Food Court"...)."""
    from src import locations  # imported here: locations imports this module
    schools = load_schools(offline=offline)
    try:
        loc = locations.get(hall, locations.from_schools(schools))
    except ValueError as exc:
        raise FetchError(str(exc)) from exc
    for school in schools:
        if school["id"] == loc.school_id:
            return [
                Station(loc.key, mt["id"], mt["slug"], mt["name"], loc.school_id)
                for mt in school.get("active_menu_types", [])
                if mt["slug"] not in config.SKIP_MENU_TYPE_SLUGS
            ]
    raise FetchError(f"{loc.name} isn't listed on Nutrislice right now.")


# --- menus --------------------------------------------------------------------

def station_cache_path(station: Station, start: date) -> Path:
    folder = re.sub(r"[^A-Za-z0-9_-]+", "_", station.hall)
    return config.CACHE_DIR / start.isoformat() / folder / f"{station.menu_type_id}-{station.slug}.json"


def fetch_station_week(station: Station, day: date, offline: bool = False) -> tuple[dict, str]:
    """Return (raw week JSON, source) where source is 'cache', 'network' or 'stale-cache'."""
    start = week_start(day)
    path = station_cache_path(station, start)
    with _fetch_lock:  # re-read inside the lock: another visitor may have just fetched it
        wrapped = _read_cache(path)
        if wrapped is not None and _fresh(wrapped, start + timedelta(days=6)):
            return wrapped["data"], "cache"
        if offline:
            if wrapped is not None:
                return wrapped["data"], "stale-cache"
            raise FetchError(f"No cached menu for {station.hall} {station.name} (offline).")

        school_id = station.school_id or config.HALLS[station.hall]["school_id"]
        url = config.WEEK_URL.format(school_id=school_id, menu_type_id=station.menu_type_id,
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
            # After a refusal/rate limit, the remaining stations come from cache only.
            offline = offline or fetching_stopped()
            continue
        out["sources"][station.name] = source
        for raw_day in week.get("days", []):
            if raw_day.get("date") == day.isoformat():
                out["stations"].append((station, raw_day))
    return out


if __name__ == "__main__":
    # Quick manual checks:
    #   python -m src.fetch_menu              -> today's East and West menus
    #   python -m src.fetch_menu --locations  -> every SBU location on Nutrislice
    #   python -m src.fetch_menu Roth         -> today's menu at one location
    import sys

    from src import locations

    if "--locations" in sys.argv:
        for loc in locations.all_locations(offline=False):
            print(f"{loc.key:40} id {loc.school_id:<6} {loc.kind:8} pays with {loc.paid_with}")
        raise SystemExit(0)
    today = date.today()
    for hall in sys.argv[1:] or list(config.HALLS):
        result = fetch_hall_day(hall, today)
        n = sum(len(d.get("menu_items", [])) for _, d in result["stations"])
        print(f"{hall}: {len(result['stations'])} stations, {n} raw rows, "
              f"sources={set(result['sources'].values())}, errors={result['errors']}")
