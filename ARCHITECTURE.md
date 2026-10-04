# Job Agent — v1 Architecture & Build Specification

**Purpose:** a self-hosted, open-source AI agent that autonomously discovers job openings relevant to its owner, finds and emails the right humans at those companies (with owner approval of every email), queues application links for manual filling, and tracks the entire pipeline — running 24/7 whether or not the app is open, interacting with its owner through one twice-daily summary email.

**This document is the build brief for Claude Code.** Every design decision below is locked unless marked ⏳ (parked) or 🚩 (v1.5). Build v1 exactly as specified.

---

## 0 · Platform foundation

| Concern | Decision |
|---|---|
| Deployment | Docker Compose, single `docker compose up`. Self-hosted, single-user per install. Requires an always-on host (VPS / home server); README must state a sleeping laptop won't work. |
| Backend | Python + FastAPI |
| Background work | Celery worker + beat scheduler, Redis broker. The scheduler is what makes the agent run unattended. |
| Database | Postgres + pgvector (one DB: all state, memory, embeddings) |
| Frontend | Next.js dashboard (UI reference: `job-agent-console.html` prototype — seven screens, flashcard grammar, semantic stripe colors: red = needs you, blue = new/tracking, green = done) |
| LLM | Anthropic API, bring-your-own-key (required). Use a cheap fast model for extraction/parsing, a stronger model for judgment calls (scoring, verdicts, drafting). |
| Search | Brave Search API free tier (~2,000/mo), bring-your-own-key, optional |
| Contact enrichment | FinalScout + Hunter + Apollo free tiers, pooled as one credit budget, all optional |
| Email verification | MyEmailVerifier (100/day free) primary, EmailDetective (1,000/mo) secondary, pooled behind one `EmailVerifier` plugin interface; local SMTP handshake attempted first where possible. Zero keys configured → construction-only mode with low-confidence handling. |
| User mailbox | User's own Gmail via OAuth (send + read scopes; app reads only threads it started, tracked by message IDs). User creates own Google Cloud OAuth credentials — first-class setup docs required; app-password SMTP/IMAP fallback mode. Never a relay service. |
| Cost posture | LLM ~$3–8/mo steady state; everything else free tiers. README states this honestly. |
| Config | All rules ship as YAML files in the repo, loaded at startup, **read-only in the dashboard**. Editing = edit file + restart, by design. Per-user customization = fork and edit. |

**Timezone/locale:** owner is India-based; quiet hours and PPP logic default accordingly but are config.

---

## 1 · Module 1: Company shortlisting

Answers: *which companies should the agent watch?* Fully autonomous; no human gates. Five components.

### 1.1 Company rules (`config/company_rules.yaml`, locked)
- **Profiles:** a company is judged by the profile matching its type.
  - `startup` profile: (a) **viability proxy** — raised within 24 months OR actively hiring, AND no distress signals (layoffs/shutdown news). Unknown → **pass**. (b) **novelty** — hard category blocklist first (e-commerce, food delivery, quick commerce, generic marketplaces → instant reject, no LLM); then LLM differentiation judgment ("novel problem vs. Nth clone") with stored precedents injected. Borderline → **reject**.
  - `established` profile: LLM comparative judgment vs. the owner's employment history — brand/growth/desirability classified up / lateral / down **relative to their resume**; keep up + lateral, reject down. Unknown → **reject**. "Step-up brand" is domain-relative (e.g., Uber can beat Pfizer for an analytics career). MNC universe comes from a seed list, not discovery.
  - `shared` block: geography — India-based OR foreign-but-hires-remote-from-India/sponsors (lenient at company level; strict enforcement is per-posting in Module 2). Avoid list: staffing agencies, outsourcing/services shops.
- **No review queue.** Decisive defaults as above; everything logs to the audit log with one-tap override → override stored as precedent.

