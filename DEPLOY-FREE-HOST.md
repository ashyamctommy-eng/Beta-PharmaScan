# Deploy on a free host (Render + Neon)

For hosts with an **ephemeral filesystem**: Render, Koyeb, rollout.host free tiers — and
Railway when you don't want a volume. Nothing here needs a disk, because with
`STORAGE_BACKEND=database` the documents, the summaries and the token ledger all live in
the database.

Verified end to end: after killing the app and deleting every local file, a fresh process
still served the vault, the identical document bytes and the cached notes.

---

## 1. What you get, honestly

| | |
|---|---|
| Cost | **$0** |
| Cold start | **Yes** — the service sleeps after ~15 idle minutes; the next visit takes 30-60 s |
| Memory | 512 MB (comfortable for this app) |
| Outbound network | Allowed (only SMTP ports are blocked), so the AI calls work |
| Storage | The database free tier (~0.5 GB) — hundreds of lecture-note PDFs, not a textbook library |
| Scale | One instance, which is fine |

## 2. Database: Neon (not Render's)

Render's own free Postgres **expires after 30 days**; Neon's free tier does not (it
auto-suspends after a few minutes idle and resumes on the next query). Supabase's free
tier pauses after 7 days of no traffic, so Neon is the better fit for a quiet class vault.

1. Create a free Neon project (`neon.com`) → copy the **connection string**.
2. Convert it to the async driver this app uses by prefixing `postgresql+asyncpg://`:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@ep-xxx.region.aws.neon.tech/neondb?ssl=require
```

## 3. Deploy on Render

`render.yaml` in this repo is a Blueprint that already sets `STORAGE_BACKEND=database`,
the OpenRouter endpoint, the models and the token budgets.

1. Render → **New → Blueprint** → connect this repository.
2. When prompted, fill the values marked `sync: false`:

```dotenv
DATABASE_URL=postgresql+asyncpg://...            # from step 2
GROQ_API_KEY=sk-or-v1-...                        # your OpenRouter key
ADMIN_USERNAME=Poriotke
ADMIN_PASSWORD=<something you choose>
ACCESS_CODE=<a code your class can remember>
```

3. Deploy. The first build takes a few minutes; the health check is `/api/notes`.

## 4. Verify

| Check | What it proves |
|---|---|
| `https://your-app.onrender.com/` loads | the container started |
| `/api/notes` → `{"total":0,...}` | the app reached Neon |
| `/admin` → sign in → **Test connection** | the key works and shows the models you can use |
| Upload a PDF → **Short notes** → **Generate** | the whole pipeline, with the document in the database |
| Wait 20 minutes, come back, download the PDF | **the point of this setup** — a cold start loses nothing |

## 5. Traps worth knowing

- **Don't use OpenRouter `:free` models** — they answer `429` constantly. `openai/gpt-oss-20b`
  costs about $0.0008 for a six-page summary.
- **Logs live in Render**, not on disk: the app also writes `app.log` (which is ephemeral
  here), so read Render's **Logs** tab — it captures stdout/stderr.
- **Keep `MAX_UPLOAD_SIZE_MB` at 25** on the free database tier.
- **Postgres is the whole deployment.** Back it up with Neon's own export if the vault matters.
- The service sleeping is normal; it is not a crash.

## 6. Moving off the free tier later

Nothing here locks you in. To move to a host with a real disk (cPanel with Python, a VPS,
PythonAnywhere, or Railway with a volume):

```bash
pg_dump "$DATABASE_URL" > pharmascan.sql     # take the data with you
```

then set `STORAGE_BACKEND=disk` on the new host and import the dump. The code and the
admin panel are identical on both.
