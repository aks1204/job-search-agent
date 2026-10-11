"""The one HTTP client every outbound call goes through (spec §7.1: politeness everywhere).

- Honest user-agent pointing at the project.
- Per-host rate limit shared across all worker processes (via Redis; falls back
  to in-process timing if Redis is unavailable).
- robots.txt respected on scrape paths (`scrape=True`); public APIs skip it.
"""

import logging
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import redis

from app.core.redis_client import get_redis

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO; some APIs (Adzuna, Jooble) carry keys in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

USER_AGENT = "JobAgent/0.1 (self-hosted job-search assistant; +https://github.com/aks1204/job-search-agent)"
ROBOTS_AGENT = "JobAgent"

DEFAULT_INTERVAL = 1.0  # seconds between requests to the same host
HOST_INTERVALS = {
    "hacker-news.firebaseio.com": 0.05,  # official API, built for this
}


class RobotsDisallowed(Exception):
    pass


class PoliteClient:
    def __init__(self, *, default_interval: float = DEFAULT_INTERVAL, use_redis: bool = True, timeout: float = 20.0):
        self._http = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
        self._default_interval = default_interval
        self._use_redis = use_redis
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._http.close()

    def _interval(self, host: str) -> float:
        if self._default_interval <= 0:
            return 0.0
        return HOST_INTERVALS.get(host, self._default_interval)

    def _wait_turn(self, host: str) -> None:
        interval = self._interval(host)
        if interval <= 0:
            return
        if self._use_redis:
            try:
                r = get_redis()
                deadline = time.monotonic() + 120
                while not r.set(f"ratelimit:{host}", "1", nx=True, px=max(1, int(interval * 1000))):
                    if time.monotonic() > deadline:
                        log.warning("rate-limit wait for %s exceeded 120s; proceeding", host)
                        break
                    time.sleep(min(interval, 0.25))
                return
            except redis.RedisError:
                pass
        wait = self._last.get(host, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def _robots_allow(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            parser: RobotFileParser | None = None
            try:
                resp = self.request("GET", f"{base}/robots.txt")
                if resp.status_code >= 500:
                    parser = RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])  # server trouble: back off
                elif resp.status_code < 400:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                # 4xx: no robots.txt -> everything allowed (parser stays None)
            except httpx.HTTPError:
                parser = None
            self._robots[base] = parser
        parser = self._robots[base]
        return parser is None or parser.can_fetch(ROBOTS_AGENT, url)

    def request(self, method: str, url: str, *, scrape: bool = False, **kwargs) -> httpx.Response:
        if scrape and not self._robots_allow(url):
            raise RobotsDisallowed(url)
        self._wait_turn(urlsplit(url).hostname or "")
        return self._http.request(method, url, **kwargs)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)
