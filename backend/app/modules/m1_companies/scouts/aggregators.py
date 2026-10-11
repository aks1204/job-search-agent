"""Job aggregators (Adzuna, Jooble, Remotive) queried with the owner's target roles.

The job is a breadcrumb; the company behind it is the prize. Each aggregator is
its own scout so one failing never stops the others.
"""

from collections.abc import Iterator

from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.scouts.base import Scout, ScoutContext
from app.settings import get_settings


def _snippet(text: str | None, limit: int = 800) -> str:
    return (text or "").strip()[:limit]


class AdzunaScout(Scout):
    name = "adzuna"

    def not_ready_reason(self) -> str | None:
        s = get_settings()
        if not (s.adzuna_app_id and s.adzuna_app_key):
            return "ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env"
        return self.roles_reason()

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        cfg, s = self.settings, get_settings()
        for role in self.target_roles():
            for page in range(1, cfg.pages_per_role + 1):
                resp = ctx.http.get(
                    f"https://api.adzuna.com/v1/api/jobs/{cfg.country}/search/{page}",
                    params={
                        "app_id": s.adzuna_app_id,
                        "app_key": s.adzuna_app_key,
                        "what": role,
                        "results_per_page": cfg.results_per_page,
                        "max_days_old": cfg.max_days_old,
                        "content-type": "application/json",
                    },
                )
                resp.raise_for_status()
                results = resp.json().get("results") or []
                for job in results:
                    company = (job.get("company") or {}).get("display_name")
                    if self.is_placeholder(company) or not job.get("id"):
                        continue
                    yield Candidate(
                        scout=self.name,
                        external_id=str(job["id"]),
                        name=company.strip(),
                        url=job.get("redirect_url"),
                        data={
                            "query": role,
                            "title": job.get("title"),
                            "location": (job.get("location") or {}).get("display_name"),
                            "created": job.get("created"),
                            "salary_min": job.get("salary_min"),
                            "salary_max": job.get("salary_max"),
                            "description": _snippet(job.get("description")),
                        },
                    )
                if len(results) < cfg.results_per_page:
                    break


class JoobleScout(Scout):
    name = "jooble"

    def not_ready_reason(self) -> str | None:
        if not get_settings().jooble_api_key:
            return "JOOBLE_API_KEY not set in .env"
        return self.roles_reason()

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        key = get_settings().jooble_api_key
        for role in self.target_roles():
            resp = ctx.http.post(
                f"https://jooble.org/api/{key}",
                json={"keywords": role, "location": self.settings.location},
            )
            resp.raise_for_status()
            for job in resp.json().get("jobs") or []:
                company = job.get("company")
                job_id = job.get("id") or job.get("link")
                if self.is_placeholder(company) or not job_id:
                    continue
                yield Candidate(
                    scout=self.name,
                    external_id=str(job_id)[:255],
                    name=company.strip(),
                    url=job.get("link"),
                    data={
                        "query": role,
                        "title": job.get("title"),
                        "location": job.get("location"),
                        "updated": job.get("updated"),
                        "salary": job.get("salary"),
                        "type": job.get("type"),
                        "source": job.get("source"),
                        "description": _snippet(job.get("snippet")),
                    },
                )


class RemotiveScout(Scout):
    name = "remotive"

    def not_ready_reason(self) -> str | None:
        return self.roles_reason()

    def _location_ok(self, location: str | None) -> bool:
        wanted = self.settings.locations_any
        if not location or not wanted:
            return True
        lowered = location.lower()
        return any(w.lower() in lowered for w in wanted)

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        for role in self.target_roles():
            resp = ctx.http.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": role, "limit": self.settings.limit},
            )
            resp.raise_for_status()
            for job in resp.json().get("jobs") or []:
                company = job.get("company_name")
                location = job.get("candidate_required_location")
                if self.is_placeholder(company) or not job.get("id") or not self._location_ok(location):
                    continue
                yield Candidate(
                    scout=self.name,
                    external_id=str(job["id"]),
                    name=company.strip(),
                    url=job.get("url"),
                    data={
                        "query": role,
                        "title": job.get("title"),
                        "location": location,
                        "published": job.get("publication_date"),
                        "job_type": job.get("job_type"),
                        "category": job.get("category"),
                        "salary": job.get("salary"),
                    },
                )
