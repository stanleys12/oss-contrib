"""Discussions answers, toward the Galaxy Brain achievement (user goal 2026-10-04).

Finds unanswered Q&A discussions in repos we already contribute to (open or merged PRs), picks the ones a
careful reader of the code can answer, and has an agent research and draft the answer in the clone. A
second pass checks every claim. Drafts land in data/discuss_state.json and on the dashboard.

Posting is off unless DISCUSS_AUTO_POST is true. Only an asker or a maintainer can mark an answer as
accepted, so the useful lever is answering questions whose asker is still around, in repos whose
maintainers mark answers.

  python -m osc.discuss --dry     # list candidates, spend nothing
  python -m osc.discuss           # draft (and post, if DISCUSS_AUTO_POST) up to DISCUSS_PER_RUN answers
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback

from . import config, db, log
from .analyzer import ensure_clone
from .claude_runner import extract_json, run_claude
from .daily import PROJECT
from .opener import _strip_dashes
from .responder import _clip, _epoch, _gh

STAGE = "discuss"
STATE_F = PROJECT / "data" / "discuss_state.json"
LOCK = PROJECT / "data" / "discuss.lock"

SEARCH = """query($q: String!) { search(query: $q, type: DISCUSSION, first: 50) { nodes { ... on Discussion {
  id number title url body createdAt updatedAt isAnswered locked author { login } authorAssociation
  category { name isAnswerable } repository { nameWithOwner defaultBranchRef { target { oid } } }
  comments(first: 20) { totalCount nodes { author { login } authorAssociation createdAt body isAnswer } }
} } } }"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "boolean"},
        "reason": {"type": "string", "description": "Why you answered or skipped."},
        "body": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer", "reason"],
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}}},
    "required": ["ok", "problems"],
}

CHECK_PROMPT = """You are checking a drafted answer to a GitHub Discussions question in `__REPO__` before it is posted under
someone's name. The repo is checked out here at the commit the answer links to. Be strict: a wrong answer in public is worse
than none.

Question: __TITLE__
__BODY__

Drafted answer:
__ANSWER__

Claims the author says they checked:
__CLAIMS__

Check that the answer actually answers the question, that every factual statement and every linked file and line is right
(open them), that nothing is invented, and that it reads like a plain, short human reply (no em or en dashes, no headings,
no bold, no filler phrases, no invented personal experience). Answer {"ok": true/false, "problems": [...]}."""


def _state() -> dict:
    try:
        st = json.loads(STATE_F.read_text())
    except Exception:
        st = {}
    st.setdefault("seen", {})
    st.setdefault("drafts", [])
    st.setdefault("ledger", {})
    return st


def _save(st: dict) -> None:
    st["drafts"] = st["drafts"][-200:]
    tmp = STATE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE_F)


def our_repos() -> list[str]:
    return [r["repo"] for r in db.rows("SELECT DISTINCT repo FROM changes WHERE status IN ('submitted','merged')")]


def pool() -> list[str]:
    """Repos whose Q&A we answer in: ones we contribute to, plus the best-ranked repos in our domains."""
    from .daily import DENY
    mine = our_repos()
    extra = [r["full_name"] for r in db.rows("SELECT full_name, raw FROM repos WHERE archived IS NOT 1 AND stars >= 1500 ORDER BY score DESC LIMIT ?",
                                             (int(config.setting("DISCUSS_EXTRA_REPOS")) * 2,))
             if r["full_name"] not in DENY and '"ai_prohibited": true' not in (r["raw"] or "")]
    from .backoff import repos as backed_off
    bo = backed_off()
    return [r for r in dict.fromkeys(mine + extra[:int(config.setting("DISCUSS_EXTRA_REPOS"))]) if r not in bo]


