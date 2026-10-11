"""Email + password accounts with Supabase Auth (the hosted site's "Create account" / "Sign in").

Why Supabase: Streamlit Cloud wipes files on every restart, so memory needs a database, and
Supabase gives sign-in plus a Postgres table in one free project. The table's Row Level Security
(supabase/schema.sql) lets each signed-in user read and write only their own row, so the app can
use the public "publishable" key.

Each browser session gets its own client (kept in st.session_state) because the client holds
that person's sign-in. Nothing here is shared between visitors.

Several tabs, one sign-in: Supabase gives a new refresh token each time one is used and, if an
old one is ever used again, signs that person out everywhere. Every tab of a browser starts from
the same cookie, so tabs that each renewed on their own would trip that alarm. Instead this server
remembers the newest session of each sign-in ("family": everything descended from one sign-in),
and a tab that needs a session takes that one, renewing only when it has really expired. Different
browsers and devices are different families and never share.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import NamedTuple

MIN_PASSWORD = 8
COOKIE = "dinewolfie_session"
REMEMBER_DAYS = 30
EARLY_S = 60  # renew this long before the access token runs out


class AccountError(Exception):
    """A problem worth showing to the person signing in, in plain words.

    expired=True: the saved sign-in is no longer valid (forget it). False: probably a network or
    service problem (keep it and try again later)."""

    def __init__(self, message: str, expired: bool = False):
        super().__init__(message)
        self.expired = expired


@dataclass
class Account:
    user_id: str
    email: str
    # Lets this browser stay signed in (saved in a cookie; Supabase swaps it for a new one each use).
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: int | None = None  # when this tab's access token runs out (Unix time)
    family: str = ""               # which sign-in this belongs to (see the module docstring)


class _Session(NamedTuple):
    access_token: str
    refresh_token: str
    expires_at: int
    previous: str | None  # the refresh token just before this one (a tab may not have seen the swap yet)
    used_at: float


_newest: dict[str, _Session] = {}    # family -> its newest session, shared by that browser's tabs
_family: dict[str, str] = {}         # refresh token -> family (tokens this server has handed out)
_signed_out: dict[str, float] = {}   # families that signed out -> when (other tabs then sign out too)
_guard = threading.Lock()
_LOCKS = tuple(threading.Lock() for _ in range(64))  # a fixed pool: junk cookies can't grow it
MAX_TOKENS, MAX_FAMILIES = 4000, 1000
IDLE_S = REMEMBER_DAYS * 86400  # forget a sign-in nobody used for this long


def _lock_for(family: str) -> threading.Lock:
    return _LOCKS[hash(family) % len(_LOCKS)]


def _family_of(token: str) -> str:
    """The family a cookie's token belongs to. Unknown tokens (e.g. after a restart) get one, and the
    same one for every tab that brings that token, so those tabs don't renew it in parallel."""
    with _guard:
        family = _family.get(token)
        if family is None:
            family = _family[token] = uuid.uuid4().hex
            _trim()
        return family


def _trim() -> None:
    """Keep the maps small (call with _guard held). Old tokens and idle sign-ins are useless anyway."""
    for token in list(_family)[: max(0, len(_family) - MAX_TOKENS)]:
        _family.pop(token, None)
    now = time.time()
    for family in [f for f, s in _newest.items() if now - s.used_at > IDLE_S]:
        _newest.pop(family, None)
    for family in sorted(_newest, key=lambda f: _newest[f].used_at)[: max(0, len(_newest) - MAX_FAMILIES)]:
        _newest.pop(family, None)
    for family in [f for f, at in _signed_out.items() if now - at > IDLE_S]:
        _signed_out.pop(family, None)


