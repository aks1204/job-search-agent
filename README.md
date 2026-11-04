# Job Agent

A self-hosted AI agent that finds relevant job openings, prepares outreach for your approval, and tracks your pipeline. It runs on your own machine, with your own keys. Full design: [ARCHITECTURE.md](ARCHITECTURE.md).

> **Status:** v1 complete. Modules 0–6 ✅: companies · jobs · outreach · apply tracker · profile & memory · the app, summary mail and audit log.

## What it does, end to end

1. **Finds companies** worth watching (your seed list, curated packs, YC, Hacker News, job sites) and judges each one against your rules, with a one-line reason you can overrule.
2. **Watches their careers pages** and reads every new opening: free filters first, then a short AI read, then a score. Roles land in **Fast lane**, **Inbox** or **Maybe**.
3. **For the roles you pursue,** it finds the right people (public search snippets only), finds and checks their addresses, and **drafts** one outreach email from your résumé.
4. **You review every email.** Nothing goes out until you approve it, press **Begin outreach**, and the **Sending** switch is on.
5. **You apply yourself.** The agent never fills in forms. It gives you the company's own apply link, then follows the application through your inbox: applied, interviewing, offer, or ended.
6. **It tells you what matters,** twice a day in one summary mail, plus two optional real-time email alerts. Everything else waits quietly in the app.

## Where to run it

The agent works on a schedule, whether or not you have the app open, and **nothing runs while the machine is off or asleep**.
- **Best:** a small always-on server (2 vCPU / 4 GB RAM is plenty) or a home server, with Docker and Docker Compose v2.
- **A laptop works,** if you accept the gaps: company searches missed while it slept catch up within 15 minutes of waking, but a summary mail due while it was asleep is skipped, and outreach waves wait until it's back.

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

### Open the app

Open **http://localhost:8000** in your browser. The API's own docs are at http://localhost:8000/docs.

The app has **no login**, so it listens on `localhost` only: keep it that way. On a server, reach it through an SSH tunnel:

```bash
ssh -L 8000:localhost:8000 you@your-server
```

## Services

| Service | Role |
|---|---|
| `postgres` | Postgres 16 + pgvector: all state, memory, and embeddings |
| `redis` | Celery broker |
| `migrate` | Runs database migrations once, then exits |
| `api` | FastAPI backend (`:8000`) |
| `worker` | Celery worker: does the background work |
| `beat` | Celery scheduler: what makes the agent run unattended |

## Using the app (Module 6)

Everything you do happens in the app at http://localhost:8000; the terminal commands further down do the same things, if you prefer them. Every screen has its own address, and every card links to the page where you act on it.

### The screens

| Screen | What it's for |
|---|---|
| **Jobs** | Your matches, in tabs: **Fast lane** (strong matches; they move to Pursuing by themselves), **Inbox**, **Maybe**, and **Pursuing** (the roles you're working on: apply links, Mark applied, where outreach stands). On each match: **Pursue** or **Skip** (with a one-tap reason). |
| **Role page** | One opening's whole story: the posting, why it scored as it did, its apply links, outreach and replies, the application. Every role card opens it. |
| **Review** | Every outreach email before it goes, exactly as it will arrive, with who it goes to day by day. **Approve** or **Skip** each person, **Edit** the text, or ask for a fresh draft, then press **Begin outreach**. |
| **Companies** | The companies watched and why each was kept or dropped, their fact cards and job feeds. **Keep** / **Reject**, fix a wrong website or name, **Hold** / **Stop** outreach to a company. |
| **Profile** | You: identity, résumé, work history, and the fields your rules use. The only place your pay is shown. Edit and confirm here. |
| **Pings** | What the agent wants you to know: things that need you first, then news. Simple answers (Yes / No, which role, Apply) happen right on the ping. |
| **Memory** | What the agent has learned from you (answers, precedents, taste) and a summary of what it believes. Pin, forget or restore any entry. |
| **Audit log** | Every decision the agent made, why, and what it touched. Correct its judgment calls here (see below). |
| **Settings** | The **Sending** switch, Gmail, the summary mail and email alerts, your services and how much of each free tier is used, and your rule files (read-only). |
| **Maintenance** | How each source of companies is doing, job-feed coverage, and **Run now** for every scheduled job. |

Sidebar badges: **blue** = new matches this week, **red** = outreach waiting for your review, or unread pings.

**The same words everywhere** (app, mail, Settings). **Pursuing** is the list of roles you're working on: the Jobs tab, the mail section, "2 pursuing" in the mail's subject. Where a role stands: **to apply**, **to review** (a draft on Review), **outreach on** (or **outreach on hold**), **applied**, **interviewing**, **offer**, and **ended** with the reason (rejected, no reply, posting closed, skipped). **Pings** are what the agent wants you to know; **email alerts** are the two pings that can also reach your inbox at once.

### From a match to outreach

1. **Pursue** a role on Jobs (fast-lane roles are pursued for you). It moves to **Pursuing**, with its apply link.
2. The agent finds people to write to and drafts one email. The role then shows **to review**, and the Review badge turns red.
3. On **Review**, approve or skip each person. Edit the text if you like (approvals reset, so you approve the final words).
4. Press **Begin outreach.** Only this starts the waves (day 1, day 2, day 3); it asks you to confirm first.
5. Emails actually leave only while the **Sending** switch in Settings is on. It starts **off**. With it off, approved emails simply wait.
6. When someone replies, every unsent email to that company stops, and you're told (a ping, and an email alert if you've switched that on). **The conversation is yours: the agent never replies or follows up.**

