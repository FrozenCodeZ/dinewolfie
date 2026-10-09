"""Email + password accounts with Supabase Auth (the hosted site's "Create account" / "Sign in").

Why Supabase: Streamlit Cloud wipes files on every restart, so memory needs a database, and
Supabase gives sign-in plus a Postgres table in one free project. The table's Row Level Security
(supabase/schema.sql) lets each signed-in user read and write only their own row, so the app can
use the public "publishable" key.

Each browser session gets its own client (kept in st.session_state) because the client holds
that person's sign-in. Nothing here is shared between visitors.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

MIN_PASSWORD = 8
COOKIE = "dinewolfie_session"
REMEMBER_DAYS = 30


class AccountError(Exception):
    """A problem worth showing to the person signing in, in plain words."""


@dataclass
class Account:
    user_id: str
    email: str
    # Lets this browser stay signed in (saved in a cookie; Supabase swaps it for a new one each use).
    refresh_token: str | None = field(default=None, repr=False)


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
    return Account(str(res.user.id), res.user.email or email, res.session.refresh_token)


def sign_up(client, email: str, password: str) -> Account | None:
    """Create an account. Returns the signed-in Account, or None when Supabase first wants the
    person to confirm their email (the default): they confirm, then sign in."""
    email = _check(email, password)
    try:
        res = client.auth.sign_up({"email": email, "password": password})
    except Exception as exc:
        raise AccountError(friendly_error(exc)) from exc
    if res.user is not None and res.session is not None:  # "Confirm email" is off in Supabase
        return Account(str(res.user.id), res.user.email or email, res.session.refresh_token)
    return None


def restore(client, refresh_token: str) -> Account:
    """Sign back in from the token saved in this browser's cookie (page reloads, new tabs)."""
    try:
        res = client.auth.refresh_session(refresh_token)
    except Exception as exc:
        raise AccountError(friendly_error(exc)) from exc
    if res.user is None or res.session is None:
        raise AccountError("Your saved sign-in has expired. Sign in again.")
    return Account(str(res.user.id), res.user.email or "", res.session.refresh_token)


def cookie_script(refresh_token: str | None) -> str:
    """JavaScript that saves (or, with None, deletes) the sign-in cookie in this browser.
    Streamlit can read cookies but not write them, so the page does it."""
    if refresh_token:
        value, age = json.dumps(refresh_token).replace("<", "\\u003c"), REMEMBER_DAYS * 86400
        return (f"<script>document.cookie = '{COOKIE}=' + encodeURIComponent({value}) + "
                f"'; path=/; max-age={age}; SameSite=Lax' + (location.protocol === 'https:' ? '; Secure' : '');"
                "</script>")
    return f"<script>document.cookie = '{COOKIE}=; path=/; max-age=0; SameSite=Lax';</script>"


def sign_out(client) -> None:
    try:
        client.auth.sign_out()
    except Exception:  # signing out locally always works; a network error here doesn't matter
        pass
