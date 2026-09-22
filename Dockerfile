# Container deploy (rollout.host, Render, Railway, Fly, any Docker host).
#
# This path needs NONE of the cPanel machinery: no passenger_wsgi.py, no a2wsgi
# bridge, no Passenger spawn-method traps. uvicorn serves the ASGI app directly,
# which is what the app was written for.
#
# IMPORTANT on hosts with an ephemeral filesystem (rollout.host's free tier sleeps
# after 15 minutes and restarts servers): SQLite and uploaded_notes/ live on that
# filesystem and will be lost. Point DATABASE_URL at a managed Postgres, e.g.
#   DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/pharmascan
# Uploaded files still need object storage; until then, treat uploads as temporary.
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
