"""The dashboard's "Today" panel: what's planned, what's done, and what changed on GitHub.

A background thread in the server polls GitHub every POLL_S seconds (a few small GraphQL pages) for
activity on our PRs and keeps a rolling feed in data/updates.json, so updates show up without anyone asking.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config, db, log

STAGE = "today"
PROJECT = Path(__file__).resolve().parent.parent
UPDATES_F = PROJECT / "data" / "updates.json"
STATE_F = PROJECT / "data" / "daily_state.json"
LOCK = PROJECT / "data" / "daily.lock"
POLL_S = 300
DAILY_RUNS = ((2, 0), (8, 0), (14, 0), (20, 0))   # launchd com.stanleyshen.osc-daily StartCalendarInterval
BOTS = re.compile(r"(\[bot\]$|^codecov|^coveralls|^sonarcloud|^netlify|^vercel|^github-actions|^dependabot|^copilot|^claude$|^coderabbit|^gemini-code-assist|^cla\b|-cla$|cla-|bot$)", re.I)

_state = {"login": None, "polled_at": 0.0, "error": "", "opened_today": [], "open_prs": 0}
_lock = threading.Lock()

# Paged in small chunks: one big first:60 search with nested comments/reviews trips GitHub's
# "Resource limits for this query exceeded" once we carry dozens of open PRs, so we walk pages of 25.
QUERY = """query($q: String!, $after: String) { search(query: $q, type: ISSUE, first: 25, after: $after) {
  pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest {
    number title url state merged mergedAt closedAt createdAt reviewDecision repository { nameWithOwner }
    comments(last: 5) { nodes { author { login } createdAt url bodyText } }
    reviews(last: 5) { nodes { author { login } state submittedAt url bodyText } }
} } } }"""
MAX_PAGES = 8


def _gh(*args: str, timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GH_TOKEN": config.github_token()})
    return r.returncode, r.stdout if r.returncode == 0 else (r.stderr or r.stdout)


def _load() -> list[dict]:
    try:
        return json.loads(UPDATES_F.read_text())
    except Exception:
        return []


def poll() -> None:
    """One GitHub poll: new reviews/comments/merges on our PRs since we last looked."""
    if not _state["login"]:
        rc, me = _gh("api", "user", "-q", ".login")
        if rc != 0:
            raise RuntimeError(me[:200])
        _state["login"] = me.strip()
    me = _state["login"]
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 21 * 86400))
    q = f"author:{me} is:pr updated:>={since}"
    nodes, after = [], None
    for _ in range(MAX_PAGES):
        args = ["api", "graphql", "-f", f"query={QUERY}", "-f", f"q={q}"]
        if after:
            args += ["-f", f"after={after}"]
        rc, out = _gh(*args)
        if rc != 0:
            raise RuntimeError(out[:200])
        search = json.loads(out)["data"]["search"]
        nodes += [n for n in search["nodes"] if n]
        page = search["pageInfo"]
        if not page["hasNextPage"]:
            break
        after = page["endCursor"]
    feed = _load()
    seen = {u["id"] for u in feed}
    first_run = not feed
    new = []

    def add(uid, ts, kind, pr, who, text, url):
        if uid in seen:
            return
        seen.add(uid)
        new.append({"id": uid, "ts": ts, "kind": kind, "repo": pr["repository"]["nameWithOwner"], "number": pr["number"],
                    "title": pr["title"], "who": who, "text": (text or "").strip()[:400], "url": url or pr["url"]})

    def epoch(s):
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) if s else 0

    for pr in nodes:
        if pr.get("merged"):
            add(f"merged:{pr['url']}", epoch(pr["mergedAt"]), "merged", pr, "", "merged", pr["url"])
        elif pr.get("state") == "CLOSED":
            add(f"closed:{pr['url']}", epoch(pr["closedAt"]), "closed", pr, "", "closed without merge", pr["url"])
        add(f"opened:{pr['url']}", epoch(pr["createdAt"]), "opened", pr, me, "PR opened", pr["url"])
        for r in (pr.get("reviews") or {}).get("nodes") or []:
            who = (r.get("author") or {}).get("login") or "ghost"
            if who == me or BOTS.search(who):
                continue
            kind = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested"}.get(r["state"], "review")
            add(f"review:{r['url']}", epoch(r["submittedAt"]), kind, pr, who, r.get("bodyText"), r["url"])
        for c in (pr.get("comments") or {}).get("nodes") or []:
            who = (c.get("author") or {}).get("login") or "ghost"
            if who == me or BOTS.search(who):
                continue
            add(f"comment:{c['url']}", epoch(c["createdAt"]), "comment", pr, who, c.get("bodyText"), c["url"])
    # mark whether we replied after each comment/review (our own latest comment on that PR)
    ours = {}
    for pr in nodes:
        ts = [epoch(c["createdAt"]) for c in (pr.get("comments") or {}).get("nodes") or [] if (c.get("author") or {}).get("login") == me]
        ours[pr["url"]] = max(ts, default=0)
    feed = sorted(feed + new, key=lambda u: u["ts"], reverse=True)[:300]
    for u in feed:
        if u["kind"] in ("comment", "changes requested", "review"):
            pr_url = u["url"].split("#")[0]
            u["replied"] = ours.get(pr_url, 0) > u["ts"]
    UPDATES_F.write_text(json.dumps(feed, indent=1))
    day = time.strftime("%Y-%m-%d")
    with _lock:
        _state["opened_today"] = [n["url"] for n in nodes if time.strftime("%Y-%m-%d", time.localtime(epoch(n["createdAt"]))) == day
                                  and not n["repository"]["nameWithOwner"].startswith(me + "/")]
        _state["open_prs"] = sum(1 for n in nodes if n["state"] == "OPEN")
        _state["polled_at"] = time.time()
        _state["error"] = ""
    # keep the local DB in step with merges/closures
    for pr in nodes:
        if pr.get("merged") or pr.get("state") == "CLOSED":
            st = "merged" if pr.get("merged") else "closed"
            for c in db.rows("SELECT id FROM changes WHERE pr_url=? AND status='submitted'", (pr["url"],)):
                db.update("changes", "id", c["id"], {"status": st, "status_note": f"{st} on GitHub (seen by dashboard poller)", "updated_at": time.time()})
    if new and not first_run:
        log.info(STAGE, f"{len(new)} new GitHub update(s): " + "; ".join(f"{u['kind']} {u['repo']}#{u['number']}" for u in new[:5]))


def start_poller() -> None:
    def loop():
        while True:
            try:
                poll()
            except Exception as e:
                with _lock:
                    _state["error"] = str(e)[:200]
            time.sleep(POLL_S)
    threading.Thread(target=loop, daemon=True, name="gh-poller").start()


# ---------------------------------------------------------------- summary
def _next_at(h: int, m: int) -> float:
    now = time.time()
    lt = time.localtime(now)
    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    return t if t > now else t + 86400


def _running() -> dict:
    """What the pipeline is doing right now, from the lock and the tail of today's log."""
    pid = 0
    try:
        pid = int(LOCK.read_text().strip() or 0)
        os.kill(pid, 0)
    except Exception:
        return {"active": False}
    kind = "run"
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        kind = "quota hunt" if "osc.quota" in cmd else "scheduled run" if "osc.daily" in cmd else "run"
    except Exception:
        pass
    logf = PROJECT / "logs" / f"daily-{time.strftime('%Y-%m-%d')}.log"
    last, repo = "", ""
    if logf.exists():
        with open(logf, "rb") as f:
            f.seek(max(0, logf.stat().st_size - 60000))
            lines = f.read().decode(errors="ignore").splitlines()
        pat = re.compile(r"^(\d\d:\d\d:\d\d) (\w+)\s+\[(\w+)\](?:\[([^\]]+)\])? (.*)$")
        for ln in reversed(lines):
            m = pat.match(ln)
            if not m or "🔧" in ln or "claude start" in ln or "claude done" in ln:
                continue
            last = f"{m.group(1)} {m.group(3)}: {m.group(5).lstrip('💬 ')[:220]}"
            repo = m.group(4) or ""
            break
    return {"active": True, "kind": kind, "repo": repo, "last": last, "since": LOCK.stat().st_mtime}


