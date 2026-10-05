"""GitHub REST + GraphQL client with rate-limit awareness and a small on-disk cache."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

from .config import DATA_DIR, github_token
from . import log

CACHE_DIR = DATA_DIR / "ghcache"
CACHE_DIR.mkdir(exist_ok=True)
API = "https://api.github.com"


class GitHub:
    def __init__(self, token: str | None = None, cache_ttl: int = 6 * 3600):
        self.token = token or github_token()
        if not self.token:
            raise RuntimeError("No GitHub token (set OSC_GITHUB_TOKEN or store one in git credential helper)")
        self.cache_ttl = cache_ttl
        self.client = httpx.Client(
            base_url=API, timeout=60,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "osc-contrib-engine"},
        )
        self.calls = 0

    # ---------------- cache ----------------
    def _cache_key(self, kind: str, payload) -> Path:
        h = hashlib.sha256(json.dumps([kind, payload], sort_keys=True).encode()).hexdigest()[:32]
        return CACHE_DIR / f"{h}.json"

    def _cache_get(self, p: Path):
        try:
            if p.exists() and time.time() - p.stat().st_mtime < self.cache_ttl:
                return json.loads(p.read_text())
        except Exception:
            pass
        return None

    def _cache_put(self, p: Path, data):
        try:
            p.write_text(json.dumps(data))
        except Exception:
            pass

    # ---------------- rate limiting ----------------
    def _handle_rate(self, r: httpx.Response):
        rem = r.headers.get("x-ratelimit-remaining")
        reset = r.headers.get("x-ratelimit-reset")
        if r.status_code in (403, 429) and rem == "0" and reset:
            wait = max(1, int(reset) - int(time.time())) + 2
            log.warn("github", f"rate limited; sleeping {wait}s")
            time.sleep(min(wait, 900))
            return True
        if r.status_code in (403, 429) and "secondary" in r.text.lower():
            log.warn("github", "secondary rate limit; sleeping 60s")
            time.sleep(60)
            return True
        return False

    # ---------------- REST ----------------
    def get(self, path: str, params: dict | None = None, cache: bool = True):
        key = self._cache_key("GET", [path, params])
        self.last_was_cache = False
        if cache and (c := self._cache_get(key)) is not None:
            self.last_was_cache = True
            return c
        for attempt in range(4):
            r = self.client.get(path, params=params)
            self.calls += 1
            if self._handle_rate(r):
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if cache:
                self._cache_put(key, data)
            return data
        raise RuntimeError(f"GitHub GET {path} failed after retries")

    def search_repos(self, q: str, per_page: int = 30, sort: str = "stars", pages: int = 1) -> list[dict]:
        items = []
        for page in range(1, pages + 1):
            data = self.get("/search/repositories", {"q": q, "sort": sort, "order": "desc",
                                                     "per_page": per_page, "page": page})
            if not data:
                break
            items.extend(data.get("items", []))
            if len(data.get("items", [])) < per_page:
                break
            if not self.last_was_cache:
                time.sleep(2.2)  # search API: 30 req/min
        return items

    def search_issues(self, q: str, per_page: int = 50, pages: int = 1) -> list[dict]:
        items = []
        for page in range(1, pages + 1):
            data = self.get("/search/issues", {"q": q, "per_page": per_page, "page": page,
                                               "sort": "updated", "order": "desc"})
            if not data:
                break
            items.extend(data.get("items", []))
            if len(data.get("items", [])) < per_page:
                break
            time.sleep(2.2)
        return items

    # ---------------- GraphQL ----------------
    def graphql(self, query: str, variables: dict | None = None, cache: bool = True):
        key = self._cache_key("GQL", [query, variables])
        if cache and (c := self._cache_get(key)) is not None:
            return c
        for attempt in range(4):
            r = self.client.post("/graphql", json={"query": query, "variables": variables or {}})
            self.calls += 1
            if self._handle_rate(r):
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            body = r.json()
            if "errors" in body and not body.get("data"):
                raise RuntimeError(f"GraphQL error: {body['errors'][:2]}")
            data = body.get("data")
            if cache:
                self._cache_put(key, data)
            return data
        raise RuntimeError("GraphQL failed after retries")

    def rate_limit(self) -> dict:
        return self.get("/rate_limit", cache=False)

    def me(self) -> str:
        return self.get("/user", cache=False)["login"]


REPO_PROFILE_QUERY = """
query($owner:String!, $name:String!, $since:GitTimestamp!) {
  repository(owner:$owner, name:$name) {
    nameWithOwner description url stargazerCount forkCount isArchived isFork isMirror
    hasIssuesEnabled diskUsage pushedAt createdAt
    primaryLanguage { name }
    languages(first:6, orderBy:{field:SIZE, direction:DESC}) { edges { size node { name } } }
    licenseInfo { spdxId }
    repositoryTopics(first:20) { nodes { topic { name } } }
    defaultBranchRef { name target { ... on Commit { history(since:$since) { totalCount } } } }
    openIssues: issues(states:OPEN) { totalCount }
    gfi1: issues(states:OPEN, labels:["good first issue"]) { totalCount }
    gfi2: issues(states:OPEN, labels:["good-first-issue"]) { totalCount }
    hw1: issues(states:OPEN, labels:["help wanted"]) { totalCount }
    hw2: issues(states:OPEN, labels:["help-wanted"]) { totalCount }
    bugs: issues(states:OPEN, labels:["bug"]) { totalCount }
    openPRs: pullRequests(states:OPEN) { totalCount }
    recentPRs: pullRequests(last:60, states:[MERGED, CLOSED]) {
      nodes { state createdAt closedAt mergedAt authorAssociation author { login } }
    }
    c1: object(expression:"HEAD:CONTRIBUTING.md") { ... on Blob { byteSize text } }
    c2: object(expression:"HEAD:.github/CONTRIBUTING.md") { ... on Blob { byteSize text } }
    c3: object(expression:"HEAD:docs/CONTRIBUTING.md") { ... on Blob { byteSize text } }
    c4: object(expression:"HEAD:CONTRIBUTING.rst") { ... on Blob { byteSize text } }
    c5: object(expression:"HEAD:CONTRIBUTING") { ... on Blob { byteSize text } }
    prTemplate: object(expression:"HEAD:.github/PULL_REQUEST_TEMPLATE.md") { ... on Blob { text } }
    prTemplate2: object(expression:"HEAD:.github/pull_request_template.md") { ... on Blob { text } }
  }
}
"""

ISSUES_QUERY = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    issues(first:50, after:$after, states:OPEN, orderBy:{field:UPDATED_AT, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt updatedAt authorAssociation
        body
        labels(first:10) { nodes { name } }
        comments { totalCount }
        reactions { totalCount }
        assignees { totalCount }
        timelineItems(itemTypes:[CROSS_REFERENCED_EVENT, CONNECTED_EVENT], first:20) {
          nodes {
            ... on CrossReferencedEvent { source { ... on PullRequest { number state } } }
            ... on ConnectedEvent { subject { ... on PullRequest { number state } } }
          }
        }
      }
    }
  }
}
"""