def accept_rate(repo: str, st: dict) -> float:
    """Share of answerable discussions that end with an accepted answer (cached a week). Askers in repos with
    a habit of marking answers are the ones who will mark ours."""
    c = st.setdefault("accept_rate", {}).get(repo)
    if c and time.time() - c["ts"] < 7 * 86400:
        return c["rate"]
    n = {}
    for k in ("answered", "unanswered"):
        rc, raw = _gh("api", "graphql", "-f", "query=query($q: String!) { search(query: $q, type: DISCUSSION, first: 1) { discussionCount } }",
                      "-f", f"q=repo:{repo} is:{k}")
        n[k] = json.loads(raw)["data"]["search"]["discussionCount"] if rc == 0 else 0
    tot = n["answered"] + n["unanswered"]
    rate = round(n["answered"] / tot, 3) if tot >= 10 else 0.0
    st["accept_rate"][repo] = {"ts": time.time(), "rate": rate, "n": tot}
    return rate


def candidates(me: str, st: dict) -> list[dict]:
    now = time.time()
    max_age = float(config.setting("DISCUSS_MAX_AGE_DAYS")) * 86400
    out = []
    repos = pool()
    mine = set(our_repos())
    for i in range(0, len(repos), 6):          # the search query has a length limit
        q = " ".join(f"repo:{r}" for r in repos[i:i + 6]) + " is:unanswered is:open"
        rc, raw = _gh("api", "graphql", "-f", f"query={SEARCH}", "-f", f"q={q}")
        if rc != 0:
            log.warn(STAGE, f"discussion search failed: {raw[:160]}")
            continue
        for d in (json.loads(raw).get("data") or {}).get("search", {}).get("nodes") or []:
            if not d or d.get("locked") or d.get("isAnswered") or not (d.get("category") or {}).get("isAnswerable"):
                continue
            if d["url"] in st["seen"] or (d.get("author") or {}).get("login") == me:
                continue
            age = now - _epoch(d["createdAt"])
            if age > max_age or age < 3600:
                continue
            cs = d["comments"]["nodes"]
            if any((c.get("author") or {}).get("login") == me for c in cs):
                continue
            if any(c.get("authorAssociation") in ("MEMBER", "OWNER", "COLLABORATOR") for c in cs):
                continue                        # a maintainer is already on it
            if d["comments"]["totalCount"] > 4:
                continue
            asker = (d.get("author") or {}).get("login")
            if cs and (cs[-1].get("author") or {}).get("login") != asker:
                continue                        # someone replied and the asker has not come back: probably answered
            repo = d["repository"]["nameWithOwner"]
            rate = accept_rate(repo, st)
            if rate < float(config.setting("DISCUSS_MIN_ACCEPT_RATE")) and repo not in mine:
                continue                        # askers there rarely mark answers
            asker_active = now - _epoch(d["updatedAt"]) < 3 * 86400
            d["_score"] = (4 * rate + (2 if asker_active else 0) + (1.5 if age < 4 * 86400 else 0)
                           + (1 if d["comments"]["totalCount"] == 0 else 0) + (0.5 if repo in mine else 0) - age / max_age)
            out.append(d)
    return sorted(out, key=lambda d: -d["_score"])


def _comments_text(d: dict) -> str:
    cs = d["comments"]["nodes"]
    if not cs:
        return "(none)"
    return "\n\n".join(f"{(c.get('author') or {}).get('login')} ({c.get('authorAssociation')}): {_clip(c.get('body'), 2500)}" for c in cs)


