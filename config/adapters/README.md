# Per-company adapters (for giants with their own careers JSON)

Most companies use a standard applicant-tracking system (ATS): Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee or Workday. The agent reads those on its own.

A few giants run their own careers site backed by a JSON endpoint, for example Google, Amazon or Uber. For those, drop a small YAML file here describing that endpoint. Each one should be checked by hand against the live site before it's trusted; only `amazon.yaml` ships (checked 2026-11-01). Contributions are welcome.

```yaml
# config/adapters/example.yaml
name: "Example Corp"
domain: "example.com"            # must match the company's domain in the agent
maintainer: "@your-github-handle"
request:
  method: GET                    # or POST (then use json_body)
  url: "https://careers.example.com/api/jobs"
  params: {country: "IN", q: ""}
items: "data.jobs"               # dot-path to the job list in the response
fields:                          # dot-paths inside each job item
  external_id: "id"
  title: "title"
  location: "location.name"
  url: "links.apply"
  posted_at: "posted"
  description: "summary"
  department: "team"
url_template: null               # or "https://careers.example.com/job/{external_id}" when items carry no URL
pagination:                      # optional
  param: "offset"
  size: 50
  max_pages: 5
```

For a site with tens of thousands of jobs, put `{role}` in a request value (e.g. `params: {q: "{role}"}`): the endpoint is then searched once per target role in your profile, without seniority words ("Senior Data Analyst" is searched as "Data Analyst"; the filters judge seniority). `amazon.yaml` works this way.

Files ending in `.example.yaml` are ignored. After adding a file, run `docker compose restart`, then:

```bash
docker compose exec api python -m app.cli activate --domain example.com
```
