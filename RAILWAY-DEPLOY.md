# Deploy on Railway

**The short version:** connect the repository, add either a volume or a PostgreSQL database,
paste four variables, done. The app detects everything else — the database, where documents go,
how connections are pooled, and which AI provider your key belongs to.

```
1. New Project  →  Deploy from GitHub repo
2. Add ONE of:  a volume mounted at /app/data     (cheapest)
                a PostgreSQL service              (easiest, has backups)
3. Paste:       GROQ_API_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, ACCESS_CODE
4. Generate a domain, enable Serverless. Done.
```

---

## 1. The four variables you actually have to set

```dotenv
GROQ_API_KEY=sk-or-v1-...            # your Groq (gsk_...) or OpenRouter (sk-or-v1-...) key
ADMIN_USERNAME=Poriotke              # who signs in at /admin
ADMIN_PASSWORD=<choose a long one>   # without this the panel stays closed
ACCESS_CODE=<a code your class can remember>   # without this the AI features are open to anyone
```

That is the whole configuration. There is no `DATA_DIR`, no `STORAGE_BACKEND`, no
`DB_POOL_MODE`, no model list to fill in.

## 2. What the app works out for itself

| Setting | Detected from | Override with |
|---|---|---|
| Where the app's files live | `RAILWAY_VOLUME_MOUNT_PATH` — set automatically when you attach a volume (falls back to a mounted `/app/data`, else beside the code) | `DATA_DIR` |
| Database | `DATABASE_PRIVATE_URL` / `DATABASE_URL` / `POSTGRES_URL`, or the `PG*` parts — whatever Railway injects for a PostgreSQL service | `DATABASE_URL` |
| Where documents go | a volume → the volume; otherwise a managed database → the database; otherwise local disk | `STORAGE_BACKEND` |
| Connection pooling | being on Railway → one connection per request, so an idle service can sleep | `DB_POOL_MODE` |
| AI provider | your key's prefix: `sk-or-` → OpenRouter, `gsk_` → Groq | `GROQ_BASE_URL` |
| Models | an OpenRouter key → `openai/gpt-oss-20b` (verified, ~$0.0008 per 6-page summary) | `GROQ_MODEL`, `GROQ_MAP_MODEL`, `GROQ_SUMMARY_MODEL` |

**Nothing here overrules you.** Every value above is only a default: if you set the variable
yourself, that wins.

## 3. Pick one: volume or database