def draft(d: dict) -> tuple[dict, float]:
    repo = d["repository"]["nameWithOwner"]
    path = ensure_clone(repo)
    sha = (d["repository"].get("defaultBranchRef") or {}).get("target", {}).get("oid") or "HEAD"
    p = (config.PROMPTS_DIR / "discuss.md").read_text()
    for k, v in {"__REPO__": repo, "__TITLE__": d["title"], "__URL__": d["url"], "__WHEN__": d["createdAt"][:10],
                 "__AUTHOR__": (d.get("author") or {}).get("login") or "someone", "__BODY__": _clip(d.get("body"), 8000),
                 "__COMMENTS__": _comments_text(d), "__SHA__": sha}.items():
        p = p.replace(k, v)
    res = run_claude(p, path, model=config.setting("DISCUSS_MODEL"), max_turns=80, stage=STAGE, repo=repo, json_schema=ANSWER_SCHEMA,
                     max_budget_usd=float(config.setting("DISCUSS_BUDGET_USD")), timeout_s=2400,
                     allowed_tools=["Read", "Grep", "Glob", "LS", "Bash(git log:*)", "Bash(git show:*)", "Bash(git grep:*)", "Bash(git blame:*)",
                                    "Bash(gh issue view:*)", "Bash(gh pr view:*)", "Bash(gh search:*)", "Bash(python:*)", "Bash(python3:*)",
                                    "Bash(uv:*)", "Bash(.venv/bin/*)", "Bash(cat:*)", "Bash(ls:*)", "Bash(grep:*)", "Bash(rg:*)"])
    a = res.structured if isinstance(res.structured, dict) else extract_json(res.text)
    if not isinstance(a, dict):
        a = {"answer": False, "reason": f"no answer from agent: {res.error[:200]}"}
    cost = res.cost_usd
    if a.get("answer") and a.get("body"):
        cp = CHECK_PROMPT
        for k, v in {"__REPO__": repo, "__TITLE__": d["title"], "__BODY__": _clip(d.get("body"), 6000), "__ANSWER__": a["body"],
                     "__CLAIMS__": "\n".join(f"- {c}" for c in a.get("claims") or []) or "(none)"}.items():
            cp = cp.replace(k, v)
        chk = run_claude(cp, path, model=config.setting("REVIEWER_MODEL"), max_turns=40, stage="discuss-check", repo=repo, json_schema=CHECK_SCHEMA,
                         max_budget_usd=1.5, timeout_s=1200,
                         allowed_tools=["Read", "Grep", "Glob", "LS", "Bash(git log:*)", "Bash(git show:*)", "Bash(git grep:*)", "Bash(cat:*)", "Bash(grep:*)"])
        cost += chk.cost_usd
        v = chk.structured if isinstance(chk.structured, dict) else extract_json(chk.text) or {"ok": False, "problems": ["checker gave no verdict"]}
        a["check"] = v
        if not v.get("ok"):
            a["answer"] = False
            a["reason"] = "checker: " + "; ".join(v.get("problems") or [])[:400]
        a["body"] = _strip_dashes(a["body"]).strip()
    return a, cost


def post(d: dict, body: str, reply_to: str | None = None, disclose: bool = True) -> str:
    note = config.setting("DISCUSS_DISCLOSURE")
    if disclose and note and note not in body:
        body = body.rstrip() + "\n\n" + note
    if reply_to:
        q = 'mutation($id: ID!, $body: String!, $r: ID!) { addDiscussionComment(input: {discussionId: $id, body: $body, replyToId: $r}) { comment { url } } }'
        rc, out = _gh("api", "graphql", "-f", f"query={q}", "-f", f"id={d['id']}", "-f", f"body={body}", "-f", f"r={reply_to}")
        if rc != 0:
            raise RuntimeError(f"reply failed: {out[:200]}")
        return json.loads(out)["data"]["addDiscussionComment"]["comment"]["url"]
    q = 'mutation($id: ID!, $body: String!) { addDiscussionComment(input: {discussionId: $id, body: $body}) { comment { url } } }'
    rc, out = _gh("api", "graphql", "-f", f"query={q}", "-f", f"id={d['id']}", "-f", f"body={body}")
    if rc != 0:
        raise RuntimeError(f"post failed: {out[:200]}")
    return json.loads(out)["data"]["addDiscussionComment"]["comment"]["url"]


def accepted(me: str) -> list[str]:
    """Discussions where one of our comments was marked as the answer (Galaxy Brain counts these)."""
    rc, raw = _gh("api", "graphql", "-f", "query=query($q: String!) { search(query: $q, type: DISCUSSION, first: 50) { nodes { ... on Discussion { url answer { author { login } } } } } }",
                  "-f", f"q=commenter:{me} is:answered")
    if rc != 0:
        return []
    return [n["url"] for n in json.loads(raw)["data"]["search"]["nodes"] if n and ((n.get("answer") or {}).get("author") or {}).get("login") == me]


