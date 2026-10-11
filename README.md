# Job Agent

A self-hosted AI agent that finds relevant job openings, prepares outreach for your approval, and tracks your pipeline. It runs 24/7 on your own server. Full design: [ARCHITECTURE.md](ARCHITECTURE.md).

> **Status:** Module 0 ✅ · Module 1: discovery ✅ · judgment ✅ · watching (Part 1c) — in review.

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
| `packs` | Curated [seed packs](config/seed_packs/README.md): India GCCs, large Indian companies, Indian unicorns | nothing | daily 05:10 |
| `peers` | "Companies like the ones you keep", suggested by AI and confirmed against each live website | Anthropic key | daily 07:30 |
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

## Company judgment (Module 1, Part 1b)

Every company with a known website gets a **verdict**: keep or reject, with plain-language reasons. Rules live in `config/company_rules.yaml`.

1. **Free checks first.** These use what the scouts already know, such as the YC category tags. A blocked category (e-commerce, food delivery, quick commerce, generic marketplace) is rejected with no AI at all.
2. **Fact card.** Claude Haiku 5.5 reads a startup's homepage. For established companies it uses general knowledge instead. Every fact is tagged verified, inferred or unknown.
3. **Shared checks.** The avoid list (IT outsourcing, staffing agencies) and India reachability.
4. **Startup checks.** Viability: hiring or recently funded, and no distress news. A web search runs only when it could change the answer.
5. **One AI judgment** with Claude Sonnet 5.5:
   - for a startup, *novel or clone?* (borderline means reject);
   - for an established company, *step up, lateral or down compared with your career?* (unknown means reject).
6. **Your overrides become precedents.** Similar future judgments see them as examples.

At most `max_new_per_day` companies (default 50) are judged per day, in the order: your seeds, HN, YC, then job sites. Each verdict records its exact AI cost.

Rejected companies come back for one re-check if a relevant fact changes or the rule behind the rejection changes. A blocked category and your permanent thumbs-down never come back.

```bash
docker compose exec api python -m app.cli evaluate --limit 5         # judge 5 now
docker compose exec api python -m app.cli verdicts --last 20         # see the reasons
docker compose exec api python -m app.cli override uber.com keep "Great analytics culture"
docker compose exec api python -m app.cli stats                      # includes AI spend
```

API: `GET /api/verdicts`, `POST /api/companies/{id}/override`, `POST /api/companies/{id}/evaluate`, `GET /api/llm/usage`.

## Watching companies (Module 1, Part 1c)

Every kept company gets a **job feed**, set up automatically within about 15 minutes of passing:

1. **Find the careers page.** The agent tries the homepage's links, then common addresses (`/careers`, `jobs.` and similar), then a web search, then an AI pick from the homepage links. A page is trusted only if it actually lists jobs. Redirects to LinkedIn or Glassdoor are refused.
2. **Pick the cheapest reliable reader:**
   - the public job-board APIs of Greenhouse, Lever, Ashby, SmartRecruiters, Workable and Recruitee;
   - Workday, searched with your target roles rather than read in full;
   - a [per-company adapter](config/adapters/README.md);
   - otherwise, the **job-site fallback**, which uses what Adzuna, Jooble and Remotive show for that company. These postings are marked *lead only*.
3. **Check ownership.** A board found by guessing its name must prove it belongs to this company: an exact name match, links back to the company's site, or job descriptions that name it.
4. **First sync immediately.** Jobs land in `raw_postings`, where Module 2 will score them. Later syncs add new jobs, update changed ones and close removed ones.

**Weekly upkeep** runs on Sundays at 03:00:
- **Cadence:** active companies are read every 6 hours, quiet ones weekly.
- **Repairs:** a broken feed is re-set-up after 5 failures in a row. A feed that's missing jobs the job sites can see is re-set-up too. Every feed is re-checked every 90 days.
- **Pruning:** a company is marked dormant after 90 silent days and archived after a further 90.
- **Refreshes:** fact cards are refreshed monthly for startups and quarterly for big companies.

Companies on custom careers pages that no reader supports yet use the job-site fallback for now. A browser-based reader for those pages is planned.

```bash
docker compose exec api python -m app.cli activate --limit 5      # set up feeds now
docker compose exec api python -m app.cli feeds                   # health by method
docker compose exec api python -m app.cli jobs --domain uber.com  # a company's open roles
docker compose exec api python -m app.cli garden                  # run the weekly upkeep now
```

API: `GET /api/feeds`, `GET /api/postings`, `POST /api/companies/{id}/activate`.

## Configuration

- **Secrets and API keys:** `.env`. Only `ANTHROPIC_API_KEY` is required. Everything else is an optional free tier, and the app degrades gracefully without it.
- **Rules:** `config/*.yaml`. They're read-only in the app. To change one, edit the file and run `docker compose restart`.
- **Embeddings:** a small model runs locally on your server (`BAAI/bge-small-en-v1.5` via fastembed). It needs no key and costs nothing, and your resume text never leaves your server.

## Costs

The LLM is expected to cost about **$3–8/month** at steady state, using your own Anthropic key. Everything else uses free tiers. A full cost table will be added as modules land.
