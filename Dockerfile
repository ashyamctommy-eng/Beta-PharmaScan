# Container deploy (rollout.host, Render, Railway, Fly, any Docker host).
#
# This path needs NONE of the cPanel machinery: no passenger_wsgi.py, no a2wsgi
# bridge, no Passenger spawn-method traps. uvicorn serves the ASGI app directly,
# which is what the app was written for.
#
# The container's own filesystem is wiped on every redeploy, so state must not live on it.
# On Railway: attach a volume at /app/data and set DATA_DIR=/app/data — that one variable
# puts the SQLite database, the uploaded documents and the log inside the volume together
# (SQLite + STORAGE_BACKEND=disk, the defaults, then need no other change).
#
# Without a volume, point the app at a managed database instead:
#   DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/pharmascan   (or a plain
#       postgresql:// URL — core/config.py rewrites the scheme, and ?sslmode=require,
#       into what asyncpg accepts, so paste whatever the host gives you)
#   STORAGE_BACKEND=database    → uploaded documents live in that database too
# Add DB_POOL_MODE=null on a host that sleeps idle services (Railway Serverless: it
# decides idleness from OUTBOUND traffic, so an idle connection pool keeps it awake).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directories the app writes to at runtime (bind-mounted or on a volume in production).
RUN mkdir -p uploaded_notes tmp

EXPOSE 8000

# One worker: this app keeps SQLite and file state on local disk, and the summary
# pipeline is deliberately bounded per request. Scale with a process manager or a
# managed database before raising this.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
