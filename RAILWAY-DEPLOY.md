# Deploy on Railway — everything on Railway

One project, one service, one volume. No external database, no second host, no object
storage: the SQLite database and the uploaded documents both live on a Railway volume.

## 1. Your plan, and what changes when it does

Railway's trial is **one-time: $5 of credit, valid for up to 30 days**, and it **expires in 30
days whether or not you spend it**. When 30 days pass or the $5 is spent, the account
**automatically reverts to the Free plan: $1 of credit per month**, which does not roll over.

| | Trial | After it reverts (Free) |
|---|---|---|
| Credit | **$5, one-time, expires after 30 days** | **$1 per month** |
| Limits | 1 GB RAM, **shared** vCPU, 5 services/project, **3 volumes** | 0.5 GB RAM, 1 vCPU, 1 replica, **1 volume** |
| Volume size | 0.5 GB | 0.5 GB |
| Always-on app | ~$2–4/month — fits in the $5 | more than the credit → **the service must sleep** |

Usage prices are the same on both: RAM **$10/GB/month**, CPU **$20/vCPU/month**, volume
**$0.15/GB/month** (0.5 GB ≈ $0.08/month), egress $0.05/GB.

**Why bother with the sleeping settings now?** Because the switch is automatic and unattended:
an always-on service stops when the trial ends. Set it up once and that moment passes quietly.
During the trial you can leave Serverless off if you prefer instant responses.

## 2. The architecture

```
Railway project
└── service: pharmascan  (built from this repo's Dockerfile)
    ├── /app                     ← the code, wiped and rebuilt on every deploy
    └── /app/data  ← VOLUME      ← pharmascan.db + uploaded_notes/ + app.log
```

Set **`DATA_DIR=/app/data`** and mount the volume at **`/app/data`**. That one variable moves the
SQLite database, the uploaded documents *and* the log together. Without it the app writes beside
its code, and every redeploy quietly returns an empty vault.

Mount the volume at `/app/data`, **not at `/app`**: a mount at `/app` would shadow the
application code itself.

## 3. Deploy

1. **Push this code to your repository** (it is on GitHub already).
2. Railway → **New Project → Deploy from GitHub repo** → pick the repository. Railway finds the
   `Dockerfile` and builds with it; there is no config file to add. (I deliberately ship no
   `railway.json`: Railway deprecated Config-as-Code in favour of Infrastructure-as-Code, and it
   discovers the Dockerfile on its own.)
3. **Attach the volume**: service → right-click → **Attach Volume** → mount path **`/app/data`**.
4. **Variables** (service → Variables):

```dotenv
DATA_DIR=/app/data
STORAGE_BACKEND=disk
GROQ_API_KEY=sk-or-v1-...
GROQ_BASE_URL=https://openrouter.ai/api/v1
GROQ_MODEL=openai/gpt-oss-20b
GROQ_MAP_MODEL=openai/gpt-oss-20b
GROQ_SUMMARY_MODEL=openai/gpt-oss-20b
ADMIN_USERNAME=Poriotke
ADMIN_PASSWORD=<choose a password>
ACCESS_CODE=<a code your class can remember>
MAX_UPLOAD_SIZE_MB=25
```

5. **Generate a domain**: service → Settings → Networking → **Generate Domain**.
6. **Serverless** (optional but recommended — see §1): service → Settings → **Serverless**.
   With SQLite on a volume there is no outbound database traffic, so the service sleeps cleanly;
   the only outbound traffic is the AI call itself.

## 4. Verify

| Check | Expected |
|---|---|
| The app URL loads | the landing page |
| `/api/notes` | `{"total":0,"items":[]}` on a fresh volume |
| Upload a PDF → **Short notes** → **Generate** | real notes with page numbers |
| `/admin` → sign in → **Test connection** | your key's models are listed |
| **Redeploy** (Railway → Deploy → Redeploy), then reload | **the vault and your PDF are still there** ← the check that matters |
| Railway → Usage | during the trial, well under $5/30 days |

## 5. Traps

| Symptom | Cause |
|---|---|
| Vault empty after a redeploy | no volume attached, or `DATA_DIR` not set — the data was written beside the code |
| App fails to start after attaching a volume | the volume was mounted at `/app`, shadowing the code — use `/app/data` |
| Permission errors writing to the volume | a non-root image UID; set `RAILWAY_RUN_UID=0` |
| A minute of downtime on each redeploy | expected with a volume: Railway refuses to have two deployments mounted at once |
| `429` from the AI | an OpenRouter `:free` model — use `openai/gpt-oss-20b` |
| Network calls fail on the trial | **Limited Trial** (GitHub account unverified) restricts outbound access — check railway.com/verify |
| Service stopped mid-month | the credit is spent; it resumes next cycle. On the trial, note that Railway **deletes trial volumes 30 days after the credits expire** — see §6 |

## 6. Backups (worth two minutes)

Everything is on the volume, so that volume is the app's only copy. Railway supports **manual and
automated volume backups** (service → the volume → Backups), which keeps durability entirely
inside Railway. Take a manual backup once the class vault is populated, and keep a copy of any
export you make off-platform.

## 7. If you would rather use Railway's Postgres

Railway's Postgres plugin works too, and the app supports it without changes:

```dotenv
DATABASE_URL=postgresql+asyncpg://...   # anything Railway gives you; the scheme is rewritten
STORAGE_BACKEND=database                # documents in the database instead of the volume
DB_POOL_MODE=null                        # lets the service sleep: an idle pool is outbound traffic
```

Honest trade-off: a Postgres service **runs continuously**, so it also spends credit — on the
free plan's $1/month that is the difference between free and not. The volume path in §2 is
cheaper and simpler; this one buys you point-in-time recovery and no size ceiling near 0.5 GB.

## 8. When you outgrow free: Hobby, $5/month

Includes $5 of usage and a **5 GB volume** (resizable live, no downtime). Same project, same
volume, nothing to migrate — the service just stops sleeping and gets faster. That is the honest
recommendation once the class uses it daily; the free path is for getting it in front of students
without spending anything.

## 9. How this was verified

No Docker daemon exists in the environment this was built in, so the image was verified by doing
exactly what it does, on a clean copy of the repo:

- `pip install -r requirements.txt` into a fresh virtualenv — completed, so `requirements.txt` is
  complete for the container path;
- the image's own command, `uvicorn main:app --host 0.0.0.0 --port $PORT`, started the app:
  `/api/notes` → 200, `/` → 200;
- the full 23-check HTTP suite passed against that build (**23/23**);
- with `DATA_DIR` pointing at a directory standing in for the volume, the volume contained
  `pharmascan.db`, `uploaded_notes/` (20 documents) and `app.log`;
- the container was then killed and the image layer wiped (what a redeploy does): after restart
  the vault was intact, the document bytes were **identical**, and `/admin` still answered.