THREAD = """query($o: String!, $n: String!, $k: Int!) { repository(owner: $o, name: $n) { defaultBranchRef { target { oid } } discussion(number: $k) {
  id number title url body createdAt updatedAt isAnswered locked author { login } answer { author { login } }
  repository { nameWithOwner defaultBranchRef { target { oid } } } category { name isAnswerable }
  comments(first: 40) { totalCount nodes { id url author { login } authorAssociation createdAt body isAnswer
    replies(first: 30) { nodes { id author { login } createdAt body } } } }
} } }"""

THANKS = re.compile(r"\b(thanks?|thank you|thx|works?( now)?|worked|that did it|solved|fixed it|perfect|got it working|that helped)\b", re.I)


def followups(me: str, st: dict) -> list[dict]:
    """Our posted answers in the last 21 days: answer the asker's follow-up, or, when they said it worked but did
    not mark it, add one short note (once per discussion)."""
    done = []
    for rec in st["drafts"]:
        url = rec.get("posted") or ""
        if not url.startswith("https://") or time.time() - rec["ts"] > 21 * 86400 or rec.get("accepted"):
            continue
        owner, name, _, num = url.split("github.com/")[1].split("#")[0].split("/")[:4]
        rc, raw = _gh("api", "graphql", "-f", f"query={THREAD}", "-f", f"o={owner}", "-f", f"n={name}", "-F", f"k={int(num)}")
        d = ((json.loads(raw).get("data") or {}).get("repository") or {}).get("discussion") if rc == 0 else None
        if not d:
            continue
        if ((d.get("answer") or {}).get("author") or {}).get("login") == me:
            rec["accepted"] = time.time()
            log.ok(STAGE, f"answer accepted: {url}", repo=rec["repo"])
            continue
        if d["isAnswered"] or d["locked"]:
            continue
        ours = next((c for c in d["comments"]["nodes"] if c["url"] == url.split("?")[0] or (c.get("author") or {}).get("login") == me), None)
        if not ours:
            continue
        asker = (d.get("author") or {}).get("login")
        msgs = [(c["createdAt"], (c.get("author") or {}).get("login"), c["body"]) for c in d["comments"]["nodes"]]
        msgs += [(r["createdAt"], (r.get("author") or {}).get("login"), r["body"]) for r in (ours.get("replies") or {}).get("nodes") or []]
        last_ours = max(t for t, who, _ in msgs if who == me)
        new = [(t, who, b) for t, who, b in msgs if t > last_ours and who == asker]
        if not new:
            continue
        text = "\n\n".join(b for _, _, b in new)
        if THANKS.search(text) and len(text) < 400 and not re.search(r"\?|but |however|still|doesn.t|didn.t|error", text, re.I):
            if rec.get("nudged"):
                continue
            body = "Glad that worked. If it answers your question, marking it as the answer helps the next person who searches for this."
            rec["nudged"] = post(d, body, reply_to=ours["id"], disclose=False)
            done.append({"url": rec["nudged"], "kind": "nudge"})
            log.ok(STAGE, f"asked {asker} to mark the answer: {rec['nudged']}", repo=rec["repo"])
            continue
        if int(rec.get("followups") or 0) >= 3:
            continue
        thread = "\n\n".join(f"{who}: {_clip(b, 2500)}" for t, who, b in sorted(msgs))
        d["body"] = (d.get("body") or "") + f"\n\n---- The conversation so far (you are {me}, your earlier answer is in it). {asker} replied to you; answer that reply. ----\n" + thread
        d["comments"]["nodes"] = []
        a, cost = draft(d)
        st["ledger"][time.strftime("%Y-%m-%d")] = round(float(st["ledger"].get(time.strftime("%Y-%m-%d"), 0)) + cost, 2)
        rec["followups"] = int(rec.get("followups") or 0) + 1
        if a.get("answer") and a.get("body") and config.setting("DISCUSS_AUTO_POST"):
            u = post(d, a["body"], reply_to=ours["id"], disclose=False)
            done.append({"url": u, "kind": "follow-up"})
            log.ok(STAGE, f"followed up {u}", repo=rec["repo"])
    return done


