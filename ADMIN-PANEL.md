# Admin panel

Your API key, models, budgets and token usage, editable from the browser — **without
touching `.env` or restarting the app**. Reachable at `/admin`.

The panel is **closed by default**: with no `ADMIN_PASSWORD` set on the server it refuses
to open and says so. There is no default password, ever, and nothing sensitive is in the
page itself — everything comes from `/api/admin/*`, which requires a session.

---

## 1. Set the password (once)

**Preferred — store a hash, not the password:**

```bash
cd ~/pharmascan
source ~/virtualenv/pharmascan/3.11/bin/activate
python -m core.auth hash            # prompts, so it never lands in shell history
# → scrypt$16384$8$1$<salt>$<hash>
```

Add to `.env`:

```dotenv
ADMIN_PASSWORD_HASH=scrypt$16384$8$1$...
SESSION_SECRET=<python -m core.auth secret>
```

**Quicker, less tidy:** `ADMIN_PASSWORD=choose-something-long` instead of the hash. It is
hashed in memory at startup (scrypt, standard library — no bcrypt wheel to install).

Then restart and check:

```bash
touch tmp/restart.txt
python -m core.auth check        # reports what is configured, and from where
```

Open `https://your-domain/admin` and sign in.

> `SESSION_SECRET` is the signing key for the session cookie. Without it the key is derived
> from the password, which means changing the password signs every device out — often what
> you want. Set it explicitly if you want sessions to survive a password change.

---

## 2. What the panel does

**Groq connection** — *Test connection* makes a real call to Groq and reports the truth:
whether the key is accepted, **every model that key can actually use**, and whether your
configured analysis model is among them. That last part matters: free-tier keys do not
include every model, and a model that has been withdrawn fails in a way that looks like an
outage. The model fields autocomplete from the live list.

**Settings** — each field shows where its value comes from:

| Badge | Meaning |
|---|---|
| `panel` | set on this page (wins over everything) |
| `server` | set in `.env` / the environment |
| `default` | the built-in default |

Precedence is **panel → server → default**, so a value set here overrides `.env` until you
press **revert**. Changes take effect on the next request; no restart, no file editing.

Editable: the Groq key, the analysis model, the outline/synthesis models, the answer
budget, the two token budgets, the kill switches for analysis and short notes, and the
student access code.

**Usage** — tokens spent in the last 24 hours against the daily budget, a breakdown by
stage (analysis / outline / section / synthesis), document and summary counts, and the most
recent AI calls. Every AI call in the app is recorded here, so the numbers are not
cherished estimates: they are the same rows the budget enforcement reads.

---

## 3. Protecting the AI features

The AI endpoints cost money to call, so there are three layers, in order:

1. **Budgets** (always on) — an app-wide daily token ceiling and a per-device allowance.
   This is the hard limit: even with no code set, an open endpoint cannot drain the key
   past `SUMMARISE_DAILY_TOKEN_BUDGET`.
2. **Kill switches** — switch document analysis or short notes off entirely from the panel.
3. **Access code** — set one and students must enter it **once per device** (30 days). The
   app asks for it automatically the first time an AI feature is used, then retries the
   request they were making, so nobody sees a dead end. The admin session bypasses it.

Leave `ACCESS_CODE` blank and the AI features stay open to anyone who has the URL —
exactly how the app behaved before, so nothing changes underneath you by surprise.

**What this is not:** there are no student accounts. Anyone with the code has the features;
the code is shared, not personal. That is the right trade for a class vault, but it is not
per-student attribution. Per-device usage is best-effort (the client identity can be
spoofed), which is why the app-wide budget — not the per-device one — is the real
protection.

---

## 4. Security notes

- **Password**: scrypt (n=2¹⁴) with a random salt; constant-time comparison. Neither the
  password nor its hash is ever sent to the browser.
- **Session**: signed, expiring (default 12h), `HttpOnly`, `SameSite=Lax`, `Secure` when
  the request is HTTPS. Changing the password invalidates existing sessions.
- **CSRF**: every state-changing admin request must carry an `X-CSRF-Token` that matches
  the session, on top of `SameSite=Lax`. A request without it, or with a stale one, is
  refused with 403.
- **Login throttling**: failed sign-ins are counted per client and delay further attempts;
  after 8 failures inside 15 minutes the client is locked out for the rest of the window.
  Honest limitation: the counter is per worker process, so under Passenger it slows an
  attacker rather than stopping one — the password length is your real defence.
- **Secrets are never returned** by the API — only "set (gsk_ab…wxyz)" style masks. Saving
  with a blank secret field leaves the stored value untouched, so you cannot wipe a key by
  saving the form.
- **Only whitelisted keys** can be written. Trying to save, say, `SESSION_SECRET` from the
  panel is rejected (`'SESSION_SECRET' is not an editable setting`) — a session cannot be
  turned into arbitrary config rewrite.
- **The panel page contains no secrets** and is marked `noindex, nofollow`.

---

## 5. If something is wrong

| What you see | What it means |
|---|---|
| "No admin password is configured" | `ADMIN_PASSWORD(_HASH)` is not set, or the app was not restarted. `python -m core.auth check` says which. |
| "Wrong password" | As it says. Username is irrelevant — there is only a password. |
| "Too many failed attempts" | The throttle. Wait out the window (about 15 minutes from the first failure). |
| 403 "CSRF token missing or stale" | The page was open while the session expired. Reload and sign in again. |
| 503 "The admin panel is not configured" | Same as the first row. |
| Test connection: "Groq rejected the key (401)" | The key is wrong, revoked, or pasted with extra characters. Regenerating a key in the Groq console invalidates the old one. |
| Test connection: "Could not reach api.groq.com" | The host has no outbound HTTPS, or Groq was unreachable. Nothing to do with the key. |

---

## 6. Running without the panel

Nothing requires the panel. Every setting it exposes can also live in `.env` — the panel
is a convenience over the same values, and clearing an override returns to them.
