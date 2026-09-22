# PharmaScanKE

A study vault for pharmacy students: upload lecture notes (**PDF, DOCX, PPTX**), get
**AI-written short notes** you can revise from, and ask questions about a document. Built with
FastAPI, SQLite or PostgreSQL, and any OpenAI-compatible AI provider.

Everything you need to run it is in this repository — no external services are required.

---

## What it does

| Feature | Detail |
|---|---|
| **Document vault** | Upload lecture notes per subject and semester; list, search and delete them. |
| **Question answering** | `/api/analyze` explains a topic or answers a question about your notes. |
| **Short notes** | The summary pipeline reads a document and produces structured study notes with page citations, "remember these five", and likely exam traps. |
| **Cost controls** | Rolling 24-hour token budgets (app-wide *and* per device), a section ceiling per run, and a kill switch — so a leaked API key cannot run up a bill. |
| **Admin panel** | `/admin`: sign-in, live model list from your key, editable settings, and a token-usage dashboard. |
| **Student access gate** | Lock the AI features behind a class access code, unlocked once per device. |
| **Host-portable storage** | Documents and database can live on disk, on a mounted volume, or inside the database (stateless). |

Short notes are **outline-first**: the pipeline maps the document, then expands each section, so
long documents stay within a token budget and a failed run resumes instead of starting over.
Results are cached per document hash, so a second student asking for the same file pays nothing.

---

## Requirements

- **Python 3.10 or newer** (`groq==1.5.0` declares `>=3.10`); 3.11 or 3.12 recommended.
- An API key from **Groq** (`gsk_...`) or **OpenRouter** (`sk-or-v1-...`), or any
  OpenAI-compatible endpoint.

---

## Quick start (local)

```bash
git clone https://github.com/ashyamctommy-eng/Beta-PharmaScan.git
cd Beta-PharmaScan
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env and set GROQ_API_KEY
uvicorn main:app --reload   # http://127.0.0.1:8000
```

The database and any uploaded documents are created on first run. Nothing else to migrate.

### OpenRouter (or any OpenAI-compatible provider)

Set the base URL and a model your key can actually use:

```dotenv
GROQ_API_KEY=sk-or-v1-...
GROQ_BASE_URL=https://openrouter.ai/api/v1
GROQ_MODEL=openai/gpt-oss-20b
```

> Avoid OpenRouter's `:free` model variants: they are heavily rate-limited upstream and mostly
> answer `429`. The paid `gpt-oss` models cost a fraction of a cent per document.

Check what **your** key can use before changing models — the admin panel's **Test connection**
button lists every model your key can see.

---

## Configuration

All settings are environment variables (or entries in `.env`). On a managed host **most of them
are inferred** — the database, `DATA_DIR`, `STORAGE_BACKEND`, `DB_POOL_MODE` and the AI provider
are all detected (see RAILWAY-DEPLOY.md §2), and an explicit setting always wins. In practice you
set the AI key, the admin credentials and the access code.

The full annotated list lives in [`.env.example`](.env.example); the ones that matter most:

### Core

| Variable | Default | Notes |
|---|---|---|
| `GROQ_API_KEY` | — | **Required.** Your Groq or OpenRouter key. |
| `GROQ_BASE_URL` | *(empty)* | Empty = Groq's own API. Set for OpenRouter/other gateways. |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Main model. |
| `GROQ_MAP_MODEL`, `GROQ_SUMMARY_MODEL` | *(empty)* | Per-stage models for short notes; empty = `GROQ_MODEL`. |
| `DEBUG` | `false` | Verbose logging. |
| `PORT` | `8000` | Set by the host on Railway/Render/Heroku — do not set it yourself there. |