def summary() -> dict:
    day = time.strftime("%Y-%m-%d")
    t0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
    try:
        st = json.loads(STATE_F.read_text())
    except Exception:
        st = {}
    spent = float(st.get("ledger", {}).get(day, 0))
    cap = float(config.setting("DAILY_BUDGET_USD")) + float(config.setting("DAILY_RESCUE_USD"))
    target = int(config.setting("DAILY_MIN_PRS"))      # the bar the quota hunt works toward; DAILY_TARGET_PRS is the cap
    with _lock:
        opened = list(_state["opened_today"])
        polled, err, open_prs = _state["polled_at"], _state["error"], _state["open_prs"]
    runs = [{"what": "scheduled run (scout + build + open)", "at": _next_at(h, m)} for h, m in DAILY_RUNS]
    now = time.time()
    if len(opened) < target:
        nxt = now - (now % 3600) + 5 * 60
        runs.append({"what": f"quota hunt (keeps going until {target} PRs today)", "at": nxt if nxt > now else nxt + 3600})
    runs.sort(key=lambda r: r["at"])
    last_scan = db.kv_get("last_scan") or {}
    plan = {
        "target": target, "max": int(config.setting("DAILY_TARGET_PRS")), "opened": len(opened), "budget_spent": round(spent, 2), "budget_cap": cap,
        "next": runs[:3], "running": _running(),
        "scan_due": (last_scan.get("ts", 0) + float(config.setting("DAILY_RESCAN_DAYS")) * 86400) if last_scan else None,
        "free_gb": round(os.statvfs(PROJECT).f_bavail * os.statvfs(PROJECT).f_frsize / 1e9, 1),
    }
    built = db.rows("SELECT id, repo, status, review_verdict, review_score, pr_title, pr_url, status_note, cost_usd, updated_at "
                    "FROM changes WHERE created_at>=? OR (updated_at>=? AND status IN ('submitted','prepared','ready')) ORDER BY updated_at DESC", (t0, t0))
    scouted = db.rows("SELECT repo, COUNT(*) n FROM opportunities WHERE created_at>=? GROUP BY repo", (t0,))
    from .opener import blocker, _open_repos
    orepos = _open_repos()
    needs_you = []
    for c in db.rows("SELECT * FROM changes WHERE status IN ('prepared','ready') ORDER BY updated_at DESC"):
        b = blocker(c, orepos)
        if b and ("personally" in (c.get("status_note") or "") or b.startswith("CLA") or "written by you" in b):
            needs_you.append({"repo": c["repo"], "id": c["id"], "why": b, "title": c["pr_title"]})
    try:
        rs = json.loads((PROJECT / "data" / "respond_state.json").read_text())
    except Exception:
        rs = {}
    still_open = {r["id"] for r in db.rows("SELECT id FROM changes WHERE status='submitted'")}
    for n in reversed(rs.get("needs_you") or []):
        if n.get("id") in still_open and now - n["ts"] < 21 * 86400:
            needs_you.insert(0, {"repo": f"{n['repo']}#{n['number']}", "id": n["id"], "why": n["why"], "title": n["title"], "url": n["url"]})
    followups = [h for h in reversed(rs.get("history") or []) if not h.get("draft") and h.get("action") not in ("none",)][:12]
    feed = _load()
    return {
        "day": day, "plan": plan,
        "done": {"opened": opened, "built": [dict(b, status_note=(b["status_note"] or "")[:200]) for b in built],
                 "scouted_repos": len(scouted), "opportunities": sum(s["n"] for s in scouted)},
        "updates": feed[:40], "unread_since": None,
        "needs_you": needs_you[:12], "open_prs": open_prs,
        "followups": followups, "followup_spent": round(float((rs.get("ledger") or {}).get(day, 0)), 2),
        "polled_at": polled, "poll_error": err, "poll_every_s": POLL_S,
    }
