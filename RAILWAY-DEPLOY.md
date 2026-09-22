# Deploy on Railway (free tier)

## 1. Which plan you are on, and what changes

Railway's trial is **one-time: $5 of credit, valid for up to 30 days**, and it **expires in 30
days whether or not you spend it**. When 30 days pass or the $5 is spent, the account
**automatically reverts to the Free plan: $1 of credit per month**, which does not roll over.

| | Trial | After it reverts (Free) |
|---|---|---|
| Credit | **$5, one-time, expires after 30 days** | **$1 per month** |
| Limits | 1 GB RAM, **shared** vCPU, 5 services per project | 1 replica, 0.5 GB RAM, 1 vCPU, 0.5 GB volume |
| Always-on app | ~$2–4/month — **affordable inside the $5** | more than the credit → **the service must sleep** |
| If the credit runs out | reverts to Free | Railway **stops your workloads** until the next cycle; you cannot buy credit on Free |

Usage prices are the same on both: RAM **$10/GB/month**, CPU **$20/vCPU/month**, volume
$0.15/GB/month, egress $0.05/GB.

**So why bother with the sleeping settings now?** Because the switch is automatic and
unattended: if the app is always-on when the trial ends, it stops. Set it up once as below and
that moment passes without you noticing. During the trial you can leave Serverless off if you
prefer instant responses.

### Two trial-specific traps

- **Full trial vs limited trial.** Verification depends on your GitHub account's age and
  activity. An unverified account gets the **Limited Trial: restricted outbound network access,
  only a limited set of ports**. Both the AI calls and the database connection are outbound, so
  a limited trial can fail in confusing ways. If deploys succeed but requests that need the
  network fail, check **railway.com/verify**.
- **Trial volumes are temporary.** Railway **deletes stateful volumes created by trial accounts
  30 days after the credits expire**. That is another reason every setting here points at an
  **external Neon database**: your vault, documents and notes sit outside Railway's trial
  lifecycle, and surviving the revert is free.

## 2. The setting that makes it work: Serverless + `DB_POOL_MODE=null`

Railway's **Serverless** (formerly App Sleeping) puts a service to sleep after ~5–10 minutes
and wakes it on the next request, so you are only billed while it is actually doing
something. It decides "idle" from a service's **outbound** traffic.

That is the whole problem: this app holds a **pool of database connections open**, and an open
connection is outbound traffic. Leave it as-is and the service never sleeps, so the $1 credit
is spent in about a week.

`DB_POOL_MODE=null` fixes it — one connection per request, closed immediately:

```dotenv
DB_POOL_MODE=null        # lets a sleeping host sleep; costs a few tens of ms per request
```

Verified: with `DB_POOL_MODE=null` and a plain `postgresql://…?sslmode=disable` URL
(exactly what Railway hands you), the stateless app runs against PostgreSQL with the full
HTTP suites green.

## 3. Database: reuse your Neon database

- **Don't** add Railway's Postgres plugin on the free path: it is a second service that also
  burns the same $1 credit, and its open connections make the app harder to sleep.
- Use the **Neon** database from the Render guide instead. It is free, permanent, and
  auto-suspends when idle.
- **No volume needed.** With `STORAGE_BACKEND=database` the documents live in the database,
  so you dodge the volume cost, the 0.5 GB volume cap, and the "no replicas with a volume"
  limitation.
- Use Neon's **direct** host (no `-pooler`): asyncpg uses prepared statements, which PgBouncer
  in transaction mode can reject.

## 4. Deploy

1. Railway → **New Project → Deploy from GitHub repo** → pick the app.
2. Railway finds the `Dockerfile` and builds with it — no config file needed. (I deliberately
   ship no `railway.json`/`railway.toml`: Railway deprecated Config-as-Code in favour of
   Infrastructure-as-Code, and the Dockerfile is discovered on its own anyway.)
3. Add the variables:

```dotenv
DATABASE_URL=postgresql://user:password@ep-xxx.region.aws.neon.tech/neondb?sslmode=require
STORAGE_BACKEND=database
DB_POOL_MODE=null
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

You can paste the URL exactly as Neon or Railway gives it: `core/config.py` rewrites
`postgres://`/`postgresql://` to `postgresql+asyncpg://` and `sslmode=` to `ssl=`.

4. **Enable Serverless** (service → Settings), and **leave the healthcheck path unset**: a
   periodic healthcheck makes the app respond, and responses are outbound traffic that resets
   the idle timer.

## 5. Verify

| Check | Expected |
|---|---|
| Open the app URL | loads (first hit after a sleep: cold boot, ~5–15 s; a 502 on that first request is documented Railway behaviour) |
| `/api/notes` | `{"total":0,…}` — the app reached Neon |
| `/admin` → sign in → Test connection | your key's models are listed |
| Upload a PDF → Short notes → Generate | real notes with page numbers |
| Leave it 20 minutes, then reload | cold boot, and **nothing is lost** |
| Railway → Usage | the daily spend is well under $1/30 days |

## 6. Traps

| Symptom | Cause |
|---|---|
| Never sleeps, credit drains | an open DB pool (set `DB_POOL_MODE=null`), a healthcheck ping, or something else talking outbound |
| First request after a break returns 502 | normal for a slept service — reload |
| Slow request | cold boot, plus Neon waking (a second or two) |
| `429` from the AI | an OpenRouter `:free` model — use `openai/gpt-oss-20b` |
| Service stopped mid-month | the $1 credit is spent; it resumes next cycle |
| Prepared-statement errors from the database | you used Neon's `-pooler` host with asyncpg — use the direct host |

## 7. If you'd rather not live inside $1

Railway **Hobby is $5/month** and includes $5 of usage, plus a **5 GB volume** at
~$0.15/GB/month. With a volume you can drop all of the above: keep SQLite and
`STORAGE_BACKEND=disk`, turn Serverless off, and the app runs exactly as it does on a VPS —
no cold starts, no pool tuning. Cheaper than most VPS providers, and there is nothing to
administer. That is the honest recommendation if the class actually uses it daily.

## 8. Railway free vs Render free

| | Railway Free | Render Free |
|---|---|---|
| Credit | $1/month, then workloads stop | n/a — genuinely free |
| Sleeps after | ~5–10 min **without outbound traffic** | ~15 min without traffic |
| Cold boot | a few seconds | 30–60 s |
| Gotcha | outbound traffic (incl. DB pools) keeps it awake | slow cold start |
| Setup | Serverless + `DB_POOL_MODE=null` | none beyond the env vars |

Both run this app. Render is the safer default because it cannot stop your service mid-month;
Railway wakes faster. The code is identical either way — that is the point of
`STORAGE_BACKEND` and `DB_POOL_MODE` being settings rather than rewrites.
