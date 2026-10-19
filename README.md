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
| `waas` | YC Work at a Startup: public category pages only, never logged in. Your decision; it relaxes the spec's original "never crawl" rule | nothing | daily 06:45 |
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

## Job shortlisting (Module 2), Part 2a: intake and free filters

Every 15 minutes, raw postings from the watched companies' feeds are turned into **job postings** in one standard shape. That means parsed dates (including Workday's "Posted 7 Days Ago"), employment type, India/remote flags and stated pay.

- **Siblings** (the same role under several requisition IDs) form one cluster, so they're scored once and shown as one card.
- **Re-seen postings** only update their "last seen" date. A **material change** marks the posting for one re-score.
- **Free filters** (`config/job_rules.yaml`), with no AI involved:
  - a broad title net built from your role families in `owner_stub.yaml`;
  - your excluded families and obvious noise titles;
  - full-time only (unknown types pass on to the AI read);
  - posted within the last 21 days.
- **Full descriptions** are fetched for jobs that passed, from Workday, Oracle, Eightfold and SmartRecruiters, whose list endpoints give titles only.

```bash
docker compose exec api python -m app.cli pipeline      # process now
docker compose exec api python -m app.cli funnel        # counts + top reasons for filtering out
docker compose exec api python -m app.cli passed --domain lilly.com
```

### Part 2b: the AI read and the rules on it

**Two more free rules** run first:
- titles two or more rungs above your level (*Director, VP, Head of, Chief*) are dropped, while "Associate Director" and "Chief of Staff" go on to the AI;
- jobs outside India and not remote are dropped, but only if `open_to_relocation: false`.

Then **one AI read per role** (Claude Haiku 5.5, about $0.001; once per sibling cluster; capped at `kind2.max_reads_per_day`). From the description, not the title, it works out:
- whether the job is doable from India without a visa you'd arrange, and whether it's a 6-day week;
- the real role family, and **the rung on your own ladder**;
- years of experience asked for, people management, and employment type;
- stated or estimated pay;
- flags: shift, travel, on-call, relocation.

**Your rules are then applied in code:**
- **Level:** your level or one up.
- **Track:** no switch to people management unless you've allowed it.
- **Laterals:** only at a step-up brand.
- **Dealbreakers:** visa, 6-day week, non-full-time.
- **Pay** must beat your current pay. Relocation uses **PPP** (World Bank, refreshed yearly), remote-from-abroad uses the exchange rate; within ±15% is "marginal", and unstated pay is estimated and flagged.
- **Services penalty:** −10 for services-flavoured work, except at brands listed in `kind2.prestige_brands` or rated up/lateral against your career.

Salary and seniority are independent gates.

Changing a rule or a value in `owner_stub.yaml` re-applies the rules for free. Only a change to what the AI is told about you triggers new reads.

**Your own brand call.** If you disagree with Module 1's view of a company's brand, record your view. It wins from then on, and that company's roles are re-checked for free:

```bash
docker compose exec api python -m app.cli brand morganstanley.com up "Stronger brand than ZS"
```

```bash
docker compose exec api python -m app.cli assess --limit 10
docker compose exec api python -m app.cli assessed --passed
docker compose exec api python -m app.cli assessed --rejected
docker compose exec api python -m app.cli rates --refresh
```

### Part 2c: scoring and bands

Scoring runs in two stages:
1. **Free match.** Every role that passed is compared with your resume (`config/resume.pdf`) and profile, using local embeddings.
2. **Paid grade.** The best matches get **one AI grade each** (Claude Sonnet 5.5, about 1–2¢). The cap is `scoring.grades_per_day`, default 20.

The grade gives scores on six dimensions: skills, experience, domain, growth, brand and location. It also gives reasons, the skills you're missing, and a one-liner.

**Score** = weighted average of the six, minus penalties (services; a lateral move after 2+ years in your role).

**Bands:**

