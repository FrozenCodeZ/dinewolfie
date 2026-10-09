"""Every SBU dining location on Nutrislice, not just the two dining halls.

East and West (the all-you-care-to-eat halls, paid with a meal swipe) are fixed in config.HALLS.
Every other location (paid with dining dollars) is discovered from Nutrislice's list of locations,
so nothing has to be hard-coded and new places show up on their own. The list is cached on disk
for a week (src/fetch_menu.load_schools).

Roth Café is special: SBU lists its food by concept (Smash n' Shake, Fuze, Savor, Cocina Fresca,
Popeyes, ...), so on Nutrislice each concept may be its own location without "Roth" in the name.
Every location that mentions Roth or one of its concepts (config.ROTH_WORDS) is also gathered into
one "Roth Cafe" location, so "lunch at Roth" works. The single concepts stay pickable too.

Item ids carry their location: "E123" (East), "W123" (West), "L6401-123" (location 6401),
"LROTH-123" (the Roth Café group).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import config

DINE_IN, RETAIL = "dine_in", "retail"
ROTH_KEY, ROTH_CODE = "Roth Cafe", "LROTH-"
RETRY_AFTER_FAILURE_S = 120
# Words that don't identify a place on their own, so they don't count as "the goal named it".
_GENERIC = {"the", "east", "west", "side", "dine", "dine-in", "dining", "cafe", "café", "food", "court",
            "market", "marketplace", "kitchen", "grill", "express", "center", "hall", "stony", "brook", "at"}


@dataclass(frozen=True)
class Location:
    key: str        # what the agent and the app call it: "East", "West", "Roth Cafe", ...
    school_id: int
    slug: str
    name: str       # Nutrislice's full name (for a group: the names it gathers)
    code: str       # item id prefix
    kind: str       # dine_in | retail
    members: tuple[int, ...] = ()  # a group (Roth Cafe): the Nutrislice locations it gathers

    @property
    def paid_with(self) -> str:
        return "meal swipe" if self.kind == DINE_IN else "dining dollars"

    @property
    def school_ids(self) -> tuple[int, ...]:
        return self.members or (self.school_id,)


def builtin() -> list[Location]:
    return [Location(key, h["school_id"], h["slug"], h["name"], key[0], DINE_IN) for key, h in config.HALLS.items()]


def is_roth(name: str, slug: str = "") -> bool:
    text = f"{name} {slug}".lower().replace("é", "e")
    return any(word in text for word in config.ROTH_WORDS)


def from_schools(schools: list[dict]) -> list[Location]:
    """East and West, every other open location in Nutrislice's list, and the Roth Cafe group."""
    out = builtin()
    known = {loc.school_id for loc in out}
    roth = []
    for school in schools or []:
        sid, name = school.get("id"), (school.get("name") or "").strip()
        if not isinstance(sid, int) or sid in known or not name or not school.get("active_menu_types"):
            continue
        slug = school.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        kind = DINE_IN if "dine-in" in slug or "dine in" in name.lower() else RETAIL
        loc = Location(name, sid, slug, name, f"L{sid}-", kind)
        out.append(loc)
        known.add(sid)
        if kind == RETAIL and is_roth(name, slug):
            roth.append(loc)
    if roth and not any(loc.key.lower() == ROTH_KEY.lower() for loc in out):
        out.append(Location(ROTH_KEY, roth[0].school_id, "roth-cafe", " + ".join(r.name for r in roth),
                            ROTH_CODE, RETAIL, tuple(r.school_id for r in roth)))
    return out


_memo: dict = {}
last_error: str | None = None  # why the list of locations couldn't be loaded (shown in the app)


def all_locations(offline: bool | None = None) -> list[Location]:
    """Every known location. Without network only what's cached on disk, or just East and West."""
    global last_error
    from src.fetch_menu import FetchError, load_schools  # fetch_menu imports nothing from here
    if offline is None:
        offline = not config.LIVE_FETCH
    path = config.CACHE_DIR / "schools.json"
    stamp = (str(path), path.stat().st_mtime if path.exists() else None, offline)
    if _memo.get("stamp") == stamp and (not _memo.get("failed")  # read on almost every tool call
                                        or time.monotonic() - _memo["failed"] < RETRY_AFTER_FAILURE_S):
        return _memo["locations"]
    try:
        found, failed, last_error = from_schools(load_schools(offline=offline)), None, None
    except FetchError as exc:
        found, failed, last_error = builtin(), time.monotonic(), str(exc)
    stamp = (str(path), path.stat().st_mtime if path.exists() else None, offline)
    _memo.update(stamp=stamp, locations=found, failed=failed)
    return found


def get(name: str, locations: list[Location] | None = None) -> Location:
    """Find a location by its key: exact, then prefix, then any word ("roth" -> "Roth Cafe")."""
    locations = locations or all_locations()
    wanted = (name or "").strip().lower().replace("é", "e")
    if wanted:
        halls = {h.lower() for h in config.HALLS}
        for test in (lambda k: k == wanted,
                     lambda k: k in halls and wanted.startswith(k),  # "West Side Dine-In" -> West
                     lambda k: k.startswith(wanted),
                     lambda k: wanted in k):
            hits = [loc for loc in locations if test(loc.key.lower().replace("é", "e"))]
            if hits:
                return min(hits, key=lambda loc: len(loc.key))  # "East" beats "East Side Market"
    raise ValueError(f"Unknown location {name!r}. Use one of {[loc.key for loc in locations]}.")


def for_item_id(item_id: str, locations: list[Location] | None = None) -> Location | None:
    """The location an item id belongs to, by its prefix (longest prefix wins: "LROTH-" before "L")."""
    item_id = str(item_id).strip().upper()
    for loc in sorted(locations or all_locations(), key=lambda loc: -len(loc.code)):
        if item_id.startswith(loc.code.upper()):
            return loc
    return None


def named_in(text: str, locations: list[Location]) -> list[Location]:
    """Locations other than East/West that a goal mentions by a distinctive word ("Roth")."""
    words = set(re.findall(r"[a-z0-9']+", (text or "").lower().replace("é", "e")))
    hits = []
    for loc in locations:
        if loc.kind == DINE_IN and loc.key in config.HALLS:
            continue
        distinctive = [w for w in re.findall(r"[a-z0-9']+", loc.key.lower().replace("é", "e"))
                       if w not in _GENERIC and len(w) > 2]
        if distinctive and distinctive[0] in words:
            hits.append(loc)
    grouped = {sid for loc in hits if loc.members for sid in loc.members}
    return [loc for loc in hits if loc.members or loc.school_id not in grouped]  # the group covers its parts