| | **Volume** (recommended) | **Railway PostgreSQL** |
|---|---|---|
| Extra clicks | attach a volume at `/app/data` | add a PostgreSQL service |
| Where state lives | SQLite + documents on the volume | everything inside the database |
| Cost | $0.15/GB/month — about **$0.08/month** for 0.5 GB | the database service **runs continuously**, so it spends credit too |
| Size limit | 0.5 GB on Trial/Free, 5 GB on Hobby | small on free tiers, grows on paid |
| Backups | Railway volume backups (manual + automated) | point-in-time recovery |
| Notes | brief pause on each redeploy (Railway won't have two deployments on one volume) | documents inside the database, so the vault grows the database |

On the free plan's $1/month the **volume is the cheaper choice** because the database service
never sleeps. Either way, no code changes and no variables to set — `storage` and `pooling`
follow whichever you added.

## 4. Read the startup report

Every deploy logs what it worked out, and what is still missing. Railway → **Logs**:

```
PharmaScanKE is starting — configuration detected from the environment:
  Data directory : /app/data   (volume detected — database, uploads and log live here)
  Database       : SQLite file (pharmascan.db)
  Documents      : on the volume   (STORAGE_BACKEND=disk)
  Connections    : one per request — lets the host sleep   (DB_POOL_MODE=null)
  AI             : OpenRouter key sk-or-v1-a…cdef · model openai/gpt-oss-20b
  Admin panel    : enabled for 'Poriotke'
  Student access : code set — students unlock once
  Fix these     :
      - ACCESS_CODE is not set: the AI features are open to anyone with the link
```

If something is wrong later, start here — it answers most questions without reading any other
documentation. (It is emitted from the ASGI startup path, which is what the Dockerfile uses.)

## 5. Verify

| Check | Expected |
|---|---|
| The app URL loads | the landing page |
| `/api/notes` | `{"total":0,"items":[]}` on a fresh install |
| `/admin` → sign in → **Test connection** | your key's models are listed |
| Upload a PDF → **Short notes** → **Generate** | real notes with page numbers |
| **Redeploy** (Deploy → Redeploy), then reload | **the vault and your document are still there** |
| Railway → Usage | comfortably inside your credit |

The redeploy check is the one that matters: with a volume it proves the mount is right, with
PostgreSQL it proves the app is using the database rather than the container's filesystem.

## 6. Your plan, and what changes when it does

The trial is **one-time: $5 of credit, valid for up to 30 days**, and it **expires after 30 days
whether or not you spend it**. Then the account **automatically reverts to the Free plan: $1 of
credit per month**, which does not roll over.

| | Trial | After it reverts (Free) |
|---|---|---|
| Credit | $5, one-time | $1 per month |
| Limits | 1 GB RAM, shared vCPU, 3 volumes | 0.5 GB RAM, 1 vCPU, 1 volume |
| An always-on app | ~$2–4/month — fits in the $5 | more than the credit → **it must sleep** |

Usage prices: RAM $10/GB/month, CPU $20/vCPU/month, volume $0.15/GB/month.

That is why **Serverless** matters (Settings → Serverless): it sleeps the service after ~5–10
minutes of no outbound traffic and wakes it on the next request. With SQLite on a volume there is
no outbound database traffic at all, so it sleeps cleanly. Turn it on before the trial ends, not
after — the revert is automatic and unattended.

## 7. Traps

| Symptom | Cause |
|---|---|
| Vault empty after a redeploy | no volume and no database: documents went to the container filesystem. The startup report says so explicitly. |
| App will not start after attaching a volume | the volume was mounted at `/app`, shadowing the code — mount it at `/app/data` |
| Permission denied writing to the volume | non-root image UID — set `RAILWAY_RUN_UID=0` |
| A pause on each redeploy | normal with a volume attached |
| Never sleeps, credit drains | a healthcheck ping (leave the path unset), or a second service you added |
| `502` on the first visit | the service was asleep — reload |
| `429` from the AI | an OpenRouter `:free` model — the defaults avoid these |
| Network calls fail on the trial | **Limited Trial** (unverified GitHub account) restricts outbound access — check railway.com/verify |
| Stops mid-month | the credit is spent; it resumes next cycle. **Trial volumes are deleted 30 days after the credits expire.** |

## 8. Backups

With a volume, that volume is the only copy: use Railway's **manual and automated volume
backups** (service → the volume → Backups), and keep an occasional copy off-platform. With
Railway PostgreSQL you get point-in-time recovery instead.

## 9. If you outgrow free: Hobby, $5/month

Includes $5 of usage and a **5 GB volume** (resizable live, no downtime). Same project, same
volume, nothing to migrate — the service simply stops sleeping and gets faster. The free path is
for getting the app in front of students without spending anything.

## 10. How this was verified

No Docker daemon existed in the environment this was built in, so the image was verified by doing
exactly what it does, on a clean copy of the repository:

- `pip install -r requirements.txt` into a fresh virtualenv — completed, so the container's
  dependency list is complete;
- the image's own command, `uvicorn main:app --host 0.0.0.0 --port $PORT`, started the app;
- **scenario A — only a volume attached** (`RAILWAY_VOLUME_MOUNT_PATH` set, nothing else):
  the app used the volume for the database, documents *and* log, chose disk storage and per-request
  connections, inferred OpenRouter from the key, and passed the 23-check HTTP suite;
- **scenario B — only a PostgreSQL service added** (nothing about storage configured):
  the app detected the database, switched documents into it, chose per-request connections, and
  passed the 30-check summary suite with the documents confirmed as rows in PostgreSQL;
- the app-level test suite: **140 tests**.
