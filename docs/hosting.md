# Hosting DineWolfie on Streamlit Community Cloud

On your own computer, DineWolfie uses your Claude plan login and saves memory to `prefs.json`. A public website can't use your Claude login, and Streamlit Cloud wipes saved files whenever the app restarts. So the hosted site uses:

| Need | Hosted solution |
|---|---|
| An AI engine | **Claude** (Anthropic API key) or **Groq** (free tier), picked in the sidebar |
| Keys | Visitors paste their own, **or** you put yours in Streamlit **Secrets** (only signed-in users can use them) |
| Accounts | **Email + password** with **Supabase** (recommended), and/or **Sign in with Google** (`st.login`) |
| Memory that lasts | One row per account in a **Supabase** table (email accounts) or a **Google Sheet** (Google sign-in) |
| Nutrition gaps | Optional **Tavily** web lookup, labeled as an estimate |

Guests who don't sign in still get a working app: their memory lives only in their browser tab.

Everything below is free. Do step 1, then **either** step 2 (Supabase, about 10 minutes, one service) **or** steps 3 and 4 (Google, about 30 minutes, two setups). You can also do both: the sidebar then offers both ways to sign in.

---

## 1. Keys for the AI engine (5 minutes)

- **Groq (free):** https://console.groq.com/keys → Create API key (starts with `gsk_`).
- **Anthropic (paid per use):** https://console.anthropic.com → API keys. The default is **Claude Haiku 5.5 in fast mode**: about 28,000 input and 4,000 output tokens per full-day plan in our test, which is **under 1 cent** at Haiku's list price ($0.10 / $0.50 per million tokens). The old setup (Claude Opus 5.5, step-by-step loop) measured about $0.47 per plan. To use a bigger model for everyone, add `DINEWOLFIE_MODEL = "claude-sonnet-5-5"` to Secrets.
- **Tavily (optional, free tier):** https://app.tavily.com → API key (starts with `tvly-`).

In Streamlit Cloud: your app → **⋮ → Settings → Secrets**, and paste:

```toml
hosted = true
GROQ_API_KEY = "gsk_..."
ANTHROPIC_API_KEY = "sk-ant-..."   # optional
TAVILY_API_KEY = "tvly-..."        # optional
allowed_emails = []                # e.g. ["you@stonybrook.edu", "teammate@stonybrook.edu"]
```

Until you set up sign-in (step 2 or 3), your keys are **not** used for guests. Guests have to paste their own. That stops strangers from spending your credits. To deliberately share with everyone, add `share_keys_with_everyone = true`.

## 2. Email accounts with Supabase (10 minutes, recommended)

Supabase gives you sign-in and a database in one free project. People create an account with their email and a password, and their memory and past plans are saved to their own row.

1. Go to https://supabase.com, sign in (GitHub or Google works), and click **New project**. Name it `dinewolfie`, make up a database password (you won't need it in the app), pick the region closest to you (East US), and create it. It takes about a minute.
2. **Create the table.** In the project's left menu open **SQL Editor** → **New query**. Open [`supabase/schema.sql`](../supabase/schema.sql) from this repo, copy all of it, paste it in, and click **Run**. It should say "Success. No rows returned".
3. **Copy two values.** Click **Connect** at the top of the project page (or go to **Project Settings → API Keys**). Copy:
   - the **Project URL**, like `https://abcdefghijkl.supabase.co`
   - the **publishable key**, which starts with `sb_publishable_`. Older projects call it the **anon public** key; that works too.

   Never use the **secret** key (`sb_secret_...`) or the **service_role** key here. Those skip all the safety rules.
4. **Where the confirmation email points.** Go to **Authentication → URL Configuration** and set **Site URL** to `https://dinewolfie-gjdqzr6jrenmh73mpehbk6.streamlit.app`.
5. Add to Streamlit Secrets:

```toml
[supabase]
url = "https://abcdefghijkl.supabase.co"
key = "sb_publishable_..."
```

The sidebar now shows **Sign in** and **Create account**. A new user gets a confirmation email, clicks the link, comes back, and signs in.

**What keeps accounts private:** the app only has the publishable key, so the database itself enforces the rules (Row Level Security in `schema.sql`): a signed-in person can read and change only their own row; signed-out visitors can't read anything; nobody can delete rows through the app. We tested these rules against a real Postgres database.

**Things to know about Supabase's free plan:**

- **Only about 2 emails per hour** come from Supabase's built-in email sender, and that limit is shared by the whole project. If several people sign up at once (a demo, a class), the rest see "Too many sign-up emails". Two fixes:
  - Easiest: in **Authentication**, open the **Email** sign-in provider's settings and turn off **Confirm email**. New accounts work right away, but anyone can sign up with an email address they don't own. That's fine for food preferences. But if you use `allowed_emails` to protect your API keys, it no longer proves who someone is, so keep confirmation on in that case.
  - Better: connect your own email sender in **Authentication**'s settings (look for **SMTP Settings**; Resend's free plan works).