### Storage and database

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | beside the code | Where the app's own files live. **Set this to a mounted volume** on a host that wipes the filesystem on redeploy. It relocates the SQLite database, `uploaded_notes/` and `app.log` together. |
| `DATABASE_URL` | SQLite file | Any `postgres://` / `postgresql://` URL is rewritten automatically into what asyncpg accepts (`+asyncpg`, `sslmode=` → `ssl=`), so paste what your host gives you. |
| `STORAGE_BACKEND` | `disk` | `disk` = documents in `uploaded_notes/`; `database` = documents inside the database (stateless hosts). |
| `DB_POOL_MODE` | `default` | `null` opens a connection per request. Needed on hosts that sleep idle services, because an idle pool counts as outbound traffic. |
| `MAX_UPLOAD_SIZE_MB` | `50` | Lower it to `25` on hosts with a small disk or database. |

### Admin panel

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Panel user name. |
| `ADMIN_PASSWORD_HASH` | *(empty)* | **Preferred.** From `python -m core.auth hash`. |
| `ADMIN_PASSWORD` | *(empty)* | Plaintext alternative, hashed in memory at startup. |
| `SESSION_SECRET` | derived | `python -m core.auth secret`. Without it, changing the password signs everyone out. |

With **no** password set the panel stays closed — there is no default password, by design.

```bash
python -m core.auth hash      # paste into ADMIN_PASSWORD_HASH
python -m core.auth secret    # paste into SESSION_SECRET
python -m core.auth check     # report how authentication is configured
```

### Access gate and budgets

| Variable | Default | Notes |
|---|---|---|
| `ACCESS_CODE` | *(empty)* | Empty = AI features are open. Set a class code to gate them. |
| `ANALYZE_ENABLED` | `true` | Kill switch for question answering. |
| `SUMMARISE_ENABLED` | `true` | Kill switch for short notes. |
| `SUMMARISE_DAILY_TOKEN_BUDGET` | `150000` | **App-wide** ceiling, rolling 24 h. This is the real protection for your key. |
| `SUMMARISE_PER_IP_DAILY_TOKENS` | `30000` | Per-device ceiling, rolling 24 h. |
| `SUMMARISE_CALLS_PER_REQUEST` | `3` | Model calls per HTTP request, to keep shared hosts from timing out. |
| `SUMMARISE_MAX_SECTIONS` | `24` | Sections expanded per run. |

---

## Deployment

### Railway — about two minutes

Connect the repository, add **one** of the two state options, paste **four** variables. The app
works out the rest for itself: the database, where documents go, how connections are pooled and
which AI provider your key belongs to.

```
1. New Project  →  Deploy from GitHub repo   (Railway finds the Dockerfile)
2. Add ONE of:  a volume mounted at /app/data   (cheapest)
                a PostgreSQL service            (easiest, includes backups)
3. Variables:   GROQ_API_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, ACCESS_CODE
4. Generate a domain, then enable Serverless (Settings).
```

```dotenv
GROQ_API_KEY=sk-or-v1-...            # Groq (gsk_...) or OpenRouter (sk-or-v1-...)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<choose a long one>
ACCESS_CODE=<a code your class can remember>
```

Nothing else is required — there is no `DATA_DIR`, `STORAGE_BACKEND` or `DB_POOL_MODE` to set.
Every deploy logs what it detected and what is still missing, so on Railway open **Logs**:

```
  Data directory : /app/data   (volume detected — database, uploads and log live here)
  Documents      : on the volume   (STORAGE_BACKEND=disk)
  Connections    : one per request — lets the host sleep   (DB_POOL_MODE=null)
  AI             : OpenRouter key sk-or-v1-a…cdef · model openai/gpt-oss-20b
  Fix these     : - ACCESS_CODE is not set: the AI features are open to anyone with the link
```

**Verify:** upload a PDF → **Short notes** → **Generate**, then **redeploy** and reload — the vault
and your document must still be there. That last check proves the volume mount (or the database)
is right; it is what fails if state ended up on the container's filesystem.

Full detail — what is auto-detected, volume vs PostgreSQL, the trial → free timeline, backups and
the traps: **[RAILWAY-DEPLOY.md](RAILWAY-DEPLOY.md)**.

### Other targets

