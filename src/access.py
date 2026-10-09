"""Who may use which API key on the hosted site.

Keys can come from two places:
  * the visitor pastes their own key: kept in their browser session only, never saved;
  * the app owner's keys in Streamlit secrets: these cost the owner money, so by default
    only signed-in users (Google or an email account) may use them, and only emails on the
    allow list if there is one.
"""
from __future__ import annotations

YOURS, APP, SIGN_IN, NOT_ALLOWED, NONE = "yours", "app", "sign_in", "not_allowed", "none"


def choose_key(visitor_key: str | None, app_key: str | None, *, hosted: bool, auth_enabled: bool,
               signed_in: bool, email: str | None, allowed_emails: list[str] | None,
               open_to_all: bool = False) -> tuple[str | None, str]:
    """Return (key to use or None, why)."""
    if visitor_key and visitor_key.strip():
        return visitor_key.strip(), YOURS
    if not app_key:
        return None, NONE
    if not hosted or open_to_all:
        return app_key, APP  # running on your own computer, or you chose to share with everyone
    if not auth_enabled or not signed_in:
        return None, SIGN_IN
    allowed = {e.strip().lower() for e in (allowed_emails or []) if e.strip()}
    if allowed and (email or "").strip().lower() not in allowed:
        return None, NOT_ALLOWED
    return app_key, APP


def explain(source: str, provider: str) -> str:
    name = {"claude": "Anthropic", "groq": "Groq", "gemini": "Gemini", "cerebras": "Cerebras"}.get(provider, provider)
    return {
        YOURS: f"Using the {name} key you pasted (kept only in this browser tab).",
        APP: f"Using the app's {name} key.",
        SIGN_IN: f"Sign in to use the app's {name} key, or paste your own.",
        NOT_ALLOWED: f"Your account isn't on this app's list for the shared {name} key. Paste your own key.",
        NONE: f"No {name} key yet. Paste one above.",
    }[source]