def run(dry: bool = False) -> dict:
    db.init()
    rc, me = _gh("api", "user", "-q", ".login")
    if rc != 0:
        raise RuntimeError(me[:160])
    st = _state()
    cands = candidates(me, st)
    if dry:
        return {"candidates": [{"url": d["url"], "title": d["title"], "score": round(d["_score"], 2), "comments": d["comments"]["totalCount"]} for d in cands[:20]],
                "accepted_so_far": accepted(me)}
    if LOCK.exists():
        return {"skipped": "lock"}
    LOCK.write_text(str(os.getpid()))
    day = time.strftime("%Y-%m-%d")
    done = []
    try:
        try:
            fu = followups(me, st)
            _save(st)
        except Exception as e:
            fu = []
            log.warn(STAGE, f"follow-ups failed: {e}")
        # drafts that passed the checker earlier and are still unanswered go first
        if config.setting("DISCUSS_AUTO_POST"):
            for rec in [r for r in st["drafts"] if r["answer"] and not r["posted"] and time.time() - r["ts"] < 3 * 86400]:
                if len(done) >= int(config.setting("DISCUSS_PER_RUN")):
                    break
                owner, rest = rec["url"].split("github.com/")[1].split("/", 1)
                num = int(rest.rsplit("/", 1)[1])
                rc, raw = _gh("api", "graphql", "-f", "query=query($o:String!,$n:String!,$k:Int!){repository(owner:$o,name:$n){discussion(number:$k){id isAnswered locked}}}",
                              "-f", f"o={owner}", "-f", f"n={rest.split('/')[0]}", "-F", f"k={num}")
                dd = (json.loads(raw).get("data") or {}).get("repository", {}).get("discussion") if rc == 0 else None
                if not dd or dd["isAnswered"] or dd["locked"]:
                    rec["posted"] = "skipped: answered or locked meanwhile"
                    continue
                rec["posted"] = post(dd, rec["body"])
                log.ok(STAGE, f"answered {rec['posted']}", repo=rec["repo"])
                done.append(rec)
                _save(st)
        for d in cands:
            if len(done) >= int(config.setting("DISCUSS_PER_RUN")) or float(st["ledger"].get(day, 0)) >= float(config.setting("DISCUSS_DAILY_USD")):
                break
            t0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
            if sum(1 for r in st["drafts"] if str(r.get("posted", "")).startswith("http") and r["ts"] >= t0) >= int(config.setting("DISCUSS_MAX_PER_DAY")):
                log.info(STAGE, "daily answer cap reached; drafting stops until tomorrow")
                break
            st["seen"][d["url"]] = time.time()
            try:
                a, cost = draft(d)
            except Exception as e:
                log.warn(STAGE, f"draft failed for {d['url']}: {e}")
                _save(st)
                continue
            st["ledger"][day] = round(float(st["ledger"].get(day, 0)) + cost, 2)
            rec = {"ts": time.time(), "url": d["url"], "repo": d["repository"]["nameWithOwner"], "title": d["title"], "answer": bool(a.get("answer")),
                   "reason": a.get("reason", ""), "body": a.get("body", ""), "cost": round(cost, 2), "posted": ""}
            if rec["answer"] and config.setting("DISCUSS_AUTO_POST"):
                rec["posted"] = post(d, rec["body"])
                log.ok(STAGE, f"answered {rec['posted']}", repo=rec["repo"])
            elif rec["answer"]:
                log.ok(STAGE, f"drafted an answer for {d['url']} (auto-post is off)", repo=rec["repo"])
            st["drafts"].append(rec)
            _save(st)
            if rec["answer"]:
                done.append(rec)
        st["accepted"] = accepted(me)
        _save(st)
        return {"answered": [{"url": r["url"], "posted": r["posted"]} for r in done], "follow_ups": fu, "accepted_so_far": st["accepted"]}
    except Exception:
        log.error(STAGE, "discuss run crashed:\n" + traceback.format_exc())
        raise
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    print(json.dumps(run(dry="--dry" in sys.argv[1:]), indent=1, default=str))