| Target | How | Notes |
|---|---|---|
| **Any container host** | `docker build -t pharmascan . && docker run -p 8000:8000 --env-file .env pharmascan` | Requires a **persistent** disk or a managed database; most free tiers wipe the filesystem. |
| **cPanel with Python** | Uvicorn behind Phusion Passenger via `passenger_wsgi.py`; `requirements-cpanel.txt` | No "Setup Python App" needed. See **[CPANEL-DEPLOY.md](CPANEL-DEPLOY.md)**. |
| **PythonAnywhere / VPS** | Same as cPanel, or plain uvicorn behind nginx | A real disk means `STORAGE_BACKEND=disk` works unchanged. |
| **Stateless host** (no volume) | `STORAGE_BACKEND=database` + `DB_POOL_MODE=null` with a managed Postgres | Documents live in the database; nothing depends on local disk. |

A first-run sanity check for any host:

```bash
python cpanel_check.py     # environment, database, AI connectivity, writable paths
```

---

## API

The interactive docs are at `/docs`; these are the endpoints the UI uses.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The web UI. |
| `GET` | `/admin` | The admin panel. |
| `POST` | `/api/upload` | Upload a document (multipart). |
| `GET` | `/api/notes` | List the vault (`?subject=&semester=`). |
| `GET` | `/api/notes/stats` | Totals by subject and semester. |
| `GET` | `/api/notes/{id}/file` | Download an uploaded document. |
| `DELETE` | `/api/notes/{id}` | Delete a resource and its stored file. |
| `POST` | `/api/analyze` | Ask a question / analyse notes. |
| `GET` | `/api/summarise/{id}/preview` | Free document preview (no AI cost). |
| `GET` | `/api/summarise/{id}` | Cached notes, or progress if still running. |
| `POST` | `/api/summarise/{id}` | Generate or resume short notes (bounded work per call). |
| `POST` | `/api/unlock` | Unlock the AI features with the access code. |
| `POST` | `/api/admin/login`, `/api/admin/logout` | Panel session. |
| `GET`/`POST`/`DELETE` | `/api/admin/settings` | Read, save, revert settings. |
| `POST` | `/api/admin/settings/test` | Live check against your provider. |
| `GET` | `/api/admin/usage` | Token usage and vault totals. |

Settings changed in the panel take effect immediately — no redeploy. Precedence is
**panel → server (env) → built-in default**, and each key can be reverted to the server value.

---

## Tests

```bash
pip install -r requirements-dev.txt      # fpdf2, for generating fixtures
python -m unittest discover -s tests -t .
```

140 tests: extraction (PDF/DOCX/PPTX), the summary pipeline and its caching and budgets,
storage backends, auth and sessions, settings precedence, database-URL normalisation, and host
auto-detection (volume, injected database, pooling, provider inference).
The HTTP suites used during development (`wsgi_smoke.py`, `summary_http_test.py`,
`admin_http_test.py`) live outside the repository because they need a running server.

---

## Documentation in this repository

| File | Contents |
|---|---|
| **[RAILWAY-DEPLOY.md](RAILWAY-DEPLOY.md)** | Railway deployment, the volume architecture, the trial → free timeline, backups, traps, and how the build was verified. |
| **[ADMIN-PANEL.md](ADMIN-PANEL.md)** | Admin panel: authentication, settings, the usage dashboard, and the access gate. |
| **[PDF-SUMMARY.md](PDF-SUMMARY.md)** | The short-notes pipeline: stages, caching, token budgets, measured costs. |
| **[CPANEL-DEPLOY.md](CPANEL-DEPLOY.md)** | Shared hosting: Passenger/WSGI setup, permissions, and the container-less path. |

---

## Known limitations

Stated plainly, because they matter if you run this for a real class:

- **One shared access code**, not per-student accounts — anyone with the code is in. The
  app-wide token budget is what actually protects your API key.
- **The per-device token allowance is spoofable** (a determined student can change their
  apparent client identity). The app-wide daily budget is the hard ceiling.
- **The login throttle is per process.** On a host running multiple workers it is per worker.
- **The document vault is only as durable as the volume it sits on.** Take volume backups.
- **Small-disk hosts**: keep `MAX_UPLOAD_SIZE_MB` low (25) and treat the vault as a few hundred
  lecture notes, not a textbook library.
