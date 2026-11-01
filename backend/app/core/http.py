"""The one HTTP client every outbound call goes through (spec §7.1: politeness everywhere).

- Honest user-agent pointing at the project.
- Per-host rate limit shared across all worker processes (via Redis; falls back
  to in-process timing if Redis is unavailable).
- robots.txt (read per RFC 9309, Crawl-delay included) respected on scrape paths (`scrape=True`), at every
  redirect too; public APIs skip it.
"""

import logging
import re
import time
from urllib.parse import urlsplit

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


MAX_REDIRECTS = 10
MAX_CRAWL_DELAY = 30.0  # a site asking for a longer Crawl-delay is waited on this long


class RobotsDisallowed(Exception):
    pass


def _rule_pattern(path: str) -> re.Pattern:
    """A rule's path with its `*` (anything) and trailing `$` (end of address) wildcards."""
    anchored = path.endswith("$")
    body = path[:-1] if anchored else path
    return re.compile(".*".join(re.escape(part) for part in body.split("*")) + ("$" if anchored else ""))


class Robots:
    """robots.txt read the way RFC 9309 says and Google does — Python's own reader does none of this:
    - rules are grouped by their User-agent lines; a blank line doesn't end a group (careers.walmart.com has
      one straight after `User-agent: *`, and Python's reader then dropped every rule);
    - every group naming us is merged (higher.gs.com has two `*` groups; Python's reader kept only the last);
    - the longest matching rule wins, Allow on a tie, with `*` and `$` wildcards (`Disallow: /*/apply`).
    Also keeps the Crawl-delay those groups ask for."""

    def __init__(self, text: str):
        groups: list[tuple[set[str], list[tuple[bool, str]], float | None]] = []
        agents: set[str] = set()
        rules: list[tuple[bool, str]] = []
        delay: float | None = None
        in_rules = False
        for raw in text.lstrip("﻿").splitlines():   # (a byte-order mark would hide the first User-agent)
            key, sep, value = raw.split("#", 1)[0].partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:                         # a User-agent line after rules starts the next group
                    groups.append((agents, rules, delay))
                    agents, rules, delay, in_rules = set(), [], None, False
                agents.add(value.split("/")[0].strip().lower())
            elif key in ("allow", "disallow") and agents:
                in_rules = True
                if value:                            # an empty Disallow allows everything: no rule
                    rules.append((key == "allow", value))
            elif key == "crawl-delay" and agents:
                in_rules = True
                try:
                    delay = float(value)
                except ValueError:
                    pass
        if agents:
            groups.append((agents, rules, delay))
        chosen = [g for g in groups if ROBOTS_AGENT.lower() in g[0]] or [g for g in groups if "*" in g[0]]
        self._rules = [(allow, _rule_pattern(path), len(path)) for g in chosen for allow, path in g[1]]
        delays = [g[2] for g in chosen if g[2] is not None and g[2] > 0]
        self.crawl_delay = min(max(delays), MAX_CRAWL_DELAY) if delays else None

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None
        for allow, pattern, length in self._rules:
            if pattern.match(path) and (best is None or length > best[0] or (length == best[0] and allow)):
                best = (length, allow)
        return best is None or best[1]


class PoliteClient:
    def __init__(self, *, default_interval: float = DEFAULT_INTERVAL, use_redis: bool = True, timeout: float = 20.0):
        self._http = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
        self._default_interval = default_interval
        self._use_redis = use_redis
        self._last: dict[str, float] = {}
        self._robots: dict[str, Robots | None] = {}
        self._crawl_delay: dict[str, float] = {}   # host -> the Crawl-delay its robots.txt asks for

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._http.close()

    def _interval(self, host: str) -> float:
        if self._default_interval <= 0:
            return 0.0
        return max(HOST_INTERVALS.get(host, self._default_interval), self._crawl_delay.get(host, 0.0))

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
            robots: Robots | None = None
            try:
                resp = self.request("GET", f"{base}/robots.txt")
                if resp.status_code >= 500 or resp.status_code == 429:
                    robots = Robots("User-agent: *\nDisallow: /")  # server trouble or "too many requests": back off
                elif resp.status_code < 400:
                    robots = Robots(resp.text)
                # 4xx: no robots.txt -> everything allowed (stays None)
            except httpx.HTTPError:
                robots = None
            self._robots[base] = robots
            if robots and robots.crawl_delay and parts.hostname:
                self._crawl_delay[parts.hostname] = robots.crawl_delay
        robots = self._robots[base]
        return robots is None or robots.allowed(url)

    def request(self, method: str, url: str, *, scrape: bool = False, **kwargs) -> httpx.Response:
        if not scrape:
            self._wait_turn(urlsplit(url).hostname or "")
            return self._http.request(method, url, **kwargs)
        if not self._robots_allow(url):
            raise RobotsDisallowed(url)
        self._wait_turn(urlsplit(url).hostname or "")
        resp = self._http.request(method, url, follow_redirects=False, **kwargs)
        # Redirects one hop at a time, so each address is checked against its own site's robots.txt
        # (an allowed page can redirect into a disallowed one).
        for _ in range(MAX_REDIRECTS):
            nxt = resp.next_request
            if nxt is None:
                return resp
            if not self._robots_allow(str(nxt.url)):
                raise RobotsDisallowed(str(nxt.url))
            self._wait_turn(nxt.url.host or "")
            resp = self._http.send(nxt, follow_redirects=False)
        raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects", request=resp.request)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)