### 1.2 Scouts (feeders → one candidate pipe)
- **YC directory scout** (daily): public YC company directory, pre-filtered by batch/industry; rich fact cards.
- **Aggregator scout** (daily): Adzuna/Jooble/Remotive free APIs queried with target-role keywords; emits the **companies** behind results (job link is a breadcrumb; company is the prize).
- **HN scout** (monthly + mid-month rescan): "Ask HN: Who is hiring?" via official HN API; LLM parses comments → companies + any **contact emails** (stored for Module 3).
- **Seed list loader** (`config/seed_companies.yaml`, on startup/change): owner's MNC list + known startups. **Seeds are judged like everyone else** (no evaluator bypass; optional `trusted: true` flag exists but unused by default).
- All scouts: incremental (remember what they've seen), isolated failures, per-scout health shown in dashboard.
- Candidate pipe: never-seen companies only (dedupe key = normalized domain).

### 1.3 Enrich + dedupe → fact cards
- Identity resolution: domain = true ID; fuzzy name merge; union of source data.
- Enrichment layers, cheapest first, stop early: feeder data → company website (LLM extracts what they build, size hints, locations, hiring status) → targeted web search only for rule-required missing facts (funding recency, distress).
- Every fact tagged: **verified / inferred / unknown** — unknown handling per rule policy.
- Type-adapted: startups get the full recipe (monthly refresh); MNCs mostly LLM general knowledge + one careers-page check for **India presence** (quarterly refresh).

### 1.4 Evaluator
- Order: shared checks (deterministic, free) → profile checks (deterministic parts first, then at most ~1 LLM call).
- Output: binary **pass/fail** + structured reasoning → audit log. Re-runnable: verdict = f(fact card, rules version, precedents).
- **Rejected companies can return:** triggers = (a) fact card refresh changes a fact relevant to the stored failure reason, (b) rules change touching that reason, (c) owner override. One re-evaluation per trigger; permanent grounds (blocklist, owner thumbs-down) never auto-revive.

### 1.5 Watchlist + gardening
- **Activation** (on pass, queued, idempotent, rate-limited):
  1. Find careers page: URL guesses (`/careers`, `/jobs`, `jobs.` subdomain) + homepage nav scan → **web search fallback** (guard against landing on LinkedIn/Glassdoor) → LLM-assisted navigation. Every candidate page **validated** (actually contains listings/apply links) before trust.
  2. Detect reading method — the ladder, cheapest rung that works: known ATS public endpoints (Greenhouse boards API, Lever postings, Ashby, Workable, SmartRecruiters, Recruitee) → Workday JSON pattern → **per-company adapters for giants** (Google/Amazon-class internal careers-JSON endpoints; community-maintained adapter files) → aggregator coverage as guaranteed floor → constrained Playwright+LLM fallback for small custom pages (use the site's own filters, never crawl catalogs).
  3. **Verify feed ownership** before recording (slug-collision guard: payload company name / job URLs must point back at the company's domain).
  4. Write tracking record (source, slug, cadence, health) → **first sync immediately**.
- **Gardening** (weekly): cadence tuning (active 6-hourly ↔ dormant weekly, promote on signs of life); health checks + self-healing (loud break → retry/backoff → re-run activation; quiet staleness caught via "aggregator sees jobs our feed doesn't" cross-check + quarterly re-verification; ATS vendor migrations heal via re-activation); pruning (90-day silent → demote, further 90 → archive; owner thumbs-down → permanent blocklist; usage signals promote/demote); refresh scheduling (startup cards monthly, MNC quarterly, rejects slow-lane).

---

## 2 · Module 2: Job shortlisting

Answers: *which openings at watched companies matter, and how much?* Fully autonomous. Four components.

### 2.1 Job rules (`config/job_rules.yaml`, locked) — three kinds, cheapest enforced first

**Kind 1 — hard filters (deterministic):**
- **Titles: derived, not configured.** The Career Direction Profile (Module 5) supplies progression/lateral/excluded role families; a broad title net kills obvious noise; real family+rung classification happens from the **description, not the title** (stated YoE, scope) during the Kind-2 LLM pass. Title = hint; description = truth.
- **Seniority: sliding ladder, resume-anchored.** Target = current rung (with conditions) + one rung up. Nothing below; nothing 2+ rungs up; no IC→management track jump unless the direction profile says so. **Laterals require step-up brand AND senior-level within the family.** **Tenure-aware lateral appetite:** ~6mo–1yr in role → laterals primary; 1–2yr → fine; 2+yr → laterals penalized in scoring (never hidden). Slides automatically as the resume changes.
- Employment type: **full-time only**.
- Posting age ≤ 21 days.
- **Salary, relative:** stated pay must beat current comp. Branch by where money is spent: remote-paid-from-abroad → nominal conversion; relocation → **PPP-adjusted** (World Bank factors, shipped + auto-refreshed yearly). Borderline (±15%) → pass to scoring flagged "comp: marginal". Unstated → LLM/web estimate with confidence: clearly above → proceed; clearly below → reject (logged, overridable); uncertain → through, flagged "comp: unverified". **Salary and seniority are independent gates — money never buys back a failed ladder check.**

**Kind 2 — text rules (one LLM pass reads the description; also does level/family inference):**
- Dealbreakers (instant reject): geography/visa — role doable from India without candidate-arranged visa (remote-from-anywhere, India location, or explicit sponsorship; LLM reads fine print like "US work authorization required"); **6-day work week**.
- Penalty: **services-flavored work at middle-tier brands: −10** (prestige-tier services exempt; mass-tier died in Module 1).
- Ignored but flagged in digest: travel, relocation-onsite, on-call, night shift (distinguish rigid graveyard shift vs. overlap-flexibility — flag "hours: US-aligned shift" / "hours: confirm", no score impact).

**Kind 3 — fit definition:** must-have/nice-to-have skills auto-drafted from resume (owner corrects once at onboarding); scoring dimensions start at **equal weights**, tuned by feedback (skips/acts/thumbs with reasons); weight changes logged, never silent. Bands: **85+ fast lane · 65–84 Inbox · 40–64 Maybe · <40 archive**.

### 2.2 Intake & dedupe
- Normalize all sources to one canonical JobPosting schema.
- **Link resolution:** aggregator links followed to canonical home; dead-ends = lead only, never acted on. Act only on confirmed canonical postings.
- Dedupe sightings: fingerprint (company+title+location) + embedding similarity; direct source authoritative; re-syncs update `last_seen` only.
- **Sibling clustering** (same role, different req IDs): scored once per cluster, one card; owner's policy: **apply to every sibling's link; one outreach email listing all req IDs.** Genuinely different roles (different team/requirements) are not clustered; borderline shown adjacent with a note.
- Closure detection (feed disappearance / dead apply URL → closed; removed from Inbox; noted if applied). Content-hash: unchanged repost reuses old score and old owner decision; material change → re-scored once, flagged "updated".
- **Reopening matrix** (reads outcome stamps written by Module 3/4 tracking): silent death → re-surface immediately, confident persistence framing; explicit rejection pre-interview → 30-day cooldown; rejected post-interview → 90-day cooldown, warm framing. **Reopening emails never open with or dwell on the past outcome.** Where the ATS blocks re-application, route via recruiter email instead.

### 2.3 Scoring funnel
- Stage 1: pgvector cosine similarity (resume+direction-profile vector vs posting vector); top ~20/day advance; the rest band directly by similarity.
- Stage 2: LLM grade → structured JSON: `score, reasons, flags, missing (skill gaps — feeds outreach drafting), one_liner`.
- Re-score triggers only: content hash change, resume/weights change (lazy, on next touch). Same job re-seen (any req ID within a cluster) is never re-scored.

### 2.4 Routing
- Band placement; unresolved-question flags cap out of fast lane.
- Fast lane → immediate ping + **auto-prepare** outreach drafts (never auto-send). Inbox → next summary mail, ranked. Maybe/archive → silent.
- Quiet hours on pings; multiple fast-laners batch into one ping.
- Every routed card instrumented for feedback capture.

---

## 3 · Module 3: Cold outreach

Answers: *who to email and how — with the owner's voice protected.* **Human gate is permanent: every email is reviewed and approved individually before anything sends. No auto-send mode exists for outreach.**

### 3.1 Contact discovery
- **Designated-contact override (strict):** if the posting names an address ("email us at…"), that address is the ONLY channel for the role — no bench, no hunting, no escalation on silence. Manual owner override only.
- **Store first:** contacts accumulate passively (HN scout, postings, careers pages, past runs). Freshness: 6-month on-use re-verification (LinkedIn-snippet person check + mailbox check). **Routine sweeps:** hot contacts (open threads/active roles) monthly, warm quarterly, cold on-use only (~50–80 searches/mo budget). Positive proof of departure retires a contact; ambiguity never does (mailbox history trumps). Every real send passively re-verifies.
- **Person search: LinkedIn-scoped search snippets ONLY** (queries shaped to pull linkedin.com results; parse public snippets; NEVER log into or crawl LinkedIn). Identity = full fingerprint: name + title + location + snippet + mailbox history — never name alone. Same-name collisions: fingerprint disambiguates; truly ambiguous → freeze, don't guess; co-suspects share one bench slot.
- **Target selection by company type:** startup → founders; mid-size → TA + hiring manager; MNC → recruiter per function+geography.
- **Address ladder (owner's order):** published → **pooled enrichment free tiers (FinalScout-heavy, Hunter/Apollo reserve; one credit budget, cached forever)** → construction (learn domain pattern from one known-real anchor address, else common formats; startups skew first@) → none.
- **Verification doctrine — nothing sends unproven:** DNS/MX → mailbox handshake (local attempt, then verification-API pool) → catch-all detection (fake-name probe; catch-all ⇒ permanently low-confidence: no auto-send, needs owner's explicit go-ahead or provider vouching) → mailbox history overrides all (delivered/replied = proven).
- **Bench: eager one-shot fill** to caps — MNC **7** per function+geo · mid-size **5** · startup **5 (2 founders + 3 recruiting)**. Unfillable slots stay open (sweeps top up); construction is the overflow when credits run dry. One human = one bench slot (address-merge on same person). Rotation order: function/geo fit → recruiter seniority → confidence → history.
- No contact found (~30–50% of companies, be honest in README): role proceeds to apply anyway; retry in 2 weeks.

### 3.2 Draft engine
- **ONE draft per role** — identical body for every recipient, only the greeting name swapped. No per-role-type angle variation (owner's decision; accepted trade-off).
- Inputs: posting, profile/resume, fact card, scoring output (address the `missing` gap), scenario.
- Scenarios: fresh · reopening-after-silence · post-rejection (never opens with the rejection) · post-interview · multi-req (list all sibling req IDs).
- **Truthfulness verifier (hard requirement):** second cheap LLM pass — every factual claim must trace to resume/profile; ungrounded claims rewritten or struck before the draft reaches the owner.
- ⏳ **Email template structure: PARKED — owner will author.** Ships as one swappable template file with a sane placeholder. Resume attached per template.

### 3.3 Send + safety
- Identity: owner's Gmail OAuth (see Platform). Sent mail appears in their Sent folder.
- **Approval flow (owner-specified):** campaign drafts (all recipients) prepared → summary mail flags "approval pending → [Review in app]" → Review screen: step through each final email exactly as it will send (recipient, subject, body, resume attachment visible) → **per-email Approve / Edit / Skip** → all reviewed → wave scheduler starts.
- **Waves: day 0 (primary + one more) → day 1 (next two) → day 2 (rest)**, each wave only on continued silence.
- Throttle stack (unbypassable, in order): quiet hours (recipient-local best-effort) → global cap 12/day → per-company cap 2/day → stagger 30–60 min → **kill-switch**.
- **Kill-switch:** ENGAGED human replies (interest, questions, forwarded-to-team) and REJECTIONS cancel all pending sends for that company instantly, even approved ones. **OOO/auto-replies do NOT cancel** (log, shift that contact past return date). **Redirects/"not the right person" do NOT cancel** (owner's decision) — wrong-target contact marked responsive-but-wrong, named right person joins bench at front of next wave. Ambiguous → pause that company, show owner the raw email.
- **Bounce breaker:** trips at 2+ bounces OR >5% over 30+ sends → freeze unsent queue only (rest of app unaffected) → self-cleanup (delete dead address, hunt replacement, re-verify entire queue via API pool; untrust the guilty construction pattern for that domain) → **self-resume** when queue verified-clean + 6h cooldown → repeat trip within 7 days: self-fix if conservative (e.g., distrust construction at domain), ask owner only for account-level/judgment fixes.
- Audit every send: recipient, wave, draft version, outcome.

### 3.4 Threads + follow-ups
- Watch replies to agent-started threads only. Classify: interested / forwarded / rejection / OOO / question — confidence-gated (unsure → raw email to owner, no suggested action).
- **The agent NEVER writes replies and NEVER sends follow-ups** (owner's decisions). A human reply = ping owner with Gmail link; conversation is entirely theirs. Campaign ends when waves end; silence reported in summary ("no responses — follow up yourself if you'd like") and stamped as silent-death for the reopening matrix.
- Statuses flow to Applied tab, mailbox history, reopening matrix, pipeline pulse.

---

## 4 · Module 4: Apply tracker (v1 — NO form filling)

Owner's decision: **the agent never fills application forms in v1.** Module is a lean tracker:

1. **Queue the link:** pursued/fast-lane roles → summary mail carries "Apply manually: [role] — [Apply link]" (canonical company-form link, live-checked before inclusion; sibling req links included per apply-to-all policy).
2. **Watch for proof:** ATS confirmation emails in the owner's inbox matched to queued roles → auto-marked **Applied** with timestamp + receipt link. No confirmation (~10–20% of ATSs) → manual "Mark applied" toggle per entry.
3. **Reminder policy:** un-actioned link rides the mail as a full card, folds into a compact "still pending (N)" line after 3 days, disappears when applied/skipped/posting closes.
4. **Track the lifecycle:** ATS status emails classified → applied → confirmed → in review → interviewing → offer/rejected/silent. Stamps feed the reopening matrix; visible in Applied tab and pipeline pulse. Outreach and application for a role are linked into one story.

🚩 **v1.5 (design for, don't build):** structured-ATS auto-fill (Greenhouse/Lever/Ashby only) behind an end-of-day batch-review gate with per-answer display + truthfulness verifier on composed answers; fill-and-submit in one fresh session at review time (session-timeout aware); weekly application budget; enterprise-portal (Workday) robots explicitly out of scope forever. Schema note: queue entries, review-gate pattern, and Applied tracking are shaped to receive this without rework.

---

## 5 · Module 5: Profile & memory

### 5.1 Fixed core (~12 typed fields, onboarding ≈ 5 min)
Identity/links · resume file · **current compensation** (salary rule) · **current role + start date** (tenure/ladder) · **employment history** (brand comparisons) · visa status (geography dealbreaker) · notice period (drafts). Onboarding: resume upload first → parse pre-fills → owner confirms → only asks what a resume can't say. Edits ripple immediately; time-sensitive fields (comp, notice) carry visible "last confirmed" chips and re-confirm via one summary-mail line every ~6 months **on use**.

Profile screen order (owner-specified): identity header → resume card → employment history (with rule annotations: tenure, current rung, promoted ↑) → rule-driving fields with freshness chips. Notification settings live in Settings, not Profile.

### 5.2 Career direction profile (derived)
LLM reads resume → structured judgment: position/rung/tenure state · trajectory · progression families · lateral families + conditions · exclusions · lateral-appetite level — each with a one-line why. Own screen, fully editable. **Owner edits are durable overrides layered on the derived base:** resume update regenerates the base, re-applies overrides; incoherent overrides (e.g., promotion vs. "no management") → one question in summary mail. Behavioral feedback tunes weights, not this document; at most the mail may *suggest* a family edit (one tap, never automatic).

### 5.3 Open memory layer (one table + pgvector)
Entry = kind (answer | precedent | taste_signal) · meaning (+embedding) · content · context · provenance · freshness (expires/evergreen) · usage · status. Writes: parked-question answers, verdict/score overrides, reasoned thumbs-downs, aggregated taste patterns (threshold-gated). Dedupe gate before insert (semantic match → update, don't fork). Reads: pulled by meaning, never dumped — drafts pull relevant answers, judgments pull top-k precedents as examples, and **any question about to be asked first searches memory** (asked-at-most-once guarantee). Aging: money/timing answers expire to needs-reconfirm (on use); precedents evergreen but superseded-with-history; taste signals decay; unused-1yr → archived. Memory screen: browse/search/edit/delete/pin + "what the agent believes" summary.

### 5.4 Resume handling (owner-simplified)
**PDF-only upload, replace-in-place (no version history), attached as-is (no renaming).** Content hash kept (change detection only). Flow: upload replaces → parse → **owner confirms parse (mandatory — the one anti-silent-corruption gate)** → ripple in dependency order: fixed core (tenure) → direction profile (base + overrides) → ladder/comp anchors → embeddings → lazy re-score of open jobs. **The agent never edits or tailors the resume** (tailoring = possible v2, out of scope).

---

## 6 · Module 6: Summary mail & dashboard

### 6.1 The twice-daily summary mail — the app's single channel
Times config (default 08:00 / 20:00 IST). Subject = compressed counts ("2 need you · 3 new matches · 3 updates · 6 in flight"). **Unified flashcard grammar in every section** (title = role · company; one gray detail line; status corner; ≤2 buttons; left stripe: red=needs you, blue=new, green=done, neutral=tracking):
1. **⚡ Needs you** — email approvals ("[Review drafts →]"), apply links ("[Open apply link →]" + pending counter), parked questions with pre-filled suggestions, re-confirmations, rare escalations. Absent when empty.
2. **📥 New matches** — fast-laners first, then Inbox ranked; buttons: **"Draft outreach"** (label is exact: prepares drafts for review, does NOT send) / Skip. Maybe appears as a count only.
3. **📤 What happened** — **grouped per opening** (role header, events beneath: waves sent, replies classified w/ Gmail links, applications confirmed); non-role events = one gray "Elsewhere" line.
4. **📊 Pipeline** — every in-flight opening, one card, colored status chip (live / on you / tracking / silent), sorted red→green→blue→gray; deep-links to role detail.
5. **🔧 Housekeeping** — one gray line.
Long sections truncate to top cards + "+N more in app". Quiet day → short note or skip (config). Action links: signed single-purpose tokens, deep-link to the exact screen, expire with their referent; trivial confirms resolvable from the mail.

### 6.2 Real-time pings — exactly two
Fast-lane find (drafts auto-prepared, never auto-sent) · recruiter reply ("[Open in Gmail]"). Quiet-hours aware (queued to morning; config exception), batched, individually toggleable, email by default. **Nothing else may interrupt the owner.**

### 6.3 App screens (seven) — UI reference: `job-agent-console.html`
Jobs (Inbox/Fast lane/Maybe/Applied tabs; Draft outreach · Skip-with-reason · Maybe-rescue) · Role detail (one opening's whole story) · **Review** (step through final drafts, resume attachment visible, per-email Approve/Edit/Skip → arms waves) · Companies (watchlist states, fact cards, benches, verdicts with one-tap override) · Profile (§5.1 order) · Memory (§5.3) · Settings (mail times, ping toggles, quiet hours, API keys with quota bars, read-only rule viewers, export-all/delete-all). Everything deep-linkable; nav badges: blue = new count, red = pending approvals.

### 6.4 Audit log
Every consequential machine decision: what, why (rule/reasoning), what it affected. Filterable, plain-language with expandable detail, judgment entries carry one-tap override→precedent. Zero notification weight. ~12-month retention then compaction.

---

## 7 · Cross-cutting requirements

1. **Politeness everywhere:** global per-domain rate limiter, honest user-agent, robots.txt respected on scrape paths.
2. **Hard boundaries:** never log into/crawl LinkedIn, Indeed, Glassdoor, or Work-at-a-Startup; snippets from search results are the only LinkedIn data. No relay email services. Agent never speaks in conversations, never follows up after waves, never fills forms (v1), never edits the resume, never sends an unapproved or unverified-address email (catch-all = low-confidence = owner's explicit go-ahead required).
3. **Idempotency as repair:** activation re-runs are the healing mechanism; verdicts and scores are re-runnable pure functions; dedupe everywhere (companies, postings, contacts, memory entries).
4. **Honest README:** always-on host needed; OAuth setup walkthrough; contact hit-rate expectations; "use at your own judgment" note re portal ToS; cost table; every free-tier signup optional with graceful degradation.
5. **Open-source contribution surfaces:** company seed lists, giant-company adapters, ATS detectors, verifier/enrichment plugins.
6. **Suggested build order = module order.** Each module is independently testable: M1 alone → live watchlist; +M2 → scored daily feed; +M3 → gated outreach; +M4 → tracked pipeline; +M5/M6 → full product.

## 8 · Open items
- ⏳ Email template structure (owner will author; one swappable file).
- 🚩 v1.5 structured-ATS auto-fill (designed-for, not built).