def _account(res, email: str, family: str, used: str | None = None) -> Account:
    """Turn a Supabase auth response into an Account and remember it as the family's newest session.
    `used`: the refresh token that was just swapped for this one (it stays acceptable, see restore)."""
    session = res.session
    expires = int(getattr(session, "expires_at", 0) or 0) or None
    account = Account(str(res.user.id), res.user.email or email, session.refresh_token, expires, family)
    access = getattr(session, "access_token", None)
    with _guard:
        if access and session.refresh_token:
            old = _newest.get(family)
            if old and old.refresh_token == session.refresh_token:
                previous = old.previous      # same session taken over: nothing was swapped
            elif old:
                previous = old.refresh_token
            else:
                previous = used if used != session.refresh_token else None
            _newest[family] = _Session(access, session.refresh_token, expires or 0, previous, time.time())
        if session.refresh_token:
            _family[session.refresh_token] = family
        _trim()
    return account


def _check_signed_in(family: str) -> None:
    with _guard:
        if family in _signed_out:
            raise AccountError("You signed out in another tab. Sign in again.", expired=True)


def _forget_family(family: str) -> None:
    with _guard:
        _newest.pop(family, None)


def _rejected(exc: Exception) -> bool:
    """Supabase said no (bad or spent token), as opposed to a network or server problem."""
    status = getattr(exc, "status", None)
    if status in (408, 429):  # timeout or too many requests: try again later
        return False
    return isinstance(status, int) and 400 <= status < 500 or getattr(exc, "code", None) in (
        "refresh_token_not_found", "refresh_token_already_used", "session_not_found", "session_expired")


def make_client(url: str, key: str):
    """A fresh Supabase client for one browser session."""
    from supabase import ClientOptions, create_client  # only needed when accounts are switched on

    # No background refresh thread per visitor: storage.SupabaseStore refreshes the sign-in when it
    # has expired (tokens last an hour) right before it reads or writes.
    return create_client(url, key, options=ClientOptions(auto_refresh_token=False))


FRIENDLY = {
    "invalid_credentials": "That email and password don't match. Try again, or create an account.",
    "email_not_confirmed": "Confirm your email first: open the link we sent you, then sign in here.",
    "user_already_exists": "There's already an account with that email. Sign in instead.",
    "email_exists": "There's already an account with that email. Sign in instead.",
    "weak_password": f"Pick a stronger password (at least {MIN_PASSWORD} characters).",
    "email_address_invalid": "That email address doesn't look right.",
    "validation_failed": "That email address doesn't look right.",
    "signup_disabled": "New accounts are switched off for this app right now.",
    "email_provider_disabled": "Email sign-in is switched off for this app right now.",
    "over_email_send_rate_limit": "Too many sign-up emails were sent in the last hour. Try again later, "
                                  "or continue as a guest for now.",
    "over_request_rate_limit": "Too many tries. Wait a minute and try again.",
}


def friendly_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code in FRIENDLY:
        return FRIENDLY[code]
    text = str(getattr(exc, "message", "") or exc)
    if "connect" in text.lower() or "timed out" in text.lower():
        return "Couldn't reach the account service. Check your connection, or continue as a guest."
    return f"Couldn't do that: {text}"


