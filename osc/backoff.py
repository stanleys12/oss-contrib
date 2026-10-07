"""Auto back-off from repos that signal they don't want our PRs (user 2026-10-07).

When a maintainer closes one of our PRs as unwanted/automated/spam, or tells us to stop, the repo is added
to a persistent no-touch list and never contributed to again. Built to avoid false positives: the signal
must come from a MAINTAINER (MEMBER/OWNER/COLLABORATOR, never a bot or an outside user), and either the PR
was closed-without-merge or the comment is an explicit stop, AND the text matches a tight phrase list. A
normal technical closure ("this approach is wrong", "not needed", "duplicate") does NOT trigger it. Every
back-off emails the user so a false positive can be reverted (just remove the repo from data/backoff.json).

  python -m osc.backoff --list            # show backed-off repos
  python -m osc.backoff --scan            # check open/recently-closed PRs now
  python -m osc.backoff --remove <repo>   # undo a back-off
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, db, log
from .daily import PROJECT, send_digest

STAGE = "backoff"
STATE_F = PROJECT / "data" / "backoff.json"
MAINTAINER = ("MEMBER", "OWNER", "COLLABORATOR")

# high-precision phrases that mean "we don't want these PRs" / "stop". Kept deliberately narrow.
_STOP = re.compile(
    r"\bunsolicited\b"
    r"|\bai[\s-]?slop\b"
    r"|\bplease stop\b.{0,40}\b(open|send|submit|creat|mak|pr|pull request|contribut)"
    r"|\bstop\b.{0,25}\b(opening|sending|submitting|creating|making|contributing)\b"
    r"|\b(do not|don'?t|please do not)\s+(open|send|submit|creat\w*|make)\b.{0,25}\b(any|more|further|another|additional|pr|pull request)\b"
    r"|\bno\s+(ai|bot|automated|machine[\s-]?generated)\s+(pr|prs|pull request|pull requests|contribution|contributions)\b"
    r"|\b(we|i)\s+(do not|don'?t|will not|won'?t|cannot|can'?t)\s+(accept|want|review|merge|take)\b.{0,30}\b(ai|automated|unsolicited|bot|machine[\s-]?generated|generated)\b"
    r"|\b(ai|bot|machine)[\s-]?generated\b.{0,30}\b(not accepted|not welcome|not wanted|aren'?t welcome|will be closed|rejected|spam)\b"
    r"|\bclos(e|ing|ed)\b.{0,20}\bas spam\b"
    r"|\b(this|it) is spam\b"
    r"|\bmark(ed|ing)?\b.{0,10}\bas spam\b",
    re.I,
)


def _gh(*args: str, timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GH_TOKEN": config.github_token(), "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def _state() -> dict:
    try:
        return json.loads(STATE_F.read_text())
    except Exception:
        return {}


def repos() -> set[str]:
    return set(_state().keys())


def is_backed_off(repo: str) -> bool:
    return repo in _state()


def match(text: str) -> str | None:
    if not text:
        return None
    m = _STOP.search(text)
    return m.group(0).strip()[:80] if m else None


def _record(repo: str, phrase: str, who: str, url: str) -> bool:
    st = _state()
    if repo in st:
        return False
    st[repo] = {"ts": time.time(), "phrase": phrase, "by": who, "url": url}
    STATE_F.write_text(json.dumps(st, indent=1))
    log.warn(STAGE, f"backing off {repo}: {who} signalled unwanted ('{phrase}') on {url}", repo=repo)
    send_digest(f"[oss-contrib] backing off {repo} (no more PRs)",
                f"A maintainer of {repo} signalled they don't want our automated PRs, so the system will not "
                f"contribute there again.\n\nSignal: \"{phrase}\" from {who}\n{url}\n\n"
                f"If this is a mistake, run: python -m osc.backoff --remove {repo}\n")
    return True


def check_pr(repo: str, pr: dict, me: str) -> bool:
    """Decide from one PR's close state + maintainer comments whether to back off. `pr` is a GraphQL node
    with state, merged, comments{nodes{author,authorAssociation,body}}, reviews{...}, and timelineItems
    for the close actor. Returns True if a back-off was recorded."""
    if pr.get("merged") or is_backed_off(repo):
        return False
    closed = pr.get("state") == "CLOSED"
    # (a) a maintainer comment/review with a stop phrase — triggers whether or not the PR is closed
    for src in ((pr.get("comments") or {}).get("nodes") or [], (pr.get("reviews") or {}).get("nodes") or []):
        for c in src:
            who = (c.get("author") or {}).get("login") or ""
            if who == me or who.endswith("[bot]") or c.get("authorAssociation") not in MAINTAINER:
                continue
            ph = match(c.get("body") or "")
            if ph:
                return _record(repo, ph, who, c.get("url") or pr.get("url", ""))
    # (b) the PR was closed-not-merged BY a maintainer and the close comment matches (covers "closed as spam"
    #     where the phrase is on the closing event, not a separate comment)
    if closed:
        for ev in ((pr.get("timelineItems") or {}).get("nodes") or []):
            actor = (ev.get("actor") or {}).get("login") or ""
            if actor == me or actor.endswith("[bot]"):
                continue
            # only trust the close actor if they are a maintainer (checked via a cheap membership call below)
            ph = match(pr.get("title") or "")
            if ph and _is_maintainer(repo, actor):
                return _record(repo, ph, actor, pr.get("url", ""))
    return False


_maint_cache: dict[str, bool] = {}


def _is_maintainer(repo: str, login: str) -> bool:
    key = f"{repo}:{login}"
    if key in _maint_cache:
        return _maint_cache[key]
    rc, out = _gh("api", f"repos/{repo}/collaborators/{login}/permission", "-q", ".permission")
    ok = rc == 0 and out.strip() in ("admin", "write", "maintain")
    _maint_cache[key] = ok
    return ok


QUERY = """query($owner: String!, $name: String!, $num: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $num) {
  url state merged
  comments(last: 30) { nodes { author { login } authorAssociation body url } }
  reviews(last: 20) { nodes { author { login } authorAssociation body url } }
} } }"""


def scan() -> dict:
    """Check our submitted/closed PRs for a stop signal. Cheap: one GraphQL call per PR."""
    rc, me = _gh("api", "user", "-q", ".login")
    me = me.strip()
    hit = []
    rows = db.rows("SELECT repo, pr_url FROM changes WHERE status IN ('submitted','closed') AND pr_url LIKE 'https://github.com/%/pull/%' "
                   "AND updated_at > ?", (time.time() - 30 * 86400,))
    for r in rows:
        repo = r["repo"]
        if is_backed_off(repo):
            continue
        owner, name = repo.split("/")
        num = int(r["pr_url"].rsplit("/", 1)[1])
        rc, out = _gh("api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"num={num}")
        if rc != 0:
            continue
        try:
            pr = json.loads(out)["data"]["repository"]["pullRequest"]
        except Exception:
            continue
        if pr and check_pr(repo, pr, me):
            hit.append(repo)
    return {"backed_off": hit, "total": list(repos())}


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--remove" in a:
        st = _state(); repo = a[a.index("--remove") + 1]
        st.pop(repo, None); STATE_F.write_text(json.dumps(st, indent=1))
        print(f"removed {repo} from the back-off list")
    elif "--list" in a:
        for repo, d in _state().items():
            print(f"  {repo}  ('{d['phrase']}' by {d['by']}, {time.strftime('%Y-%m-%d', time.localtime(d['ts']))})")
    else:
        print(json.dumps(scan(), indent=1))
