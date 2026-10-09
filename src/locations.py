"""Every SBU dining location on Nutrislice, not just the two dining halls.

East and West (the all-you-care-to-eat halls, paid with a meal swipe) are fixed in config.HALLS.
Every other location (Roth Food Court and the rest, paid with dining dollars) is discovered from
Nutrislice's list of locations, so nothing has to be hard-coded and new places show up on their
own. The list is cached on disk for a week (src/fetch_menu.load_schools).

Item ids carry their location: "E123" (East), "W123" (West), "L6401-123" (school 6401).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import config

DINE_IN, RETAIL = "dine_in", "retail"
# Words that don't identify a place on their own, so they don't count as "the goal named it".
_GENERIC = {"the", "east", "west", "side", "dine", "dine-in", "dining", "cafe", "café", "food", "court",
            "market", "marketplace", "kitchen", "grill", "express", "center", "hall", "stony", "brook"}


@dataclass(frozen=True)
class Location:
    key: str        # what the agent and the app call it: "East", "West", "Roth Food Court"
    school_id: int
    slug: str
    name: str       # Nutrislice's full name
    code: str       # item id prefix
    kind: str       # dine_in | retail

    @property
    def paid_with(self) -> str:
        return "meal swipe" if self.kind == DINE_IN else "dining dollars"


def builtin() -> list[Location]:
    return [Location(key, h["school_id"], h["slug"], h["name"], key[0], DINE_IN) for key, h in config.HALLS.items()]


def from_schools(schools: list[dict]) -> list[Location]:
    """East and West plus every other location in Nutrislice's schools list."""
    out = builtin()
    known = {loc.school_id for loc in out}
    for school in schools or []:
        sid, name = school.get("id"), (school.get("name") or "").strip()
        if not isinstance(sid, int) or sid in known or not name or not school.get("active_menu_types"):
            continue
        slug = school.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        kind = DINE_IN if "dine-in" in slug or "dine in" in name.lower() else RETAIL
        out.append(Location(name, sid, slug, name, f"L{sid}-", kind))
        known.add(sid)
    return out


_memo: dict = {}


def all_locations(offline: bool | None = None) -> list[Location]:
    """Every known location. Without network only what's cached on disk, or just East and West."""
    from src.fetch_menu import FetchError, load_schools  # fetch_menu imports nothing from here
    if offline is None:
        offline = not config.LIVE_FETCH
    path = config.CACHE_DIR / "schools.json"
    stamp = (str(path), path.stat().st_mtime if path.exists() else None, offline)
    if _memo.get("stamp") == stamp:  # read on almost every tool call, so keep it in memory
        return _memo["locations"]
    try:
        found = from_schools(load_schools(offline=offline))
    except FetchError:
        found = builtin()
    stamp = (str(path), path.stat().st_mtime if path.exists() else None, offline)
    _memo.update(stamp=stamp, locations=found)
    return found


def get(name: str, locations: list[Location] | None = None) -> Location:
    """Find a location by its key: exact, then prefix, then any word ("roth" -> "Roth Food Court")."""
    locations = locations or all_locations()
    wanted = (name or "").strip().lower()
    if wanted:
        halls = {h.lower() for h in config.HALLS}
        for test in (lambda k: k == wanted,
                     lambda k: k in halls and wanted.startswith(k),  # "West Side Dine-In" -> West
                     lambda k: k.startswith(wanted),
                     lambda k: wanted in k):
            hits = [loc for loc in locations if test(loc.key.lower())]
            if hits:
                return min(hits, key=lambda loc: len(loc.key))  # "East" beats "East Side Market"
    raise ValueError(f"Unknown location {name!r}. Use one of {[loc.key for loc in locations]}.")


def for_item_id(item_id: str, locations: list[Location] | None = None) -> Location | None:
    item_id = str(item_id).strip().upper()
    match = re.match(r"L(\d+)-", item_id)
    for loc in locations or all_locations():
        if match and loc.school_id == int(match.group(1)):
            return loc
        if not match and loc.code.upper() == item_id[:1] and len(loc.code) == 1:
            return loc
    return None


def named_in(text: str, locations: list[Location]) -> list[Location]:
    """Locations other than East/West that a goal mentions by a distinctive word ("Roth")."""
    words = set(re.findall(r"[a-z0-9']+", (text or "").lower()))
    hits = []
    for loc in locations:
        if loc.kind == DINE_IN and loc.key in config.HALLS:
            continue
        distinctive = [w for w in re.findall(r"[a-z0-9']+", loc.key.lower()) if w not in _GENERIC and len(w) > 2]
        if distinctive and distinctive[0] in words:
            hits.append(loc)
    return hits