### Your summary mail and email alerts

The agent writes to you in two ways, both from your connected Gmail to your own inbox. **Both start switched off**: turn each on in **Settings → Summary mail & email alerts**. **Send me the summary now** sends a preview at any time.

- **Summary mail**, at the times in `config/app.yaml` (default 08:00 and 20:00). Sections: ⚡ Needs you, 📥 New matches, 📤 What happened (per role), 📊 Pursuing (the roles still in play that aren't already shown above) and 🔧 Housekeeping, each only when it has something. Each role appears once, in the first section where it matters. A day with nothing new sends nothing.
- **Two email alerts**, each with its own switch: a **fast-lane find**, and **someone replying to your outreach** (with an Open in Gmail link). They are checked every 3 minutes, arrive together as one email, and wait during quiet hours (22:00 to 08:00).

The buttons in the mail open the right screen in the app (`app_url` in `config/app.yaml`, default `http://localhost:8000`), so they work on the machine that runs the app, not on your phone. Nothing is sent or changed from the mail itself. Your pay never appears in the mail, and roles held back by a wait after a rejection never appear in the mail or an email alert.

### The Audit log

Every consequential decision, in plain words: what the agent did, **why**, and what it **affected** (a link to the role or company). **More detail** shows the record behind it. Filter by area (Companies, Jobs, Outreach, Applications, Profile & memory, Mail, Housekeeping), by time (7 days, 30 days, everything), by a search word, or **only judgment calls**. The Audit log never notifies you.

**Correcting a judgment call:**
- **A company kept or rejected:** **Keep it instead** / **Reject it instead**, on that company's latest judgment. Your reason is kept in Memory as an example for future judgments.
- **A website the agent found:** **Fix it on the company's page →**. Once fixed, the entry shows *corrected by you*.
- **A reply the AI read:** **Re-sort…** and pick what it really was. It asks first. If it was in fact a person answering, every unsent email to that company is cancelled; your correction is kept in Memory.

These ask you to confirm rather than offering Undo, because some effects can't be taken back (a cancelled email, a company judged again). A company's verdict can always be flipped back.

Entries over **12 months** old are condensed on the 1st of each month to one line per kind per month ("Mar 2026 · 212 × Judged a company"); entries you corrected are kept in full. **Maintenance → Run now** does it at once.

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
- **Start:** once every email is approved or skipped, you press **Begin outreach** on the Review screen (or `review begin` in the terminal), and only then do the waves start.

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

**When someone replies,** the reply is sorted (Part 3d, below) and the outreach follows what it means: a person's answer stops every unsent email to that company. **The agent never replies and never sends follow-ups.**

**When an email bounces:**
- That address is dead, and a guessed address pattern at that domain is no longer trusted.
- **Bounce breaker:** 2+ bounces, or more than 5% once 30+ are sent, freezes the unsent queue. Every queued address is re-checked, and sending resumes by itself after 6 hours once the queue is clean. A second spike within 7 days that guessed addresses don't explain waits for you.

```bash
docker compose exec api python -m app.cli review                         # what's waiting / running
docker compose exec api python -m app.cli review show <campaign>         # every email exactly as it will go out
docker compose exec api python -m app.cli review approve <campaign> --email 1     # or --all; add --allow-low if asked
docker compose exec api python -m app.cli review skip <campaign> --email 3
docker compose exec api python -m app.cli review begin <campaign>         # everyone decided: start the waves
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

Campaign outcomes (interested, forwarded, question, rejected, silent, stopped) feed Module 4 and the pipeline. You see each reply on the Pings screen, in the summary mail, and (if switched on) in an email alert. In the terminal:

```bash
docker compose exec api python -m app.cli pings            # what the agent wants you to know (--read marks them read)
docker compose exec api python -m app.cli replies          # replies and how each was sorted
```

API: `GET /api/gmail/connect` · `GET /api/gmail/status` · `GET /api/campaigns` · `GET /api/campaigns/{id}` ·
`POST /api/campaigns/{id}/emails/{email_id}` (`{"action": "approve"|"skip", "allow_low": false}`) ·
`PUT /api/campaigns/{id}/text` (your edit) · `GET/PUT /api/outreach/sending` · `GET /api/notifications` · `POST /api/notifications/{id}/read`.

## Apply tracker (Module 4)

**The agent never fills in application forms.** You apply; it gives you the links and keeps track.

**1. Apply list.** Fast-lane roles, and roles you mark `act`, go on your apply list:
- **Links:** the company's own apply link for every opening of that role (several job IDs means several links). Each link is checked to still work before it's shown, then once a day.
- **Reminders:** a pending link is a full card for 3 days, then folds into "still pending (N)".
- **Leaving the list:** a role leaves when you apply, skip it, or the posting closes.
- **Other roles:** every other role still shows its link in `shortlist`. Which roles go on the list is set by `apply_bands` in `config/apply.yaml`.

**2. Spotting that you applied.** The agent reads **only** emails from hiring systems (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Oracle, iCIMS, Taleo…) and from companies you're tracking, from the last 60 days. Each email is read once.
- **Applied automatically:** a "we received your application" email marks the role **Applied**, with a link to that email. This works for **any** role the agent knows, even one that wasn't on your list.
- **No confirmation email:** for the 10–20% of hiring systems that don't send one, run `applied <role>`.
- **Unclear emails:** if an email doesn't clearly match one role, you're asked instead of the agent guessing.
- **What's kept:** only facts (role, status, date, link), never email content. See `PRIVACY.md`.

**Outside applications.** From the date set in `track_outside_since` (in `config/apply.yaml`), applying to a job the agent doesn't know is tracked too. The company and job title are taken from the email, and the entry is labelled "outside application". Emails from before that date are past applications and are ignored.
- **A company the agent doesn't know** is handed to Module 1 ("you applied here") and judged by the usual rules. If it passes, its job board is watched and its matching roles reach your shortlist.
- **An email that doesn't name the job:**
  - **a receipt** is recorded as "role not named";
  - **an interview or offer** that matches nothing is put to you;
  - **other news** (rejection, in review) that matches nothing is recorded quietly.
- **Answering "which role?":** use `assign <no> <role>`, `assign <no> --other "Job title"` or `assign <no> --ignore`. In the dashboard these will be buttons.

**3. Following each application:** applied → confirmed → in review → interviewing → offer / rejected / silent (no news for 21 days). Interviews, offers and rejections are added to your summary. `story <role>` shows a role's outreach and application together.

```bash
docker compose exec api python -m app.cli apply                      # your apply list, links live-checked
docker compose exec api python -m app.cli applied <role id> "note"   # mark applied (no confirmation email)
docker compose exec api python -m app.cli apply-skip <role id> "not for me"
docker compose exec api python -m app.cli applications               # where each one stands
docker compose exec api python -m app.cli story <role id>            # outreach + application, one timeline
docker compose exec api python -m app.cli track                      # run the tracker now (it runs every 30 min)
```

API: `GET /api/apply-list` · `GET /api/applications` · `POST /api/applications/{role}/applied` · `POST /api/applications/{role}/skip` · `GET /api/roles/{role}/story`.

## Your profile and memory (Module 5)

### Part 5a: your profile from your resume

`config/owner_stub.yaml` was a stand-in. Your profile now comes from your resume and is confirmed by you:

1. **Read:** `profile read` reads `config/resume.pdf` with one AI pass (about 2–3¢). It merges the result with what you already told the agent: your owner_stub answers, or your current profile.
2. **Check:** the draft is written to `review/profile.yaml`, with notes at the top:
   - what came from the resume;
   - what changed;
   - what's still missing that a resume can't say (pay, notice period, your company's level ladder, locations, mobile for your signature).
3. **Confirm:** edit the file if needed, save it, and run `profile confirm`. **Nothing uses a draft until you confirm it**, so a misread resume can't silently change your scores or emails.
4. **From then on, every module uses your confirmed profile:** company judgments, job rules and scoring, drafts and your signature. `owner_stub.yaml` is only a fallback until the first confirm, and can be kept as a backup.

**Experience is worked out from your job dates**, never typed in. Overlapping jobs count once, and the current job counts up to today. It's rounded to the nearest half year (2 years 11 months shows as 3) and stays current as time passes. To change it, fix a job's dates with `profile edit`.

**Updating your resume.** Replace `config/resume.pdf`. The daily check notices the change and prepares a new draft for you to confirm, and the old profile stays in use until you do. The agent never edits or tailors your resume.

**Staying fresh.** Pay and notice period carry a "last confirmed" date. When it's more than 6 months old, you're asked once. To change any answer, run `profile edit`, edit the file, then `profile confirm`.

```bash
docker compose exec api python -m app.cli profile read      # read the resume into review/profile.yaml
docker compose exec api python -m app.cli profile export    # write the latest draft (e.g. after a resume change) to the file
docker compose exec api python -m app.cli profile confirm   # after checking the file
docker compose exec api python -m app.cli profile show      # what's in use, and when pay/notice were last confirmed
docker compose exec api python -m app.cli profile edit      # change an answer later
```

API: `GET /api/profile` · `POST /api/profile/read` · `POST /api/profile/confirm`.

### Part 5b: career direction

The agent works out its view of your career from your resume and profile (one AI read, about 3¢). It runs again by itself after your profile changes. The view covers:
- **where you are** and **where you're heading**;
- which role families are **step up**, **sideways** (and when) or **excluded**;
- which **job titles** are worth searching;
- your **appetite for sideways moves**: low = never, medium = only at step-up brands (the default), high = anywhere that isn't a step down;
- your **track** (individual contributor or manager).

Each point has a one-line reason.

**Nothing is added to or removed from what the agent looks for without your yes** (owner decision). Your actual lists stay yours, and any difference becomes a question:
- add a family or title;
- drop one;
- start including one you'd excluded;
- change your appetite or track.

Answer each with yes or no. A "no" is remembered, and asked again only if a later resume makes a strong case. Reasons and summaries refresh automatically.

```bash
docker compose exec api python -m app.cli direction              # your lists with reasons + questions waiting
docker compose exec api python -m app.cli direction refresh      # re-work the agent's view now
docker compose exec api python -m app.cli direction yes 12       # or: direction no 12
docker compose exec api python -m app.cli direction edit         # change the lists yourself (review/direction.yaml) …
docker compose exec api python -m app.cli direction confirm      # … then save
```

**Drafts and your direction.** Drafts are rewritten when your resume or profile changes, but not when only your direction changes, since the email doesn't depend on it.
- **Drafts you edited** are kept as you left them, unless `drafts.redraft_edited_drafts: true` is set in `config/outreach.yaml`.
- **For a fresh draft any time,** run `review redraft <campaign>`. Your edits stay saved as style examples.

API: `GET /api/direction` · `POST /api/direction/suggestions/{id}` (`{"yes": true}`).

### Part 5c: memory

The agent remembers what you tell it and what you decide, and uses it so it never asks twice:

| What | Examples | Used for |
|---|---|---|
| **Answers** | Your yes/no to direction questions, "which role?" answers, pay and notice period | **Asked at most once**: before any question, memory is checked |
| **Decisions** | Company keep/reject, brand calls, act/skip/👍/👎 on roles **with your reason** | Job grading sees your most similar past decisions as examples |
| **Patterns** | "You usually pass on sales analytics roles (5 skipped vs 0 pursued)", formed only once it repeats | A strong pattern becomes a **yes/no suggestion** to edit your direction, never a change by itself |

**Ageing:**
- pay and notice-period answers need re-confirming after about 6 months;
- patterns that stop repeating fade;
- anything unused for a year is **archived** (moved aside, not deleted) unless you **pin** it.

**Why you skipped.** A reason is optional. In the dashboard it will be one tap: too junior, too senior, not my field, location, company, pay, hours/travel. For now, use `feedback <role> skip --why location`. Without a reason, the agent works out a **probable** one from the role's facts (level, pay, brand, city, hours, years asked) and labels it "probably". Your own reason always wins.

**Patterns by reason, not just by role type.** Examples: "you usually skip roles in Pune", "you usually skip roles paying below ~X". A strong pattern becomes the matching yes/no question, such as "remove Pune from your preferred locations?" or "only consider roles from ~X LPA?". Skips explained by pay or location don't count against the role type. Pay patterns stay on your machine.

**When you say yes to including a kind of role you used to pass on** (e.g. ML), the agent sorts each old skip by its reason:
- skips about the field are set aside, with a note and Undo (`memory undo <code>`);
- skips about other things (too junior, pay…) are kept;
- you're asked only about the ones it can't explain.

Any "you usually pass on it" pattern is set aside at once.

**After a resume update,** memories that clearly clash with your new profile (for example "skipped: too senior" after a promotion) are shown to you to **keep** or **forget**. The agent never changes or deletes a memory itself.

**Privacy:** your pay and notice period stay on your machine, and are never sent to the AI service, even for the summary.

```bash
docker compose exec api python -m app.cli memory                  # what the agent believes about you
docker compose exec api python -m app.cli memory list             # every memory (--kind answer|precedent|taste_signal, --all)
docker compose exec api python -m app.cli memory search "travel"
docker compose exec api python -m app.cli memory pin 12           # unpin | forget | restore | keep
```

API: `GET /api/memory` · `GET /api/memory/search?q=` · `GET /api/memory/beliefs` · `POST /api/memory/{id}/{pin|unpin|forget|restore|keep}`.

## Configuration

- **Secrets and API keys:** `.env`. Only `ANTHROPIC_API_KEY` is required. Everything else is an optional free tier, and the app degrades gracefully without it.
- **Rules:** `config/*.yaml`. They're read-only in the app. To change one, edit the file and run `docker compose restart`.
- **Embeddings:** a small model runs locally on your server (`BAAI/bge-small-en-v1.5` via fastembed). It needs no key and costs nothing, and your resume text never leaves your server.

## Costs

You pay only for the AI, on your own Anthropic key. Everything else is a free tier, and every one of them is optional.

**AI (Anthropic).** Two models: a cheap one (`fast`, Claude Haiku) for reading websites, postings and replies, and a stronger one (`judge`, Claude Sonnet) for judging companies, grading roles against you, and drafting emails. The agent's own estimate is about **$3–8 a month** once it's settled; the first weeks cost more, while it judges every company it finds for the first time. Your real spend is shown by `docker compose exec api python -m app.cli stats`.

| What uses the AI | Model | Roughly |
|---|---|---|
| Judging a new company | judge | once per company; again only when your rules change or you press **Judge again** |
| Reading a job description | fast | once per new posting (an unchanged posting is never read again) |
| Grading a role against your résumé | judge | once per role that passes the filters, with a daily cap; again only when your résumé or profile changes |
| Drafting an outreach email | judge | about 2 cents a draft; shown before you ask for a fresh one |
| Sorting a reply | fast | once per message |
| Reading your résumé / re-working your career direction | judge | only when you change your résumé or press the button (about 3 cents) |

**Free tiers (optional; without a key, that part simply does less):**

| Service | Used for | Monthly budget the agent keeps to |
|---|---|---|
| Brave Search | company websites, careers pages, startup news, people | 600 + 300 + 800 + 80 searches (`config/scouts.yaml`, `company_rules.yaml`, `outreach.yaml`) |
| Hunter | email finder and verifier | 20 finds, 5 domain searches, 40 checks |
| Apollo | email finder (after Hunter) | 50 |
| MyEmailVerifier | mailbox checks | 100 a day |
| Adzuna, Jooble | job-site scouts | their free API limits |

Settings shows how much of each is used this month.

## Honest notes

- **It's only on while your machine is.** See *Where to run it*.
- **People and addresses are often not found.** Expect many roles to end up with no one to write to, especially at small companies, and many addresses at large companies to stay **not proven** (their mail servers accept any address). An unproven address is never emailed without your explicit **Approve anyway**. The role still goes to your apply list either way.
- **Public data only.** The agent never logs into anything but your own Gmail, and never crawls LinkedIn, Indeed or Glassdoor: people are found from public search snippets. Work at a Startup is read from its public pages only (your choice; see Company discovery). It respects `robots.txt` and rate-limits itself on every site. Whether reading a given careers portal fits its terms of use is **your judgment**: use it accordingly.
- **It never speaks for you.** No email goes out without your approval of that exact text, it never replies or follows up, never fills in an application form, and never edits your résumé.
- **Your data stays with you.** Everything is in your own database; embeddings run locally; your pay and notice period are never sent to the AI, and your pay is shown only on the Profile screen (never in the mail).
