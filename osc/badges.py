"""GitHub achievement progress for the dashboard (user goal 2026-10-04: as many badges and tiers as possible).

Counts come from the GitHub API; which badges are already on the profile comes from the public achievements
page. Cached in data/badges.json for CACHE_S, refreshed in a background thread so the page never waits.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config

PROJECT = Path(__file__).resolve().parent.parent
CACHE_F = PROJECT / "data" / "badges.json"
CACHE_S = 1800
_lock = threading.Lock()

# name, how it is earned, tier thresholds (default, bronze, silver, gold); [] = single badge
BADGES = [
    ("Pull Shark", "merged pull requests", [2, 16, 128, 1024]),
    ("Pair Extraordinaire", "merged PRs with a co-author", [1, 10, 24, 48]),
    ("Galaxy Brain", "accepted answers in Discussions", [2, 8, 16, 32]),
    ("Starstruck", "stars on your most-starred repo", [16, 128, 512, 4096]),
    ("Quickdraw", "closed an issue or PR within 5 minutes of opening", []),
    ("YOLO", "merged a PR with a review still pending", []),
    ("Public Sponsor", "sponsored someone on GitHub Sponsors", []),
]
TIER_NAMES = ["", "bronze", "silver", "gold"]

# profile "highlight" badges (not tiered achievements): name, how, how we judge status, action link
HIGHLIGHTS = [
    ("Pro", "Use GitHub Pro (free for students via the Student Developer Pack)", "plan", "https://education.github.com/pack"),
    ("Developer Program Member", "Register for the GitHub Developer Program (free)", "manual", "https://github.com/developer/register"),
    ("Security advisory credit", "Be credited on an advisory in the GitHub Advisory Database", "advisory", "https://github.com/github/advisory-database"),
    ("GitHub Campus Expert", "Join GitHub's student leadership program", "manual", "https://education.github.com/experts"),
    ("Security Bug Bounty Hunter", "Report a valid vulnerability in GitHub itself (we are NOT pursuing this)", "declined", "https://bounty.github.com/"),
]
# achievements GitHub lists but nobody can currently earn
UNAVAILABLE = [
    ("Heart On Your Sleeve", "React to something with a heart emoji", "in testing, not released"),
    ("Open Sourcerer", "PRs merged in many public repos", "in testing, not released"),
    ("Arctic Code Vault", "Code in the 2020 Archive Program", "no longer earnable"),
    ("Mars 2020 Contributor", "Code in the Mars 2020 mission", "no longer earnable"),
]

PAIR_Q = """query($q: String!, $after: String) { search(query: $q, type: ISSUE, first: 50, after: $after) {
  pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { url commits(first: 60) { nodes { commit { message authors(first: 6) { totalCount } } } } } } } }"""


def _gh(*args: str) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120, env={**os.environ, "GH_TOKEN": config.github_token()})
    return r.returncode, r.stdout if r.returncode == 0 else r.stderr


def _count(q: str) -> int:
    rc, out = _gh("api", "-X", "GET", "search/issues", "-f", f"q={q}", "-q", ".total_count")
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else 0


def _pair(me: str) -> int:
    n, after = 0, None
    for _ in range(10):
        args = ["api", "graphql", "-f", f"query={PAIR_Q}", "-f", f"q=author:{me} is:pr is:merged"]
        if after:
            args += ["-f", f"after={after}"]
        rc, out = _gh(*args)
        if rc != 0:
            break
        s = json.loads(out)["data"]["search"]
        for pr in s["nodes"]:
            cs = (pr.get("commits") or {}).get("nodes") or []
            if any(c["commit"]["authors"]["totalCount"] > 1 or "co-authored-by:" in c["commit"]["message"].lower() for c in cs):
                n += 1
        if not s["pageInfo"]["hasNextPage"]:
            break
        after = s["pageInfo"]["endCursor"]
    return n


MERGED_Q = """query($q: String!) { search(query: $q, type: ISSUE, first: 100) { issueCount nodes { ... on PullRequest {
  url number title mergedAt additions deletions repository { nameWithOwner stargazerCount } mergedBy { login } } } } }"""


def merged(me: str) -> list[dict]:
    """Our merged PRs in other people's repos, newest first."""
    rc, out = _gh("api", "graphql", "-f", f"query={MERGED_Q}", "-f", f"q=author:{me} is:pr is:merged -user:{me} sort:updated-desc")
    if rc != 0:
        return []
    rows = [{"repo": n["repository"]["nameWithOwner"], "stars": n["repository"]["stargazerCount"], "number": n["number"], "title": n["title"],
             "url": n["url"], "merged_at": n["mergedAt"], "by": (n.get("mergedBy") or {}).get("login"), "lines": n["additions"] + n["deletions"]}
            for n in json.loads(out)["data"]["search"]["nodes"] if n]
    return sorted(rows, key=lambda r: r["merged_at"], reverse=True)


def _earned(me: str) -> set[str]:
    try:
        req = urllib.request.Request(f"https://github.com/{me}?tab=achievements", headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=30).read().decode(errors="ignore")
        return set(re.findall(r'alt="Achievement: ([^"]+)"', html))
    except Exception:
        return set()


