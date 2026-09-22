# PharmaScanKE

Study vault for pharmacy students: upload lecture notes (**PDF, DOCX, PPTX**), get **AI-written
short notes**, and ask questions about a document. FastAPI + SQLite/PostgreSQL + any
OpenAI-compatible provider.

- **Vault** — uploads per subject and semester; list, download, delete.
- **Short notes** — outline-first pipeline, page citations, cached per document.
- **Ask questions** — `/api/analyze` explains a topic from your notes.
- **Admin panel** (`/admin`) — sign-in, live model list, settings, token usage.
- **Access code** — students unlock once per device.
- **Cost controls** — rolling 24-hour token budgets and kill switches, so a leaked key can't
  run up a bill.

---

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then set GROQ_API_KEY
uvicorn main:app --reload   # http://127.0.0.1:8000
```

Python **3.10+** (3.11/3.12 recommended). The database and folders are created on first run.

---

## Deploy on Railway

One project, one service, one volume. **The app configures itself** from Railway's environment,
so you only paste four variables and attach the volume.

**1. Create it** — Railway → **New Project → Deploy from GitHub repo** → this repository. It finds
the `Dockerfile` and builds with it.

**2. Attach a volume** — on the service: right-click → **Attach Volume** → mount path:

```
/app/data
```

That exact path. (`/app` would cover the application code and the service would not start.) No
variable is needed for it — the app reads Railway's `RAILWAY_VOLUME_MOUNT_PATH` and puts the
SQLite database, the uploaded documents and the log there together.

**3. Variables** — service → Variables:

```dotenv
GROQ_API_KEY=sk-or-v1-...            # OpenRouter (sk-or-v1-...) or Groq (gsk_...)
ADMIN_USERNAME=admin                 # who signs in at /admin
ADMIN_PASSWORD=<choose a long one>   # without it the panel stays closed
ACCESS_CODE=<a code your class can remember>   # without it the AI features are open to anyone
```

That is all four. There is no database URL, storage mode, data directory, pool mode or model list
to set: the app detects them (see below).

**4. Domain and sleep** — Settings → Networking → **Generate Domain**; Settings → **Serverless**
→ on. Leave the healthcheck path unset (a periodic ping would keep it awake).

**5. Check it** — open Railway → **Logs**. The app says what it detected, and what is still
missing:

```
PharmaScanKE is starting — configuration detected from the environment:
  Data directory : /app/data   (volume detected — database, uploads and log live here)
  Documents      : on the volume   (STORAGE_BACKEND=disk)
  Connections    : one per request — lets the host sleep
  AI             : OpenRouter key sk-or-v1-a…cdef · model openai/gpt-oss-20b
  Admin panel    : enabled for 'admin'
  Student access : code set — students unlock once
  Fix these     : - ACCESS_CODE is not set: the AI features are open to anyone with the link
```

Then: upload a PDF → **Short notes** → **Generate**, and finally **redeploy** and reload — your
vault and document must still be there. That last check is what proves the volume is really in use.

**Prefer PostgreSQL to a volume?** Add a PostgreSQL service and set
`DATABASE_URL=${{Postgres.DATABASE_PRIVATE_URL}}` in the app service. Documents then live inside
the database and no volume is needed. On the free plan the **volume is cheaper**, because a
database service never sleeps.

Full detail — what is auto-detected, the trial → free timeline, backups, traps, and how the build
was verified: **[RAILWAY-DEPLOY.md](RAILWAY-DEPLOY.md)**.

---

## What you don't configure

The app reads the host and fills these in; anything you set yourself wins:

| Detected | From |
|---|---|
| Data directory | `RAILWAY_VOLUME_MOUNT_PATH` (set when you attach a volume) |
| Database | `DATABASE_PRIVATE_URL` / `DATABASE_URL` / `POSTGRES_URL` / `PG*` |
| Where documents go | a volume → the volume; a managed database → the database |
| Connection pooling | on Railway → one connection per request, so it can sleep |
| AI provider and models | your key's prefix: `sk-or-` → OpenRouter, `gsk_` → Groq |

Other hosts (cPanel, VPS, any container, a stateless host with no disk) are covered in
[CPANEL-DEPLOY.md](CPANEL-DEPLOY.md) and the tables in `.env.example`.

---

## Docs

| File | Contents |
|---|---|
| **[RAILWAY-DEPLOY.md](RAILWAY-DEPLOY.md)** | Railway deployment, auto-detection, backups, traps. |
| **[ADMIN-PANEL.md](ADMIN-PANEL.md)** | Panel authentication, settings, usage dashboard, access gate. |
| **[PDF-SUMMARY.md](PDF-SUMMARY.md)** | The short-notes pipeline, caching and token budgets. |
| **[CPANEL-DEPLOY.md](CPANEL-DEPLOY.md)** | Shared hosting via Passenger/WSGI. |

## Tests

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .
```

140 tests: extraction, the summary pipeline and its caching and budgets, storage backends, auth,
settings precedence, database-URL normalisation, and host auto-detection.

## Known limitations

- **One shared access code**, not per-student accounts. The app-wide token budget is what protects
  your API key.
- **The per-device allowance is spoofable**; the app-wide daily budget is the hard ceiling.
- **The vault is only as durable as the volume it sits on** — take volume backups.