- **Free projects pause after about a week with no activity.** While paused, the sign-in form shows an error and nobody's saved memory loads; the app still works for guests. Restore it from the Supabase dashboard, and check it a day before any demo.
- **Signing in lasts for the browser tab.** Reloading the page signs you out, and you sign in again; your memory is still saved.
- There's no "forgot password" button yet. You can delete a user under **Authentication → Users** so they can sign up again; that also deletes their saved memory.

## 3. Google sign-in (15 minutes, optional)

1. Go to https://console.cloud.google.com, create a project called `DineWolfie` (top bar → project picker → **New project**).
2. **APIs & Services → OAuth consent screen**: choose **External**, app name `DineWolfie`, your email for support and developer contact. Under **Audience/Test users**, add your team's emails while the app is in "Testing".
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs: add **both**
     - `https://dinewolfie-gjdqzr6jrenmh73mpehbk6.streamlit.app/oauth2callback`
     - `http://localhost:8501/oauth2callback` (for testing on your computer)
   - Copy the **Client ID** and **Client secret**.
4. Make a cookie secret: any long random string. For example, run `python -c "import secrets; print(secrets.token_hex(32))"`.
5. Add to Secrets:

```toml
[auth]
redirect_uri = "https://dinewolfie-gjdqzr6jrenmh73mpehbk6.streamlit.app/oauth2callback"
cookie_secret = "the-long-random-string"
client_id = "....apps.googleusercontent.com"
client_secret = "GOCSPX-..."
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

A **Sign in with Google** button now appears at the top of the sidebar.

## 4. Google Sheet for memory of Google users (15 minutes, optional)

1. Create a new Google Sheet named **DineWolfie users**. Leave it empty; the app adds the header row.
2. Copy the sheet's ID from its URL: `https://docs.google.com/spreadsheets/d/`**`THIS-PART`**`/edit`.
3. In the same Google Cloud project: **APIs & Services → Library** → enable **Google Sheets API** and **Google Drive API**.
4. **IAM & Admin → Service Accounts → Create service account**, name `dinewolfie`. No roles needed. Open it → **Keys → Add key → JSON**. A `.json` file downloads. Keep it private and never commit it.
5. In the Google Sheet, click **Share** and add the service account's email (`dinewolfie@<project>.iam.gserviceaccount.com`) as **Editor**.
6. Add to Secrets. Copy each field from the downloaded JSON file; the `private_key` stays on one line with its `\n`s:

```toml
[gsheets]
sheet_id = "THIS-PART"
tab = "users"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "dinewolfie@....iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```

Each signed-in person gets one row: `user_id` (their email) | `name` | `prefs_json` | `history_json` | `updated_at`. Only the service account and people you share the sheet with can see it.

## 5. Check it

1. Save the secrets; the app restarts.
2. Open the site and **Create account** (or **Sign in with Google**).
3. Pick **Groq** in **AI engine**. It should say "Using the app's Groq key."
4. Change something under **Your memory** and click **Save memory**. A row appears in Supabase (**Table Editor → dinewolfie_memory**) or in the Google Sheet.
5. Press **Plan my day**. In fast mode it should finish in about 10 seconds.

A full template of every setting is in [`.streamlit/secrets.toml.example`](../.streamlit/secrets.toml.example).

## Notes

- **Groq's free tier has per-minute token limits** (about 8,000 tokens per minute for `gpt-oss-120b`), and they're shared by **everyone using your key**. One fast-mode plan uses roughly 4,000 to 5,000, so the whole site gets only one or two plans a minute per model. DineWolfie handles it in three steps: it switches to the next Groq model (each has its own limit; see `DINEWOLFIE_GROQ_FALLBACKS`), waits only if a model frees up within a few seconds, and otherwise makes the plan with the built-in planner and says so. **For a demo or many users, the real fix is Groq's paid Developer tier** (Settings → Billing): much higher limits for well under a cent per plan.
- **Live menus:** the first visitor of the day would wait for about 35 Nutrislice requests, so the app starts loading today's East and West menus in the background as soon as it starts. Streamlit Cloud forgets the cache when the app restarts, so the first plan after a restart can take longer.
- **Model choice on the hosted site:** visitors using your key always get the default model (so nobody can run up your bill with a bigger one). Visitors who paste their own key can pick any model in the list.
- **Phone notifications:** the hosted site never uses your `NTFY_TOPIC`. Each visitor types their own topic.
- **The 8 AM morning run** still runs on your own computer (Task Scheduler), not on Streamlit Cloud.
