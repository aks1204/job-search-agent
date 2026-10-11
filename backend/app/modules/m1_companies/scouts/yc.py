"""YC directory via the community yc-oss mirror (static JSON on GitHub Pages).

Using the mirror means ycombinator.com itself is never scraped.
"""

import re
from collections.abc import Iterator

from app.config_schemas import YCScout as YCSettings
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.scouts.base import Scout, ScoutContext

YC_URL = "https://yc-oss.github.io/api/companies/all.json"

_YEAR4 = re.compile(r"(20\d{2})")
_YEAR2 = re.compile(r"^[A-Z](\d{2})$", re.I)  # "W22", "S24", "X25"

_KEEP_FIELDS = (
    "batch", "one_liner", "team_size", "industry", "subindustry", "industries", "tags",
    "regions", "all_locations", "stage", "isHiring", "launched_at", "url",
)


def batch_year(batch: str | None) -> int | None:
    if not batch:
        return None
    if m := _YEAR4.search(batch):
        return int(m.group(1))
    if m := _YEAR2.match(batch.strip()):
        return 2000 + int(m.group(1))
    return None


def _as_text(value) -> str:
    if isinstance(value, list):
        return " | ".join(str(v) for v in value)
    return str(value or "")


def keep(company: dict, cfg: YCSettings) -> bool:
    if (company.get("status") or "").lower() != "active":
        return False
    year = batch_year(company.get("batch"))
    if year is None or year < cfg.min_batch_year:
        return False
    if cfg.require_hiring and not company.get("isHiring"):
        return False
    if cfg.regions_any:
        haystack = f"{_as_text(company.get('regions'))} | {_as_text(company.get('all_locations'))}"
        # Whole words only: "India" must not match "Indiana, USA".
        if not any(re.search(rf"\b{re.escape(r)}\b", haystack, re.I) for r in cfg.regions_any):
            return False
    return True


class YCScout(Scout):
    name = "yc"

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        resp = ctx.http.get(YC_URL)
        resp.raise_for_status()
        companies = resp.json()
        ctx.state["last_total"] = len(companies)

        for c in companies:
            if not keep(c, self.settings) or self.is_placeholder(c.get("name")):
                continue
            data = {k: c[k] for k in _KEEP_FIELDS if c.get(k) not in (None, "", [])}
            if c.get("long_description"):
                data["long_description"] = c["long_description"][:1500]
            yield Candidate(
                scout=self.name,
                external_id=str(c.get("id") or c.get("slug") or c["name"]),
                name=c["name"],
                domain=c.get("website"),
                url=c.get("url"),
                type_hint="startup",
                data=data,
            )