| Score | Band |
|---|---|
| 85+ | **Fast lane** |
| 65–84 | **Inbox** |
| 40–64 | **Maybe** |
| below 40 | **Archive** |

Some roles can't go above Maybe:
- roles not graded yet;
- leads (job-site, HN or WaaS mentions without a confirmed posting).

An open question (location, level or hours) also keeps a role out of the fast lane.

Editing the weights or bands in `job_rules.yaml` re-bands everything for free. A role is re-graded only if its posting, your resume or your profile changes.

Your feedback (`act`, `skip`, `thumbs_up`, `thumbs_down`, `maybe_rescue`) applies right away:
- skip and thumbs-down move the role to the archive;
- a rescue lifts it into the Inbox.

Feedback is also stored for later tuning.

```bash
docker compose exec api python -m app.cli score --limit 5
docker compose exec api python -m app.cli shortlist
docker compose exec api python -m app.cli shortlist --band maybe
docker compose exec api python -m app.cli feedback <cluster_id> skip "Not my domain"
```

API: `GET /api/shortlist`, `POST /api/jobs/{cluster_id}/feedback`.

## Cold outreach (Module 3), Part 3a: finding the right people

**Nothing in this part sends email.** In Module 3, every email is approved by you, one at a time.

Contacts are found for **pursued roles**: fast-lane roles automatically, plus any role you mark `act`. Each pursued role gets one **channel**:

| Channel | When |
|---|---|
| **Designated** | The posting names an address ("send your CV to…"). That address is the *only* channel for the role, with no hunting for people. |
| **Bench** | People at the company. Startups: founders + recruiters. Mid-size: recruiters + hiring managers. MNCs: recruiters, per function and geography. Caps: 5 / 5 / 7. |
| **Mailbox** | No people found, but the company publishes a hiring mailbox (jobs@, careers@). |
| **None** | Nothing found. The role goes to the apply list anyway, and the agent looks again in 2 weeks. |

**How people are found:**
- **Public search snippets only.** The agent runs Brave searches shaped to return LinkedIn profile results, and reads only what the search engine shows. It never logs into, opens or crawls LinkedIn.
- **No guessing on identity.** Each person is matched on name, title, location, snippet and profile URL, never on the name alone. Two same-name profiles at one company are frozen and never guessed at.

**How addresses are found:** published → lookup service (Hunter, then Apollo; one pooled budget, cached forever) → built from the company's address pattern. The pattern is learned from one real address; otherwise common formats are tried.

**How addresses are checked:**
- **Mail server:** the domain must accept email.
- **Mailbox check:** first a direct check with the company's mail server, which is often blocked on home internet; then MyEmailVerifier, then Hunter's verifier.
- **Catch-all domains:** if the domain accepts any made-up address, every address there stays low-confidence.
- **History wins:** an address that has already delivered or been replied to beats every other signal.

**Be realistic:** for roughly 30–50% of companies, no usable contact will be found.

**Setup (all optional; add keys to `.env`, then run `docker compose up -d`):**
- `BRAVE_API_KEY`: without it, contacts come only from postings and HN posts.
- `HUNTER_API_KEY`, `APOLLO_API_KEY`: email lookups. Apollo's free API needs an account registered with a work email.
- `MYEMAILVERIFIER_API_KEY`: mailbox verification.

```bash
docker compose exec api python -m app.cli lookups              # which services are set up, free-tier use
docker compose exec api python -m app.cli feedback <cluster_id> act
docker compose exec api python -m app.cli contacts --cluster <cluster_id>
docker compose exec api python -m app.cli channel <cluster_id>
docker compose exec api python -m app.cli bench <domain>
docker compose exec api python -m app.cli designate <cluster_id> <email>|auto "<reason>"
docker compose exec api python -m app.cli add-contact <domain> "<name>" --email <email> --title "<title>"
```

API: `GET /api/roles/{cluster_id}/channel`, `GET /api/companies/{company_id}/benches`.

### Part 3b: drafts

**Still nothing is sent.** Each pursued role with a channel gets **one draft**. Every recipient gets the same email; only the name in the greeting changes.