def compute() -> dict:
    rc, me = _gh("api", "user", "-q", ".login")
    me = me.strip()
    rc, stars = _gh("api", "user/repos?affiliation=owner&per_page=100", "-q", "[.[].stargazers_count] | max // 0")
    try:
        accepted = json.loads((PROJECT / "data" / "discuss_state.json").read_text()).get("accepted") or []
    except Exception:
        accepted = []
    counts = {"Pull Shark": _count(f"author:{me} is:pr is:merged"), "Pair Extraordinaire": _pair(me),
              "Galaxy Brain": len(accepted), "Starstruck": int(stars.strip() or 0) if rc == 0 else 0}
    open_prs = _count(f"author:{me} is:pr is:open -user:{me}")
    earned = _earned(me)
    out = []
    for name, how, tiers in BADGES:
        n = counts.get(name)
        b = {"name": name, "how": how, "earned": name in earned, "count": n, "tiers": tiers}
        if tiers and n is not None:
            level = sum(1 for t in tiers if n >= t)
            b["tier"] = TIER_NAMES[level - 1] if level else None
            b["earned"] = b["earned"] or level > 0
            b["next"] = tiers[level] if level < len(tiers) else None
            b["next_name"] = (TIER_NAMES[level] or "badge") if level < len(tiers) else None
            prev = tiers[level - 1] if level else 0
            b["progress"] = 1.0 if b["next"] is None else round((n - prev) / max(1, b["next"] - prev), 3)
        out.append(b)
    notes = {
        "Pull Shark": f"{open_prs} PRs open upstream; every merge counts. The responder answers reviews to get them merged.",
        "Pair Extraordinaire": "Counts merged PRs whose commits carry a co-author (the Claude line, or a maintainer's commit).",
        "Galaxy Brain": "Answers post 3x a day in repos where askers mark answers; follow-ups and one 'mark as answer' note per thread.",
        "Starstruck": "Needs people to find SkillJail: a Show HN or Reddit post from you.",
        "Public Sponsor": "A $1 one-time sponsorship from you.",
    }
    for b in out:
        b["note"] = notes.get(b["name"], "")

    # highlight badges (not tiered achievements)
    rc, plan = _gh("api", "user", "-q", ".plan.name")
    is_pro = rc == 0 and plan.strip() not in ("free", "")
    try:
        adv = json.loads((PROJECT / "data" / "advisory_state.json").read_text()).get("drafts") or {}
    except Exception:
        adv = {}
    adv_prs = [d for d in adv.values() if d.get("pr_url")]
    adv_merged = _count(f"author:{me} is:pr is:merged repo:github/advisory-database")
    highlights = []
    for name, how, kind, link in HIGHLIGHTS:
        h = {"name": name, "how": how, "link": link, "earned": name in earned}
        if kind == "plan":
            h["earned"] = h["earned"] or is_pro
            h["status"] = "earned" if h["earned"] else "apply with your @ucsc.edu email, free for students"
        elif kind == "advisory":
            h["earned"] = h["earned"] or adv_merged > 0
            h["status"] = (f"{adv_merged} correction(s) merged" if adv_merged else
                           f"{len(adv_prs)} advisory PR(s) open, waiting on review" if adv_prs else "advisory PRs opening daily")
        elif kind == "declined":
            h["status"] = "not pursuing (requires attacking GitHub's own systems)"
        else:
            h["status"] = "earned" if h["earned"] else "needs you (one-time signup)"
        highlights.append(h)
    unavailable = [{"name": n, "how": hw, "status": s} for n, hw, s in UNAVAILABLE]
    return {"ts": time.time(), "login": me, "badges": out, "merged": merged(me), "open_upstream": open_prs,
            "highlights": highlights, "unavailable": unavailable}


def get(refresh: bool = False) -> dict:
    try:
        data = json.loads(CACHE_F.read_text())
    except Exception:
        data = None
    stale = not data or time.time() - data.get("ts", 0) > CACHE_S
    if refresh or not data:
        with _lock:
            data = compute()
            CACHE_F.write_text(json.dumps(data, indent=1))
    elif stale and _lock.acquire(blocking=False):
        def bg():
            try:
                CACHE_F.write_text(json.dumps(compute(), indent=1))
            finally:
                _lock.release()
        threading.Thread(target=bg, daemon=True).start()
    return data


def report() -> tuple[list[str], list[str]]:
    """Digest lines for every badge, plus what was newly earned since the last report (badge or tier)."""
    from . import db
    d = get(refresh=True)
    prev = db.kv_get("badges_last") or {}
    now, lines, news = {}, [], []
    for b in d["badges"]:
        key = b.get("tier") or ("earned" if b["earned"] else "")
        now[b["name"]] = key
        if key and prev.get(b["name"]) != key and b["name"] in prev:
            news.append(f"{b['name']}: {key if b.get('tier') else 'unlocked'}")
        if b["tiers"]:
            nxt = f"{b['count']}/{b['next']} for {b['next_name']}" if b.get("next") else "top tier"
            lines.append(f"  - {b['name']}: {'earned' if b['earned'] else 'not yet'}{' (' + b['tier'] + ')' if b.get('tier') else ''}, {nxt}")
        else:
            lines.append(f"  - {b['name']}: {'earned' if b['earned'] else 'not yet'}")
    db.kv_set("badges_last", now)
    return lines, news