def _check(email: str, password: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AccountError("Enter your email address.")
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters.")
    return email


def sign_in(client, email: str, password: str) -> Account:
    email = _check(email, password)
    try:
        res = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:
        raise AccountError(friendly_error(exc)) from exc
    if res.user is None or res.session is None:
        raise AccountError("Sign-in didn't go through. Try again.")
    return _account(res, email, uuid.uuid4().hex)


def sign_up(client, email: str, password: str) -> Account | None:
    """Create an account. Returns the signed-in Account, or None when Supabase first wants the
    person to confirm their email (the default): they confirm, then sign in."""
    email = _check(email, password)
    try:
        res = client.auth.sign_up({"email": email, "password": password})
    except Exception as exc:
        raise AccountError(friendly_error(exc)) from exc
    if res.user is not None and res.session is not None:  # "Confirm email" is off in Supabase
        return _account(res, email, uuid.uuid4().hex)
    return None


def restore(client, refresh_token: str) -> Account:
    """Sign back in from the token saved in this browser's cookie (page reloads, new tabs).

    If another tab already renewed this sign-in, take its newest session instead of using the
    cookie's older token (which Supabase would treat as stolen). Only the newest token, or the one
    just before it, gets that shortcut: any older token goes to Supabase to judge."""
    family = _family_of(refresh_token)
    with _lock_for(family):
        _check_signed_in(family)
        try:
            res = _take_newest(client, family, refresh_token) or client.auth.refresh_session(refresh_token)
        except Exception as exc:
            if _rejected(exc):
                _forget_family(family)
            raise AccountError(friendly_error(exc), expired=_rejected(exc)) from exc
        if res is None or res.user is None or res.session is None:
            raise AccountError("Your saved sign-in has expired. Sign in again.", expired=True)
        return _account(res, "", family, used=refresh_token)


def _take_newest(client, family: str, presented: str | None = None):
    """Use the family's newest session if it's still good (no new token), else renew it once.
    `presented`: a token from a cookie, which must be the newest or the one just before it."""
    with _guard:
        newest = _newest.get(family)
    if newest is None or (presented is not None and presented not in (newest.refresh_token, newest.previous)):
        return None
    if newest.expires_at - EARLY_S > time.time():
        return client.auth.set_session(newest.access_token, newest.refresh_token)
    return client.auth.refresh_session(newest.refresh_token)


def ensure_fresh(client, account: Account) -> bool:
    """Make sure this tab's sign-in is valid before reading or writing. True if its refresh token
    changed (the app then updates the cookie). Raises AccountError(expired=True) if it's gone,
    including right after this browser signed out in another tab."""
    family = account.family or uuid.uuid4().hex
    _check_signed_in(family)
    if account.expires_at and account.expires_at - EARLY_S > time.time():
        return False
    with _lock_for(family):
        try:
            res = _take_newest(client, family) if account.family else None
            used = None if res is not None else account.refresh_token
            if res is None:
                res = client.auth.refresh_session(account.refresh_token)
        except Exception as exc:
            if _rejected(exc):
                _forget_family(family)
            raise AccountError(friendly_error(exc), expired=_rejected(exc)) from exc
        if res is None or res.session is None:
            raise AccountError("Your sign-in has expired. Sign in again.", expired=True)
        fresh = _account(res, account.email, family, used=used)
    changed = fresh.refresh_token != account.refresh_token
    account.refresh_token, account.expires_at, account.family = fresh.refresh_token, fresh.expires_at, family
    return changed


def cookie_script(refresh_token: str | None, only_if: str | None = None) -> str:
    """JavaScript that saves (or, with None, deletes) the sign-in cookie in this browser.
    Streamlit can read cookies but not write them, so the page does it. only_if: delete only while the
    cookie still holds this token, so a tab whose sign-in ended can't wipe a newer sign-in made in
    another tab."""
    if refresh_token:
        value, age = json.dumps(refresh_token).replace("<", "\\u003c"), REMEMBER_DAYS * 86400
        return (f"<script>document.cookie = '{COOKIE}=' + encodeURIComponent({value}) + "
                f"'; path=/; max-age={age}; SameSite=Lax' + (location.protocol === 'https:' ? '; Secure' : '');"
                "</script>")
    clear = f"document.cookie = '{COOKIE}=; path=/; max-age=0; SameSite=Lax';"
    if not isinstance(only_if, str) or not only_if:
        return f"<script>{clear}</script>"
    mine = json.dumps(only_if).replace("<", "\\u003c")
    return (f"<script>(function () {{ var m = document.cookie.match(/(?:^|; ){COOKIE}=([^;]*)/); if (!m) return; "
            f"var v; try {{ v = decodeURIComponent(m[1]); }} catch (e) {{ v = null; }} "
            f"if (v === null || v === {mine}) {{ {clear} }} }})();</script>")  # a junk cookie is cleared too


def sign_out(client, account: Account | None = None) -> bool:
    """Sign out in this browser only ("local"): your phone and other computers stay signed in.
    This browser's other tabs sign out on their next step too. False if Supabase couldn't be told
    (the session then ends on its own when it expires)."""
    if account is not None and account.family:
        with _lock_for(account.family), _guard:
            _newest.pop(account.family, None)
            _signed_out[account.family] = time.time()
    try:
        client.auth.sign_out({"scope": "local"})
    except Exception:  # signing out here still works; Supabase just didn't hear about it
        return False
    return True