**Your template** is `config/email_template.md`. It has three parts:
- **settings:** subject line, greeting, number of points, whether to attach your resume;
- **the email exactly as it goes out:** fixed wording with placeholders;
- **instructions** for the two parts the AI writes: the intro and the numbered points.

Edit the template and run `docker compose restart`, and every draft is rewritten on the next round. Your name, mobile number and LinkedIn URL come from your private `owner_stub.yaml` (`name`, `mobile`, `linkedin_url`). A signature line with no value is left out.

**Truth check:** a second AI pass lists every claim the draft makes about you and checks each one against your resume and profile. Anything unsupported gets one rewrite. If it's still unsupported, the sentence is cut before you see the draft. A draft that couldn't be fixed cleanly is marked **FLAGGED**. Drafts never mention gaps.

**When drafts are rewritten:** only when the posting, your resume or profile, the template, or the job IDs change. Each draft costs about 2–3¢, with at most 15 drafts a day.

```bash
docker compose exec api python -m app.cli drafts --cluster <cluster_id>   # write it now
docker compose exec api python -m app.cli draft <cluster_id>              # read it as a recipient would
docker compose exec api python -m app.cli drafts --cluster <cluster_id> --force   # rewrite
```

API: `GET /api/roles/{cluster_id}/draft`.

### Part 3c: review and sending

**Nothing is ever sent without your approval of that exact email.** There is no auto-send mode.

#### Connect Gmail (one time, about 15 minutes)

Emails go out from **your own Gmail** and appear in your Sent folder. The agent only reads the threads it started. Use a personal Gmail account.

1. At console.cloud.google.com, create a project and enable the **Gmail API** (APIs & Services → Library).
2. In **Google Auth Platform** (the OAuth consent screen), click Get started. Fill in an app name and your email, and choose **External**.
3. Under **Data Access**, add two scopes: `https://www.googleapis.com/auth/gmail.send` and `https://www.googleapis.com/auth/gmail.readonly`.
4. Under **Audience**, click **Publish app** so it shows "In production". In "Testing" mode, Google signs the app out every 7 days. Google will call it unverified, which is normal for a personal app; you click past one warning when connecting.
5. Under **Clients**, create a client: **Web application**, with redirect URI `http://localhost:8000/api/gmail/callback`.
6. In `.env`, set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and an `APP_SECRET_KEY` generated with:
   `docker compose run --rm api python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`
7. Run `docker compose up -d`, then `python -m app.cli gmail connect`. Open the link it prints, sign in, and tick both permissions. Then run `gmail status` and `gmail test`, which sends an email to yourself.

Your sign-in is stored encrypted with `APP_SECRET_KEY`. To revoke access anytime, go to myaccount.google.com/permissions.

#### Review: approve / edit / skip

- **Campaign:** each drafted role becomes a campaign. Every recipient's email is shown exactly as it will go out: recipient, subject, body, attachment.
- **Approve or skip each email.** An address that couldn't be proven (a catch-all domain, or one that couldn't be checked) needs your explicit `--allow-low`, unless a lookup service vouches for it.
- **Edit:** your text replaces the shared email for every recipient. Earlier approvals are reset, so you approve the final text.
- **Learning from your edits:** each edit is kept as a style example. Future drafts see your latest edits (5 by default) and follow your preferences. Facts still come only from your resume and still pass the truth check.
- **Start:** once every email is approved or skipped, the waves start.

#### Waves and limits

**Waves:**
- day 0: the first two people;
- day 1: the next two;
- day 2: the rest.

Each wave goes out only if nobody at the company has replied.

**Limits** (none can be bypassed):
- your pause switch;
- the bounce breaker;
- quiet hours, in the recipient's local time (best effort);
- **12 emails a day** in total;
- **2 a day per company**;
- **30–60 minutes** between any two emails.

