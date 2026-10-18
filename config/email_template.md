---
# Outreach email template (Module 3, Part 3b). Read-only in the dashboard: edit, then `docker compose restart`.
# The part between the --- lines is settings; below it is the email exactly as it goes out, then the
# instructions the AI follows for the parts it writes.
#
# Placeholders filled in by the app:
#   {first_name}    recipient's first name (the ONLY thing that changes between recipients)
#   {role}          the job title, cleaned up        {role_short}  shortened for the subject
#   {company}       company name                     {years}       your total years of experience
#   {req_ids}       " (job IDs: R-1, R-2)" when one role has several openings, else nothing
#   {full_name} {mobile} {linkedin_url}  from your private config/owner_stub.yaml
#                   (a line whose placeholder is empty is left out)
# Parts the AI writes: {{intro}} and {{points}} — see the "## intro" and "## points" sections.

subject: "Application for {role_short} in {company} ({years} YoE)"
subject_max_chars: 90
greeting: "Hi {first_name},"
greeting_without_name: "Hi Hiring Team,"   # designated addresses / hiring mailboxes
points_min: 4
points_max: 7
attach_resume: true
---
{greeting}

I came across the {role} opening{req_ids} at {company} and wanted to reach out directly, since my background maps closely onto what the role needs.

{{intro}}

{{points}}

I'd love the chance to discuss how my experience would translate to the {role} at {company}. I've attached my resume for your reference. I hope to hear back from you soon.

Best regards,
{full_name}
Mobile: {mobile}

## subject
Shorten the job title so the subject line stays short: drop location, team codes, levels in brackets and
anything after a dash or comma that isn't the core title (e.g. "Data Analyst – Consultant/Senior Consultant"
→ "Data Analyst").

## intro
Say who I am: where I work, my current role, how long I have been working, and what I do at a broad level,
and how that carries over to this opening (just a gist). At most 2-3 lines. First person.

## points
Explain, as numbered points, how my current roles, responsibilities and background line up with this opening.
Each point: a short heading naming the area, a colon, then 1-2 lines on exactly what I did there, e.g.
"Field Force Effectiveness & GTM Analytics: <what I did in that area>".
Points must not repeat each other. Pick the areas that matter most for THIS posting.

## rules
- Only highlight why I am a strong, ideal candidate. Never mention gaps, missing skills or what I lack.
- Use only facts from my resume and profile. Never invent numbers, tools, clients or titles.
- No salary, notice period or visa talk.
- Plain, confident, specific wording; no buzzword padding.
