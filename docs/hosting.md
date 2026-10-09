# Hosting DineWolfie on Streamlit Community Cloud

On your own computer, DineWolfie uses your Claude plan login and saves memory to `prefs.json`. A public website can't use your Claude login, and Streamlit Cloud wipes saved files whenever the app restarts. So the hosted site uses:

| Need | Hosted solution |
|---|---|
| An AI engine | **Claude** (Anthropic API key) or **Groq** (free tier), picked in the sidebar |
| Keys | Visitors paste their own, **or** you put yours in Streamlit **Secrets** (only signed-in users can use them) |
| Accounts | **Sign in with Google** (`st.login`) |
| Memory that lasts | One row per user in a **Google Sheet** |
| Nutrition gaps | Optional **Tavily** web lookup, labeled as an estimate |

Guests who don't sign in still get a working app: their memory lives only in their browser tab.

Everything below is free. Set it up in this order; each part works on its own.

---

## 1. Keys for the AI engine (5 minutes)

- **Groq (free):** https://console.groq.com/keys → Create API key (starts with `gsk_`).
- **Anthropic (paid per use):** https://console.anthropic.com → API keys. We measured about **$0.47 per full-day plan** on the default model (Claude Opus 5.5). Claude Sonnet 5.5 costs half as much per token. To use it, add `DINEWOLFIE_MODEL = "claude-sonnet-5-5"` to Secrets.
- **Tavily (optional, free tier):** https://app.tavily.com → API key (starts with `tvly-`).

In Streamlit Cloud: your app → **⋮ → Settings → Secrets**, and paste:

```toml
hosted = true
GROQ_API_KEY = "gsk_..."
ANTHROPIC_API_KEY = "sk-ant-..."   # optional
TAVILY_API_KEY = "tvly-..."        # optional
allowed_emails = []                # e.g. ["you@stonybrook.edu", "teammate@stonybrook.edu"]
```

Until you set up sign-in (step 2), your keys are **not** used for guests. Guests have to paste their own. That stops strangers from spending your credits. To deliberately share with everyone, add `share_keys_with_everyone = true`.

## 2. Google sign-in (15 minutes)

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

## 3. Google Sheet for memory (15 minutes)

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

## 4. Check it

1. Save the secrets; the app restarts.
2. Open the site and click **Sign in with Google**.
3. Pick **Groq** in **AI engine**. It should say "Using the app's Groq key."
4. Change something under **Your memory** and click **Save memory**. A row appears in the sheet.
5. Press **Plan my day**.

A full template of every setting is in [`.streamlit/secrets.toml.example`](../.streamlit/secrets.toml.example).

## Notes

- **Groq's free tier has per-minute token limits.** If a plan stops with "rate-limiting", wait a minute. The Groq engine is told to prefer small searches to stay under the limit.
- **Phone notifications:** the hosted site never uses your `NTFY_TOPIC`. Each visitor types their own topic.
- **The 8 AM morning run** still runs on your own computer (Task Scheduler), not on Streamlit Cloud.
