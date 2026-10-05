"""Free, deterministic checks that run before any paid agent.

* :func:`shortlist_issues` asks the GitHub API for issues a maintainer has already confirmed and
  nobody has claimed. If a repo has none, the scout agent is not launched at all.
* :func:`claim_check` re-checks an opportunity's issues right before the builder runs, so money
  is not spent on a fix somebody else already owns.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

from . import config, log

STAGE = "prefilter"
CLAIM_RE = re.compile(r"((would|'d) (like|love) to (take|work on|pick up|tackle) (it|this)|can i (take|work on|pick up) (it|this)|"
                      r"i('ll| will| can) (take|pick up|work on) (it|this)|i('ll| will| can) pick (it|this) up|(may|could) i (take|work on) (it|this)|assign (it |this )?to me|"
                      r"happy to (open|send|submit)|(can|will|could) (open|send|submit) (a |the )?(pr|prs|patch|fix)|working on (this|it)|i'?ll (open|send|submit|put up) a (pr|patch|fix)|i can open (a|the) pr|opened? (a )?pr|"
                      r"patch(es)? (is|are) attached|attached (a )?(diff|patch)|ha(ve|s) a (fix|patch)|github\.com/[\w.-]+/[\w.-]+/(tree|commit|compare)/|here'?s (a|the) (fix|patch)|"
                      r"submitted (a )?(pr|cl|patch)|go\.dev/cl/|please assign|/assign\b)", re.I)
CONFIRM_LABELS = ("bug", "confirmed", "help wanted", "needsfix", "triaged", "triage/accepted", "accepted",
                  "kind/bug", "type-bug", "type: bug", "actionable", "good second issue", "priority")
SKIP_LABELS = ("good first issue", "wontfix", "invalid", "duplicate", "needs-info", "waitingforinfo", "question", "proposal")
MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}


class GitHubUnavailable(RuntimeError):
    """The API refused or timed out; the caller must not treat this as 'no results'."""


_PACE_LOCK = threading.Lock()
_last_call = [0.0]
PACE_S = 0.35   # minimum gap between GitHub calls across ALL scout threads (secondary/burst limit)


def _pace() -> None:
    with _PACE_LOCK:
        wait = _last_call[0] + PACE_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _gh(*args: str, timeout: int = 90, _retry: bool = True) -> str:
    env = {**os.environ, "GH_TOKEN": config.github_token()}
    _pace()
    r = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, timeout=timeout)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()[:200]
        if _retry and re.search(r"secondary rate limit|abuse", err, re.I):
            log.warn(STAGE, "GitHub secondary rate limit hit; pausing 90s before one retry")
            time.sleep(90)
            return _gh(*args, timeout=timeout, _retry=False)
        if re.search(r"rate limit|abuse|403|502|503|timed out|timeout", err, re.I):
            raise GitHubUnavailable(err)
        log.warn(STAGE, f"gh {' '.join(args[:3])} failed: {err}")
        return ""
    return r.stdout


def _linked_prs(repo: str, number: int) -> list[int]:
    q = ('{repository(owner:"%s",name:"%s"){issue(number:%d){timelineItems(itemTypes:[CROSS_REFERENCED_EVENT,CONNECTED_EVENT],last:30)'
         '{nodes{__typename ... on CrossReferencedEvent{source{... on PullRequest{number state}}} ... on ConnectedEvent{subject{... on PullRequest{number state}}}}}}}}'
         % (repo.split("/")[0], repo.split("/")[1], number))
    out = _gh("api", "graphql", "-f", f"query={q}")
    try:
        nodes = json.loads(out)["data"]["repository"]["issue"]["timelineItems"]["nodes"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    prs = []
    for n in nodes:
        src = n.get("source") or n.get("subject") or {}
        if src.get("number") and src.get("state") in ("OPEN", "MERGED"):
            prs.append(src["number"])
    return prs


def issue_claims(repo: str, number: int) -> dict:
    """Return why an issue is unavailable, or {} if nobody has claimed it."""
    out = _gh("api", f"repos/{repo}/issues/{number}", "--jq", "{assignees:[.assignees[].login], state:.state, pr:(.pull_request!=null), body:(.body // \"\"), author:.user.login}")
    try:
        meta = json.loads(out)
    except json.JSONDecodeError:
        return {"reason": "issue not readable"}
    if meta.get("state") != "open" or meta.get("pr"):
        return {"reason": "issue closed or is a PR"}
    if meta.get("assignees"):
        return {"reason": f"assigned to {', '.join(meta['assignees'])}"}
    body = (meta.get("body") or "").replace("\n", " ")
    m = CLAIM_RE.search(body)
    if m:
        return {"reason": f"reporter {meta.get('author')} reserved the fix in the issue body: ...{body[max(0, m.start()-60):m.end()+60]}"}
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60 * 86400))
    out = _gh("api", f"repos/{repo}/issues/{number}/comments?since={cutoff}&per_page=100",
              "--jq", '.[] | "\\(.user.login)\\t\\(.author_association)\\t\\(.body|gsub("\\n";" "))"')
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and CLAIM_RE.search(parts[2]):
            return {"reason": f"claimed in a comment by {parts[0]}: {parts[2][:120]}"}
    prs = _linked_prs(repo, number)
    if prs:
        return {"reason": f"linked PR(s) {prs}"}
    return {}


def shortlist_issues(repo: str, limit: int = 8, min_age_days: int = 7, max_age_days: int = 120) -> list[dict]:
    """Open, unassigned issues 7-120 days old with a confirming label or maintainer comment and no claim."""
    now = time.time()
    lo = time.strftime("%Y-%m-%d", time.gmtime(now - max_age_days * 86400))
    hi = time.strftime("%Y-%m-%d", time.gmtime(now - min_age_days * 86400))
    q = f"repo:{repo} is:issue is:open no:assignee created:{lo}..{hi}"
    out = _gh("api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", "sort=comments", "-f", "order=desc", "-f", "per_page=60",
              "--jq", '.items[] | {n:.number, t:.title, c:.created_at, labels:[.labels[].name], assignees:(.assignees|length), comments:.comments, assoc:.author_association}')
    cands = []
    for line in out.splitlines():
        try:
            it = json.loads(line)
        except json.JSONDecodeError:
            continue
        age = (now - time.mktime(time.strptime(it["c"], "%Y-%m-%dT%H:%M:%SZ"))) / 86400
        labels = [l.lower() for l in it["labels"]]
        if it["assignees"] or age < min_age_days or age > max_age_days:
            continue
        if any(s in l for l in labels for s in SKIP_LABELS):
            continue
        confirmed = any(any(cl in l for cl in CONFIRM_LABELS) for l in labels)
        cands.append((confirmed, it["comments"], it))
    # confirmed-by-label first, then issues with a maintainer comment
    cands.sort(key=lambda x: (not x[0], -x[1]))
    picked = []
    for confirmed, _, it in cands[: limit * 3]:
        if not confirmed:
            raw = _gh("api", f"repos/{repo}/issues/{it['n']}/comments?per_page=50", "--jq", "[.[].author_association] | unique")
            try:
                assoc = set(json.loads(raw or "[]"))
            except json.JSONDecodeError:
                assoc = set()
            if not (assoc & MAINTAINER):
                continue
        claim = issue_claims(repo, it["n"])
        if claim:
            continue
        picked.append({"number": it["n"], "title": it["t"], "labels": it["labels"], "age_days": int((now - time.mktime(time.strptime(it["c"], "%Y-%m-%dT%H:%M:%SZ"))) / 86400),
                       "url": f"https://github.com/{repo}/issues/{it['n']}"})
        if len(picked) >= limit:
            break
    log.info(STAGE, f"{repo}: {len(picked)} unclaimed confirmed issues (from {len(cands)} candidates)", repo=repo)
    return picked


def claim_check(repo: str, issue_numbers: list[int]) -> str:
    """'' if every referenced issue is still free, else the reason to skip the build."""
    for n in issue_numbers or []:
        c = issue_claims(repo, int(n))
        if c:
            return f"#{n}: {c['reason']}"
    return ""


def shortlist_text(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["Start from these issues, which were pre-checked today as maintainer-confirmed and unclaimed (no assignee, no claim comment, no linked PR):"]
    for it in items:
        lines.append(f"- #{it['number']} ({it['age_days']}d, labels: {', '.join(it['labels'][:4]) or 'none'}): {it['title']}  {it['url']}")
    lines.append("Verify each in the code before proposing it. You may add other candidates only if they meet the same bar; if none of these is viable, say so briefly and stop instead of exploring the tree at length.")
    return "\n".join(lines)
