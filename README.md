# Job Agent

A self-hosted AI agent that finds relevant job openings, prepares outreach for your approval, and tracks your pipeline. It runs 24/7 on your own server. Full design: [ARCHITECTURE.md](ARCHITECTURE.md).

> **Status:** Module 0 (platform foundation) ✅ · Module 1 Part 1a (company discovery) — in review.

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

## Company discovery (Module 1, Part 1a)

Scouts find companies and feed one candidate pipe. A company's identity is its website domain, so the same company seen by several scouts becomes one record.

| Scout | Source | Needs | Runs (IST) |
|---|---|---|---|
| `seeds` | `config/seed_companies.yaml` (your list) | nothing | on worker start-up + daily 05:00 |
| `yc` | Public YC directory (yc-oss mirror) | nothing | daily 05:30 |
| `adzuna` | Adzuna jobs API, India | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | daily 06:00 |
| `jooble` | Jooble jobs API | `JOOBLE_API_KEY` | daily 06:15 |
| `remotive` | Remotive remote-jobs API | nothing | daily 06:30 |
| `hn` | HN "Who is hiring?" (official API) | nothing | 2nd + 16th of the month |

Companies that are only seen by name, mostly from job sites, stay **unresolved** until their website is found. With `BRAVE_API_KEY` set, an hourly resolver searches for them. Without it they wait for Part 1b. Expect a good share of job-site companies to sit here.

The job-site scouts search for the `target_roles` in `config/owner_stub.yaml`.

```bash
docker compose exec api python -m app.cli scout yc        # run one scout now
docker compose exec api python -m app.cli stats           # what's been found + scout health
```

API (via `/docs`): `GET /api/companies`, `GET /api/companies/{id}`, `GET /api/scouts`, `POST /api/scouts/{name}/run`, `GET /api/audit`.

## Configuration

- **Secrets and API keys:** `.env`. Only `ANTHROPIC_API_KEY` is required. Everything else is an optional free tier, and the app degrades gracefully without it.
- **Rules:** `config/*.yaml`. They're read-only in the app. To change one, edit the file and run `docker compose restart`.
- **Embeddings:** a small model runs locally on your server (`BAAI/bge-small-en-v1.5` via fastembed). It needs no key and costs nothing, and your resume text never leaves your server.

## Costs

The LLM is expected to cost about **$3–8/month** at steady state, using your own Anthropic key. Everything else uses free tiers. A full cost table will be added as modules land.
