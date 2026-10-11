"""Email accounts (Supabase Auth) and per-account memory, against a fake Supabase (no network)."""
import time
from types import SimpleNamespace

import pytest

import config
from src import accounts, storage


class FakeAuthError(Exception):
    def __init__(self, code, message="nope", status=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class FakeSupabase:
    """Just enough of supabase-py's Client. `db` is shared like a real project, and every query
    only sees the signed-in user's row, the way the RLS policies in supabase/schema.sql work."""

    def __init__(self, db=None, users=None, confirm_email=False, server=None):
        self.db = db if db is not None else {}
        self.users = users if users is not None else {}  # email -> (user_id, password)
        self.confirm_email, self.current, self.refreshes = confirm_email, None, 0
        # Supabase's side, shared by every client of the same project (tabs, devices).
        self.server = server if server is not None else {"issued": 0, "valid": {}, "used": {}, "access": {},
                                                          "refreshes": 0, "revoked": 0}
        self.auth = SimpleNamespace(sign_in_with_password=self._sign_in, sign_up=self._sign_up,
                                    sign_out=self._sign_out, get_session=self._get_session,
                                    refresh_session=self._refresh, set_session=self._set_session)

    def _sign_in(self, creds):
        uid, pw = self.users.get(creds["email"], (None, None))
        if uid is None or pw != creds["password"]:
            raise FakeAuthError("invalid_credentials")
        self.current = uid
        return SimpleNamespace(user=SimpleNamespace(id=uid, email=creds["email"]), session=self._new_session())

    def _sign_up(self, creds):
        if creds["email"] in self.users:
            raise FakeAuthError("user_already_exists")
        uid = f"uid-{len(self.users) + 1}"
        self.users[creds["email"]] = (uid, creds["password"])
        if self.confirm_email:
            return SimpleNamespace(user=SimpleNamespace(id=uid, email=creds["email"]), session=None)
        self.current = uid
        return SimpleNamespace(user=SimpleNamespace(id=uid, email=creds["email"]), session=self._new_session())

    def _sign_out(self, options=None):
        self.signed_out_scope = (options or {}).get("scope", "global")  # supabase-auth defaults to global
        self.current = None

    def _new_session(self):
        auth = self.server
        auth["issued"] += 1
        token, access = f"rt-{self.current}-{auth['issued']}", f"at-{self.current}-{auth['issued']}"
        auth["valid"][token] = self.current
        auth["access"][access] = (self.current, int(time.time()) + 3600)
        return SimpleNamespace(refresh_token=token, access_token=access, expires_at=int(time.time()) + 3600)

    def _refresh(self, token):
        auth = self.server
        auth["refreshes"] += 1
        uid = auth["valid"].pop(token, None)  # each token works once, like Supabase
        if uid is None:
            if token in auth["used"]:  # reuse of a spent token: Supabase revokes the whole sign-in
                auth["valid"] = {t: u for t, u in auth["valid"].items() if u != auth["used"][token]}
                auth["revoked"] += 1
            raise FakeAuthError("refresh_token_not_found", "Invalid Refresh Token", status=400)
        auth["used"][token] = uid
        self.current = uid
        email = next(e for e, (u, _) in self.users.items() if u == uid)
        return SimpleNamespace(user=SimpleNamespace(id=uid, email=email), session=self._new_session())

    def _set_session(self, access, refresh):
        uid, expires = self.server["access"].get(access, (None, 0))
        if uid is None or expires <= time.time():
            return self._refresh(refresh)
        self.current = uid
        email = next(e for e, (u, _) in self.users.items() if u == uid)
        session = SimpleNamespace(refresh_token=refresh, access_token=access, expires_at=expires)
        return SimpleNamespace(user=SimpleNamespace(id=uid, email=email), session=session)

    def _get_session(self):
        self.refreshes += 1
        return SimpleNamespace() if self.current else None

    def table(self, name):
        assert name == storage.SupabaseStore.TABLE
        return FakeQuery(self)


class FakeQuery:
    def __init__(self, client):
        self.client, self.filters, self.row = client, {}, None

    def select(self, columns):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def limit(self, n):
        return self

    def upsert(self, row, on_conflict=""):
        assert on_conflict == "user_id"
        self.row = row
        return self

    def execute(self):
        me = self.client.current
        if self.row is not None:  # insert/update: RLS "with check"
            if me is None or self.row["user_id"] != me:
                raise RuntimeError("new row violates row-level security policy")
            self.client.db[me] = {"prefs": {}, "history": [], **self.client.db.get(me, {}), **self.row}  # merge
            return SimpleNamespace(data=[self.row])
        wanted = self.filters.get("user_id")
        rows = [r for uid, r in self.client.db.items() if uid == me and (wanted is None or uid == wanted)]
        return SimpleNamespace(data=rows)


# --- sign-in and sign-up ------------------------------------------------------------------------

def test_sign_up_then_sign_in():
    sb = FakeSupabase()
    account = accounts.sign_up(sb, " Wolf@StonyBrook.edu ", "correct horse")
    assert account.email == "wolf@stonybrook.edu" and account.user_id == "uid-1"
    accounts.sign_out(sb)
    again = accounts.sign_in(sb, "wolf@stonybrook.edu", "correct horse")
    assert again.user_id == "uid-1"


def test_sign_up_waits_for_email_confirmation_when_supabase_asks():
    assert accounts.sign_up(FakeSupabase(confirm_email=True), "a@b.edu", "password1") is None


@pytest.mark.parametrize("email,password,message", [
    ("not-an-email", "password1", "Enter your email"),
    ("a@b.edu", "short", "at least"),
])
def test_bad_input_is_caught_before_calling_supabase(email, password, message):
    with pytest.raises(accounts.AccountError, match=message):
        accounts.sign_in(FakeSupabase(), email, password)


def test_supabase_errors_become_plain_words():
    sb = FakeSupabase(users={"a@b.edu": ("u1", "password1")})
    with pytest.raises(accounts.AccountError, match="don't match"):
        accounts.sign_in(sb, "a@b.edu", "wrong-password")
    with pytest.raises(accounts.AccountError, match="already an account"):
        accounts.sign_up(sb, "a@b.edu", "password2")
    assert "later" in accounts.friendly_error(FakeAuthError("over_email_send_rate_limit"))


# --- memory per account ---------------------------------------------------------------------------

def test_supabase_store_round_trip_and_isolation():
    db, users = {}, {"ana@x.edu": ("u-ana", "password1"), "ben@x.edu": ("u-ben", "password1")}
    ana_client = FakeSupabase(db, users)
    accounts.sign_in(ana_client, "ana@x.edu", "password1")
    ana = storage.SupabaseStore(ana_client, "u-ana")
    assert ana.load_prefs() is None  # new account: the app falls back to default prefs
    ana.save_prefs({"diet": ["vegan"]})
    ana.save_history([{"date": "2026-10-01", "plan": {}}])

    ben_client = FakeSupabase(db, users)
    accounts.sign_in(ben_client, "ben@x.edu", "password1")
    ben = storage.SupabaseStore(ben_client, "u-ben")
    assert ben.load_prefs() is None and ben.load_history() == []  # can't see Ana's row

    again = storage.SupabaseStore(ana_client, "u-ana")  # Ana in a new session
    assert again.load_prefs() == {"diet": ["vegan"]} and len(again.load_history()) == 1
    assert ana_client.refreshes >= 3  # every read/write first renews the sign-in if needed


def test_supabase_store_keeps_history_bounded_and_reports_save_failures():
    sb = FakeSupabase(users={"a@x.edu": ("u1", "password1")})
    accounts.sign_in(sb, "a@x.edu", "password1")
    store = storage.SupabaseStore(sb, "u1")
    store.save_history([{"date": f"d{i}", "plan": {}} for i in range(storage.HISTORY_LIMIT + 15)])
    assert len(sb.db["u1"]["history"]) == storage.HISTORY_LIMIT and sb.db["u1"]["history"][-1]["date"].endswith("74")
    sb.current = None  # signed out elsewhere: the database refuses the write
    with pytest.raises(RuntimeError, match="Couldn't save to your account"):
        store.save_prefs({"diet": []})


# --- the web app with email accounts switched on ----------------------------------------------------

def _app(monkeypatch, fake):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(accounts, "make_client", lambda url, key: fake)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60)
    at.secrets["hosted"] = True
    at.secrets["supabase"] = {"url": "https://example.supabase.co", "key": "sb_publishable_test"}
    at.secrets["GROQ_API_KEY"] = "gsk-owner"
    return at