OPEN_PRS_QUERY = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(first:100, after:$after, states:OPEN, orderBy:{field:UPDATED_AT, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url createdAt updatedAt isDraft author { login } authorAssociation
              labels(first:6) { nodes { name } } }
    }
  }
}
"""


def repo_profile(gh: GitHub, full_name: str, since_iso: str) -> dict | None:
    owner, name = full_name.split("/", 1)
    try:
        data = gh.graphql(REPO_PROFILE_QUERY, {"owner": owner, "name": name, "since": since_iso})
    except RuntimeError as e:
        log.warn("github", f"profile failed for {full_name}: {e}")
        return None
    return (data or {}).get("repository")


def repo_issues(gh: GitHub, full_name: str, max_issues: int = 150) -> list[dict]:
    owner, name = full_name.split("/", 1)
    out, after = [], None
    while len(out) < max_issues:
        data = gh.graphql(ISSUES_QUERY, {"owner": owner, "name": name, "after": after}, cache=False)
        conn = data["repository"]["issues"]
        out.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    return out[:max_issues]


def repo_open_prs(gh: GitHub, full_name: str, max_prs: int = 300) -> list[dict]:
    owner, name = full_name.split("/", 1)
    out, after = [], None
    while len(out) < max_prs:
        data = gh.graphql(OPEN_PRS_QUERY, {"owner": owner, "name": name, "after": after}, cache=False)
        conn = data["repository"]["pullRequests"]
        out.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    return out[:max_prs]
