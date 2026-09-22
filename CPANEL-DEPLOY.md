# Deploying PharmaScanKE on cPanel (Phusion Passenger)

**Verdict: yes — but only if your cPanel has "Setup Python App".** If it does not,
see [If your cPanel has no Python support](#if-your-cpanel-has-no-python-support).

cPanel's Python hosting runs apps through **Phusion Passenger**, which speaks **WSGI**.
PharmaScanKE is a **FastAPI** app, which is **ASGI**. Those are not interchangeable —
the `passenger_wsgi.py` in this repo is the bridge (`a2wsgi`, pure Python, no compiler
needed), and it also fixes two Passenger-specific traps that would otherwise 500 or hang
the app. Railway stays untouched: it still uses `Procfile` + uvicorn.

---

## 0. Does my cPanel support this?

cPanel → **Software** → look for **Setup Python App** (some themes: *Application Manager*,
or *Python Selector* on Plesk/other panels). Then:

- **Yes** → continue below. You need Python **3.10 or newer** in the version dropdown
  (3.11 is the sweet spot: every dependency has a ready-made wheel, so nothing compiles).
  Do not pick 3.9 — `groq` (the AI SDK) requires `>=3.10`, so the install would fail there.
- **No** → [If your cPanel has no Python support](#if-your-cpanel-has-no-python-support).

---

## 1. What this port changes (and why)

| File | Change | Why it matters |
|---|---|---|
| `passenger_wsgi.py` | **new** — WSGI entry point | cPanel/Passenger cannot execute an ASGI app directly. Wraps `main:app` with `a2wsgi.ASGIMiddleware`, creates the DB schema, and builds the adapter **lazily per process**. |
| `core/config.py` | `.env` path is now **absolute** | The old relative `".env"` only loads when the working directory happens to be the app root. Under Passenger it may not be — the app then answers `503 GROQ_API_KEY environment variable is not set` while your `.env` sits right there. |
| `requirements-cpanel.txt` | **new** | Adds `a2wsgi`; drops `uvicorn[standard]` (Passenger *is* the web server, and uvloop/httptools/watchfiles need a compiler when no wheel matches your Python). |
| `cpanel_check.py` | **new** — pre-flight self-test | Turns "it 500s" into a named cause: Python version, `.env`, writable folders, schema, WAL, every route, and outbound HTTPS to `api.groq.com`. |
| `.gitignore` | ignores `tmp/`, `stderr.log` | Passenger scratch files. |

The two traps the entry point handles (both proven, not guessed):

1. **No lifespan event.** `main.py` creates its tables in a FastAPI *lifespan* hook.
   `a2wsgi` only ever sends `http` scopes, so that hook never runs. Without an explicit
   `init_db()`, every DB request returns `500 … no such table: resources`.
2. **Passenger forks.** The default spawn method ("smart") imports your startup file in a
   preloader and **forks** the app processes from it — and a fork keeps only the calling
   thread. `a2wsgi.ASGIMiddleware` starts a background thread with an event loop, so an
   adapter built at import time is **dead in every child** (the first request just hangs).
   This repo therefore builds it lazily, per PID.

---

## 2. Deploy — step by step

### 2.1 Upload the code (keep it OUT of `public_html`)

Target: `~/pharmascan` — a plain folder in your home directory, **not** the web root.
Everything sensitive (`.env`, `pharmascan.db`, the Python source) then sits outside the
document root and cannot be fetched over HTTP.

**Option A — cPanel Terminal (recommended):**

```bash
cd ~
git clone https://github.com/ashyamctommy-eng/Beta-PharmaScan.git pharmascan
cd pharmascan
```

You will be on `main`; the cPanel files are in the bundle you were given. If you received
this as a `.zip` instead, upload it and extract:

```bash
cd ~ && mkdir -p pharmascan && cd pharmascan
unzip ~/Beta-PharmaScan-cpanel.zip
```

**Option B — File Manager:** create `pharmascan`, upload the zip, Extract.

### 2.2 Create the Python application

cPanel → **Setup Python App** → **Create Application**:

| Field | Value |
|---|---|
| Python version | **3.11** (3.10–3.13 all work; 3.9 will *not* — `groq` needs `>=3.10`) |
| Application root | `pharmascan` |
| Application URL | a subdomain is cleanest, e.g. `scan.yourdomain.com` (create it under **Domains** first), or the domain itself |
| Application startup file | `passenger_wsgi.py` |
| Application entry point | `application` |

cPanel provisions a virtualenv and writes the Passenger directives into that URL's
document root `.htaccess`. **Note the virtualenv path it prints** — you need it next.

### 2.3 Install the dependencies

Use the **Terminal** button of the Python App (or cPanel → Terminal), then:

```bash
source ~/virtualenv/pharmascan/3.11/bin/activate   # ← exact path from the UI
cd ~/pharmascan
pip install --upgrade pip
pip install -r requirements-cpanel.txt
```

Expect one to two minutes. Everything installs from a wheel — no compiler, no
`gcc`, no `python3-devel`.

### 2.4 Create the `.env`

```bash
cd ~/pharmascan
cp .env.example .env
chmod 600 .env
nano .env          # paste your key, save with Ctrl+O, Ctrl+X
```

```dotenv
GROQ_API_KEY=gsk_your_real_key_here
DEBUG=false
```

Get a key at <https://console.groq.com>. `DATABASE_URL` can stay commented out — the
default (`~/pharmascan/pharmascan.db`) is already correct, and the DB plus the
`uploaded_notes/` folder are what persist on a cPanel host (that is the real advantage
over Railway's free tier).

### 2.5 Pre-flight check, then start

```bash
cd ~/pharmascan
python cpanel_check.py          # venv still active
```

Fix anything marked `FAIL` (the output names the cause), then:

```bash
mkdir -p tmp && touch tmp/restart.txt
```

**`touch tmp/restart.txt` is how you restart the app** after every code or `.env` change —
Passenger only notices then. First load may take a few seconds.

### 2.6 Verify from the outside

Open your URL and check, in order:

1. `/` → the vault page renders.
2. `/api/notes` → `{"total":0,"items":[]}` (proves Python + SQLite + Passenger).
3. `/docs` → FastAPI's Swagger UI (proves the ASGI bridge).
4. Upload a small PDF → it appears in the card grid; click download.
5. Run an analysis → a real Groq answer (this is the only step that needs a live key).

All five, on the same box, are what `cpanel_check.py` + local testing cover between them.

---

## 3. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| First request hangs ~60s then 500 | Adapter built before Passenger forked (only if you edited `passenger_wsgi.py` back to a module-level `application = ASGIMiddleware(app)`) | Keep the lazy `PassengerASGIAdapter`. If it still hangs: docroot `.htaccess` → add `PassengerSpawnMethod direct`, restart. |
| `500 … no such table: resources` on `/api/notes` | Schema was never created — the lifespan hook didn't run, or a stale startup file is deployed | Confirm `passenger_wsgi.py` is in the app root and is the configured startup file; `touch tmp/restart.txt`. |
| `/api/analyze` → `503 GROQ_API_KEY environment variable is not set` | `.env` missing, unreadable, or not where the app looks | `python cpanel_check.py` — it prints whether the key loaded and from which path. `chmod 600 .env`, owned by your user. |
| `/api/analyze` → `502 AI analysis service is temporarily unavailable` | No outbound HTTPS from the host, or the key was revoked | `cpanel_check.py` does a live `api.groq.com` call and distinguishes 401 (bad key) from a blocked connection. Some hosts firewall outbound TLS. |
| Everything 404s / shows a directory listing | Application URL doesn't match the docroot that has the Passenger `.htaccess` | Re-check the Application URL in Setup Python App, or re-create the app. |
| Big PDF → `413` **from the server** (not JSON) | Apache `LimitRequestBody` | App limit is 50 MB; raise the server limit in the docroot `.htaccess`: `LimitRequestBody 104857600` (100 MB), restart. |
| `database is locked` under load | Several Passenger processes writing SQLite at once | Restart with fewer processes (`PassengerMaxPoolSize 2` in the docroot `.htaccess`); or move to MySQL/MariaDB. |
| `pip install` tries to compile `greenlet` | No wheel for that Python version | Pick Python 3.10–3.13, or ask your host to install `gcc`/`python3-devel`. |
| Nothing helps | — | Read the real error: cPanel → **Metrics → Errors**, or the Python App's log viewer. Passenger prints tracebacks there, and `~/pharmascan/stderr.log` if present. |

---

## 4. What is verified, and what only your host can confirm

Verified here, on this exact code:

- The app runs under a **real WSGI server**: 3 worker processes × 4 threads via the
  `passenger_wsgi.py` adapter — **23/23 HTTP checks pass**, no server-side errors.
  Covers `/`, `/api/notes`, `/api/notes/stats`, filters, upload + filename sanitising,
  byte-identical download of the uploaded file, 400/413/422/404 validation paths,
  delete, 36 concurrent reads and 20 concurrent uploads (all persisted), `/openapi.json`.
- `/api/analyze` success path through the adapter (stubbed Groq): 200, 17 KB Markdown
  response, PubChem images and the Pharmacy180 block intact; the failure path returns a
  clean `502` JSON instead of a traceback.
- The **fork trap** is real: an eager adapter left the first forked request unanswered
  until the watchdog killed it; the lazy adapter returned `200 OK`.
- The **`.env` bug** is real: with the working directory outside the app root, the old
  relative path loaded nothing (→ `503`); the absolute path loads the key.
- The app's own DB layer needed **no changes** — the original pooled engine handled
  3 concurrent worker processes with 20 simultaneous uploads and zero lock errors.
- `python -m compileall` clean. `requirements-cpanel.txt` resolves to **13/13 prebuilt wheels on
  CPython 3.10, 3.11, 3.12 and 3.13** (x86_64) — so no compiler is needed on your host. It does
  *not* resolve on 3.9: `groq==1.5.0` declares `requires_python >=3.10`.

Only your host can confirm: that "Setup Python App" exists, the Python version offered,
and whether outbound HTTPS is open (step 2.5 answers the last two).

---

## 5. Keeping Railway

Nothing here breaks it: Railway reads `Procfile`, cPanel reads `passenger_wsgi.py`.
`requirements.txt` is unchanged. One caveat — a Railway filesystem is ephemeral, so its
SQLite DB and uploaded files vanish on redeploy; that is the strongest reason to move
this app to cPanel (or any host with a real disk).

---

## 6. If your cPanel has no Python support

In rough order of effort:

1. **Ask the host to enable "Setup Python App"** — many shared plans have the feature
   available on request or on a higher tier; it is a single toggle for them.
2. **Move to a Python host with a persistent disk** (the DB and PDFs must survive
   restarts). Render/Koyeb/Fly-style free tiers work but need a paid disk for real
   persistence; PythonAnywhere's free tier blocks outbound HTTP, which breaks Groq.
3. **Port the app to PHP** — the API surface is small (upload, list, filter, stats,
   delete, analyze) and cPanel runs PHP natively; PHP calls Groq with cURL. Genuinely
   bulletproof on cheap shared hosting, and about the same size as this bridge. Ask me
   and I'll do it end to end (I've already shipped a PHP+SQLite API on cPanel for you).

---

## 7. Uninstall / start over

```bash
# stop serving it
cd ~/pharmascan && rm -f tmp/restart.txt
# then cPanel → Setup Python App → your app → Destroy
# and (only if you want the data gone too)
rm -rf ~/pharmascan
```