def test_app_sign_in_unlocks_saved_memory_and_the_app_key(monkeypatch):
    fake = FakeSupabase(users={"wolf@stonybrook.edu": ("u-wolf", "password1")})
    at = _app(monkeypatch, fake)
    at.run()
    assert not at.exception
    at.sidebar.radio(key="provider").set_value("Groq").run()
    assert "Sign in to use the app's Groq key" in " ".join(c.value for c in at.caption)

    at.text_input(key="si_email").input("wolf@stonybrook.edu")
    at.text_input(key="si_password").input("password1")
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "Saved to your account" in captions
    assert "Using the app's Groq key" in captions
    assert any("wolf@stonybrook.edu" in m.value for m in at.markdown)
    assert at.selectbox(key="model_groq").disabled  # the owner's key runs the default model only

    # saving memory writes this account's row
    next(b for b in at.button if b.label == "Save memory").click().run()
    assert "u-wolf" in fake.db


def test_app_wrong_password_shows_a_plain_error(monkeypatch):
    fake = FakeSupabase(users={"wolf@stonybrook.edu": ("u-wolf", "password1")})
    at = _app(monkeypatch, fake)
    at.run()
    at.text_input(key="si_email").input("wolf@stonybrook.edu")
    at.text_input(key="si_password").input("not-my-password")
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert any("don't match" in err.value for err in at.error)
    assert "this browser tab only" in " ".join(c.value for c in at.caption)  # still a guest


