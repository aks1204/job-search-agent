"""Brave Search (optional, bring-your-own-key). Every caller spends from a named monthly budget."""

from dataclasses import dataclass

from app.core.http import PoliteClient
from app.core.quota import try_consume
from app.settings import get_settings

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str


def search_available() -> bool:
    return bool(get_settings().brave_api_key)


def web_search(
    http: PoliteClient, query: str, *, purpose: str, monthly_budget: int, count: int = 5
) -> list[SearchResult] | None:
    """Results, or None when no key is configured or this purpose's budget is spent."""
    key = get_settings().brave_api_key
    if not key or not try_consume(f"brave:{purpose}", monthly_budget):
        return None
    resp = http.get(
        BRAVE_URL,
        params={"q": query, "count": count},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    resp.raise_for_status()
    results = (resp.json().get("web") or {}).get("results") or []
    return [SearchResult(r.get("title", ""), r.get("url", ""), r.get("description", "")) for r in results]