**When someone replies,** every unsent email to that company is held, and you're told. Read the reply in Gmail, then release or stop that company. Part 3d will sort replies into interested, redirect, out-of-office and so on, and act as the spec says. **The agent never replies and never sends follow-ups.**

**When an email bounces:**
- That address is dead, and a guessed address pattern at that domain is no longer trusted.
- **Bounce breaker:** 2+ bounces, or more than 5% once 30+ are sent, freezes the unsent queue. Every queued address is re-checked, and sending resumes by itself after 6 hours once the queue is clean. A second spike within 7 days that guessed addresses don't explain waits for you.

```bash
docker compose exec api python -m app.cli review                         # what's waiting / running
docker compose exec api python -m app.cli review show <campaign>         # every email exactly as it will go out
docker compose exec api python -m app.cli review approve <campaign> --email 1     # or --all; add --allow-low if asked
docker compose exec api python -m app.cli review skip <campaign> --email 3
docker compose exec api python -m app.cli review export <campaign>       # edit review/campaign-N.txt in any editor…
docker compose exec api python -m app.cli review import <campaign>       # …then load your edit
docker compose exec api python -m app.cli outreach status
docker compose exec api python -m app.cli outreach pause                  # stop all sending; `unpause` to continue
docker compose exec api python -m app.cli outreach hold|release|stop <domain>
```

#### Part 3d: replies

The agent reads only the threads it started, and **never writes a reply or a follow-up**. Every 15 minutes, each new message is sorted by a cheap AI read (once per message). A read that's less than 75% sure counts as *unsure*.

| Reply | What happens |
|---|---|
| Interested / forwarded / question / rejection | Every unsent email to that company is cancelled, even approved ones. You get an instant alert with the Gmail link. The conversation is yours. |
| Out-of-office | Nothing is cancelled. That person's emails wait until they're back. If the reply names a stand-in ("contact X in my absence"), an email to X is prepared for your approval. |
| "Not the right person" | Nothing is cancelled. That person is marked wrong-target. The person they name joins the bench at the front and gets an email in the next wave, after you approve it. |
| Other automatic messages | Logged only. |
| Unsure | The company is paused, and you see the raw email with no suggested action. Then run `outreach release` or `outreach stop`. |

If a reply names someone without an address, the agent looks the address up the same way as for its own contacts: Hunter, then the company's pattern, checked with the mail server. The approval note says how the address was found and whether it was confirmed. If two people at the company share that name, it doesn't guess.

When the waves end and nobody has answered for 7 days, the campaign is marked **silent** and you're told once: "follow up yourself if you'd like".

Campaign outcomes (interested, forwarded, question, rejected, silent, stopped) feed Module 4 and the pipeline. Alerts are kept for Module 6 (instant alerts plus the summary mail); until then, see them with:

```bash
docker compose exec api python -m app.cli pings            # what the agent wants you to know (--read marks them read)
docker compose exec api python -m app.cli replies          # replies and how each was sorted
```

API: `GET /api/gmail/connect` · `GET /api/gmail/status` · `GET /api/campaigns` · `GET /api/campaigns/{id}` ·
`POST /api/campaigns/{id}/emails/{email_id}` (`{"action": "approve"|"skip", "allow_low": false}`) ·
`PUT /api/campaigns/{id}/text` (your edit) · `GET/PUT /api/outreach/sending` · `GET /api/notifications` · `POST /api/notifications/{id}/read`.

## Configuration

- **Secrets and API keys:** `.env`. Only `ANTHROPIC_API_KEY` is required. Everything else is an optional free tier, and the app degrades gracefully without it.
- **Rules:** `config/*.yaml`. They're read-only in the app. To change one, edit the file and run `docker compose restart`.
- **Embeddings:** a small model runs locally on your server (`BAAI/bge-small-en-v1.5` via fastembed). It needs no key and costs nothing, and your resume text never leaves your server.

## Costs

The LLM is expected to cost about **$3–8/month** at steady state, using your own Anthropic key. Everything else uses free tiers. A full cost table will be added as modules land.