def test_app_misconfigured_supabase_shows_an_error_not_a_crash(monkeypatch):
    def broken(url, key):
        raise ValueError("Invalid URL")
    at = _app(monkeypatch, None)
    monkeypatch.setattr(accounts, "make_client", broken)
    at.run()
    at.text_input(key="si_email").input("wolf@stonybrook.edu")
    at.text_input(key="si_password").input("password1")
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert not at.exception
    assert any("Invalid URL" in err.value for err in at.error)


# --- staying signed in across reloads -------------------------------------------------------------

def test_restore_signs_back_in_once_per_token():
    sb = FakeSupabase(users={"wolf@x.edu": ("u-wolf", "password1")})
    first = accounts.sign_in(sb, "wolf@x.edu", "password1")
    accounts._newest.clear(), accounts._family.clear()  # the server restarted: it only has the cookie
    again = accounts.restore(sb, first.refresh_token)
    assert again.user_id == "u-wolf" and again.email == "wolf@x.edu"
    assert again.refresh_token != first.refresh_token  # rotated, like Supabase does
    with pytest.raises(accounts.AccountError):
        accounts.restore(sb, first.refresh_token)  # the old one no longer works


def test_cookie_script_saves_and_clears_safely():
    saved = accounts.cookie_script('abc"</script><b>')
    assert "dinewolfie_session=" in saved and "max-age=2592000" in saved
    assert saved.count("</script>") == 1  # the token can't close the script tag early
    assert "\\u003c/script>" in saved
    assert "max-age=0" in accounts.cookie_script(None)


