# Job Agent

A self-hosted AI agent that finds relevant job openings, prepares outreach for your approval, and tracks your pipeline. It runs 24/7 on your own server. Full design: [ARCHITECTURE.md](ARCHITECTURE.md).

> **Status:** Module 0 (platform foundation) only. Nothing job-related runs yet.

## You need an always-on host

The agent works on a schedule, whether or not you have it open. **A laptop that sleeps won't work.** Use a small VPS (2 vCPU / 4 GB RAM is plenty) or a home server that stays on, with Docker and Docker Compose v2 installed.

## Quick start

```bash
git clone <your-repo-url> job-agent && cd job-agent
cp .env.example .env                                   # set POSTGRES_PASSWORD and ANTHROPIC_API_KEY
cp config/owner_stub.example.yaml config/owner_stub.yaml   # fill in your work history
docker compose up -d --build
```

The first build takes a few minutes because it downloads a ~130 MB embedding model into the image.

### Check it's working

Wait about a minute after start-up so the scheduler can send its first heartbeat. Then run:

```bash
docker compose ps
curl -s localhost:8000/health
docker compose exec api python -m app.cli selftest
docker compose exec api pytest -q
```

`selftest` checks config, database and pgvector, Redis, embeddings, memory search and dedupe, a worker round-trip, and the scheduler heartbeat.

### Reaching the API from your laptop

The API listens on the server's `localhost` only, because there's no login yet. Use an SSH tunnel:

```bash
ssh -L 8000:localhost:8000 you@your-server
```

Then open http://localhost:8000/docs on your laptop.

## Services

| Service | Role |
|---|---|
| `postgres` | Postgres 16 + pgvector: all state, memory, and embeddings |
| `redis` | Celery broker |
| `migrate` | Runs database migrations once, then exits |
| `api` | FastAPI backend (`:8000`) |
| `worker` | Celery worker: does the background work |
| `beat` | Celery scheduler: what makes the agent run unattended |

## Configuration

- **Secrets and API keys:** `.env`. Only `ANTHROPIC_API_KEY` is required. Everything else is an optional free tier, and the app degrades gracefully without it.
- **Rules:** `config/*.yaml`. They're read-only in the app. To change one, edit the file and run `docker compose restart`.
- **Embeddings:** a small model runs locally on your server (`BAAI/bge-small-en-v1.5` via fastembed). It needs no key and costs nothing, and your resume text never leaves your server.

## Costs

The LLM is expected to cost about **$3–8/month** at steady state, using your own Anthropic key. Everything else uses free tiers. A full cost table will be added as modules land.