def test_app_sign_in_saves_the_stay_signed_in_cookie(monkeypatch):
    fake = FakeSupabase(users={"wolf@stonybrook.edu": ("u-wolf", "password1")})
    at = _app(monkeypatch, fake)
    at.run()
    at.text_input(key="si_email").input("wolf@stonybrook.edu")
    at.text_input(key="si_password").input("password1")
    next(b for b in at.button if b.label == "Sign in").click().run()
    scripts = [h.proto.body for h in at.get("html")]
    assert any("dinewolfie_session=" in body and "rt-u-wolf-1" in body for body in scripts)


# --- several tabs and devices (review findings 10 and 11) ----------------------------------------------

def _expire(*accts):
    """An hour passes: every access token and every session DineWolfie knows about has expired."""
    for acct in accts:
        acct.expires_at = 0
        if acct.family in accounts._newest:
            accounts._newest[acct.family] = accounts._newest[acct.family]._replace(expires_at=0)


def _expire_server(server):
    server["access"] = {k: (u, 0) for k, (u, _) in server["access"].items()}


def _two_tabs():
    users = {"wolf@x.edu": ("u-wolf", "password1")}
    tab_a = FakeSupabase(users=users)
    tab_b = FakeSupabase(users=users, server=tab_a.server, db=tab_a.db)
    return tab_a, tab_b


def test_a_new_tab_shares_the_sign_in_instead_of_spending_the_token():
    tab_a, tab_b = _two_tabs()
    a = accounts.sign_in(tab_a, "wolf@x.edu", "password1")
    b = accounts.restore(tab_b, a.refresh_token)  # tab B reads the cookie tab A saved
    assert b.user_id == "u-wolf" and tab_a.server["refreshes"] == 0  # nothing rotated, nothing to reuse
    assert b.refresh_token == a.refresh_token


def test_expired_tabs_refresh_with_the_newest_token_so_supabase_never_sees_a_reuse():
    tab_a, tab_b = _two_tabs()
    a = accounts.sign_in(tab_a, "wolf@x.edu", "password1")
    b = accounts.restore(tab_b, a.refresh_token)
    _expire(a, b)  # an hour later
    _expire_server(tab_a.server)
    assert accounts.ensure_fresh(tab_b, b) is True  # B renews: the token rotates once
    assert accounts.ensure_fresh(tab_a, a) is True  # A takes B's new sign-in instead of reusing its old token
    assert a.refresh_token == b.refresh_token and tab_a.server["revoked"] == 0
    store = storage.SupabaseStore(tab_a, "u-wolf", account=a)
    store.save_prefs({"diet": ["vegan"]})  # and A can still save
    assert tab_a.db["u-wolf"]["prefs"] == {"diet": ["vegan"]}


def test_two_devices_keep_separate_sign_ins():
    phone, laptop = _two_tabs()
    p = accounts.sign_in(phone, "wolf@x.edu", "password1")
    lap = accounts.sign_in(laptop, "wolf@x.edu", "password1")
    _expire(lap)
    _expire_server(laptop.server)
    accounts.ensure_fresh(laptop, lap)
    assert lap.refresh_token != p.refresh_token  # the laptop didn't take over the phone's sign-in
    accounts.sign_out(laptop, lap)
    assert laptop.signed_out_scope == "local"  # finding 11: only this browser signs out
    assert phone.server["valid"].get(p.refresh_token) == "u-wolf"


def test_restore_tells_an_expired_sign_in_from_a_network_problem(monkeypatch):
    sb = FakeSupabase(users={"wolf@x.edu": ("u-wolf", "password1")})
    with pytest.raises(accounts.AccountError) as gone:
        accounts.restore(sb, "rt-never-issued")
    assert gone.value.expired  # the app forgets the cookie

    def offline(token):
        raise ConnectionError("timed out")
    sb.auth.refresh_session = offline
    with pytest.raises(accounts.AccountError) as blip:
        accounts.restore(sb, "rt-whatever")
    assert not blip.value.expired  # the app keeps the cookie and tries again later
