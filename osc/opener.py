"""Open pull requests for changes that pass every gate (user requirement 2026-09-29: at least one PR a day,
strong repos only, without lowering the quality bar).

A change is opened only when all of these hold:

* the repo is strong (stars, recent activity, outside-contributor merge rate), accepts GitHub PRs from
  non-collaborators, does not ban AI-written PR text, and its CLA (if any) is already signed;
* we have no other open PR in that repo (one at a time per project);
* the reviewer approved it at OPEN_MIN_REVIEW or better and compliance has zero failing checks after a
  fresh deterministic re-run (fork == local head, merges cleanly, no competing PR, disclosure, tests);
* the compliance agent's merge likelihood is at least OPEN_MIN_LIKELIHOOD;
* every related issue is still unclaimed right now;
* the branch is younger than OPEN_MAX_AGE_DAYS.

When the only failures are mechanical (template sections, prefixes, wording, formatting), one cheap revise
pass fixes them and the change is re-checked once before it is given up on.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from . import config, db, log

STAGE = "open"

# submission does not happen through GitHub PRs (or outside PRs are not accepted there)
NON_GITHUB = {"torvalds/linux", "golang/go", "WebKit/WebKit", "openai/openai-python", "openai/openai-node",
              "openai/openai-go", "openai/openai-agents-python", "openai/openai-agents-js"}
# CLA family by repo owner; a repo with cla_required=1 whose family is not in CLA_SIGNED is not auto-opened
CLA_FAMILY = {"google": "google", "golang": "google", "puppeteer": "google", "GoogleChrome": "google",
              "google-deepmind": "google", "microsoft": "microsoft", "Azure": "microsoft", "Comfy-Org": "comfy",
              "kubernetes": "cncf", "kubernetes-sigs": "cncf", "pytorch": "meta", "facebookresearch": "meta",
              "meta-llama": "meta", "python": "psf", "openssl": "openssl", "EleutherAI": "eleuther",
              "apache": "apache", "NVIDIA": "nvidia"}
# failures that a revise pass cannot fix
UNFIXABLE = ("no open PR by someone else", "still open and unclaimed", "CLA", "PR text must be written by the human",
             "Duplicate", "duplicate", "comment on it", "actionable", "associated with a bug", "tests pass on the branch")


def _gh_env() -> dict:
    return {**os.environ, "GH_TOKEN": config.github_token()}


def _gh(*args: str, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, env=_gh_env(), timeout=timeout)
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout))


def _raw(repo: dict) -> dict:
    raw = repo.get("raw")
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def strong_repo(repo: dict) -> str:
    """'' if the repo qualifies, else why not."""
    full = repo.get("full_name") or ""
    if not full:
        return "repo not in database"
    if full in NON_GITHUB:
        return "does not take outside GitHub PRs"
    if repo.get("archived"):
        return "archived"
    raw = _raw(repo)
    if raw.get("ai_prohibited"):
        return "project bans AI-assisted PRs"
    if raw.get("ai_prose_human"):
        return "project requires human-written PR text"
    if (repo.get("stars") or 0) < float(config.setting("STRONG_MIN_STARS")):
        return f"only {repo.get('stars')} stars"
    if (repo.get("commits_90d") or 0) < float(config.setting("STRONG_MIN_COMMITS_90D")):
        return f"only {repo.get('commits_90d')} commits in 90 days"
    if (repo.get("ext_merge_ratio") or 0) < float(config.setting("STRONG_MIN_EXT_MERGE")):
        return f"outside-PR merge rate {repo.get('ext_merge_ratio')}"
    if repo.get("cla_required"):
        fam = CLA_FAMILY.get(full.split("/")[0])
        if not fam or fam not in config.setting("CLA_SIGNED"):
            return f"CLA ({fam or 'unknown'}) not signed yet"
    return ""


def _open_repos() -> set[str]:
    return {r["repo"] for r in db.rows("SELECT repo FROM changes WHERE status='submitted'")}


def gate(cid: str, recheck: bool = True) -> tuple[bool, list[str], list[dict]]:
    """(ok, reasons it is blocked, failing compliance items)."""
    from .prefilter import claim_check, GitHubUnavailable
    c = db.parse_json_fields(db.row("SELECT * FROM changes WHERE id=?", (cid,)), ["review", "compliance"])
    repo = db.parse_json_fields(db.row("SELECT * FROM repos WHERE full_name=?", (c["repo"],)), ["raw"]) or {}
    why: list[str] = []
    if c["status"] not in ("prepared", "ready"):
        why.append(f"status is {c['status']}")
    s = strong_repo(repo)
    if s:
        why.append(f"repo: {s}")
    from .backoff import is_backed_off
    if is_backed_off(c["repo"]):
        why.append("maintainer asked us not to send PRs here (backed off)")
    if c["repo"] in _open_repos():
        why.append("we already have an open PR in this repo")
    if c.get("review_verdict") != "approve" or float(c.get("review_score") or 0) < float(config.setting("OPEN_MIN_REVIEW")):
        why.append(f"review {c.get('review_verdict')} {c.get('review_score')}")
    if time.time() - (c.get("created_at") or 0) > float(config.setting("OPEN_MAX_AGE_DAYS")) * 86400:
        why.append("branch too old; rebuild instead")
    if why:          # cheap blockers first; skip the paid/slow checks
        return False, why, []
    try:
        opp = db.parse_json_fields(db.row("SELECT related_issues FROM opportunities WHERE id=?", (c["opportunity_id"],)), ["related_issues"]) or {}
        claimed = claim_check(c["repo"], [n for n in (opp.get("related_issues") or []) if isinstance(n, int)])
    except GitHubUnavailable as e:
        return False, [f"GitHub unavailable for claim check: {e}"], []
    if claimed:
        return False, [f"issue claimed: {claimed}"], []
    comp = c.get("compliance") or {}
    if recheck or not comp:
        from .compliance import check_change
        fresh = check_change(cid, agent=not (comp.get("agent")))
        if comp.get("agent") and not fresh.get("agent"):
            fresh["agent"] = comp["agent"]
            ag_fail = [i for i in comp["agent"].get("items", []) if i.get("status") == "fail"]
            fresh["fails"] = len([i for i in fresh["deterministic"] if i["status"] == "fail"]) + len(ag_fail)
            fresh["merge_likelihood"] = comp["agent"].get("merge_likelihood")
            db.update("changes", "id", cid, {"compliance": fresh})
        comp = fresh
    fails = [i for i in (comp.get("deterministic") or []) if i.get("status") == "fail"] + \
            [i for i in ((comp.get("agent") or {}).get("items") or []) if i.get("status") == "fail"]
    if fails:
        why.append(f"{len(fails)} compliance failure(s): " + "; ".join(f["rule"][:60] for f in fails))
    ml = comp.get("merge_likelihood") or (comp.get("agent") or {}).get("merge_likelihood")
    if ml is None or float(ml) < float(config.setting("OPEN_MIN_LIKELIHOOD")):
        why.append(f"merge likelihood {ml}")
    return not why, why, fails


def fix_compliance(cid: str, fails: list[dict]) -> bool:
    """One revise pass aimed at mechanical compliance failures. True if it is worth re-gating."""
    fixable = [f for f in fails if not any(u in f["rule"] or u in str(f.get("evidence", "")) for u in UNFIXABLE)]
    if not fixable or len(fixable) != len(fails):
        return False
    from .contributor import revise_change, prepare_submission
    from .compliance import check_change
    c = db.parse_json_fields(db.row("SELECT review FROM changes WHERE id=?", (cid,)), ["review"])
    review = c.get("review") or {}
    review["required_fixes"] = [f"Compliance check failed: {f['rule']}. Evidence: {str(f.get('evidence',''))[:400]}. Fix: {f.get('fix','')}" for f in fixable]
    db.update("changes", "id", cid, {"review": review})
    log.info(STAGE, f"{cid}: revise pass for {len(fixable)} mechanical compliance failure(s)")
    res = revise_change(cid, model=config.setting("REVISE_MODEL"))
    if res.get("status") != "ready":
        return False
    prepare_submission(cid)
    check_change(cid, agent=True)
    return True


HUMANIZE_PROMPT = """Rewrite this pull request description so it reads like a regular open-source contributor wrote it quickly
and casually. Same facts, nothing new, nothing removed that a reviewer needs.

Rules:
- Keep every template heading, checkbox line, `Fixes #N` / `Closes #N` / `Related to #N` line, link, code block,
  command and command output exactly as they are. Keep any line that discloses AI assistance (reword it plainly
  if you like, but keep it).
- First person, plain words, short sentences. Sound like a dev talking to another dev, a little informal is fine.
- No em dashes or en dashes. No semicolons in prose.
- Never use: "This PR", "comprehensive", "robust", "seamless", "leverage", "ensure", "delve", "streamline",
  "enhance", "furthermore", "additionally", "in summary", "it's worth noting", "I hope this helps".
- No bold text in prose, no emoji, no closing pleasantries. Cut filler and anything that repeats the title.
- Shorter is better. Keep it under the original length.

Title (do not change unless it has an em dash, then just swap it for a colon or comma):
__TITLE__

Description:
__BODY__

Answer with JSON: {"title": "...", "body": "..."}"""

_DASHES = (("\u2014", ", "), ("\u2013", "-"))


def _strip_dashes(t: str) -> str:
    for a, b in _DASHES:
        t = t.replace(f" {a} ", b).replace(a, b)
    return t


def humanize(cid: str) -> None:
    """Casual, human-sounding PR text right before opening (user requirement). Falls back to dash stripping."""
    from .claude_runner import run_claude, extract_json
    from .analyzer import workspace_path
    c = db.row("SELECT repo, pr_title, pr_body FROM changes WHERE id=?", (cid,))
    title, body = c["pr_title"] or "", c["pr_body"] or ""
    try:
        res = run_claude(HUMANIZE_PROMPT.replace("__TITLE__", title).replace("__BODY__", body[:9000]),
                         config.ROOT if not workspace_path(c["repo"]).exists() else workspace_path(c["repo"]), model="sonnet", max_turns=2, stage=STAGE, repo=c["repo"],
                         allowed_tools=[], json_schema={"type": "object", "required": ["title", "body"],
                                                        "properties": {"title": {"type": "string"}, "body": {"type": "string"}}},
                         max_budget_usd=1.0, timeout_s=600)
        data = res.structured if isinstance(res.structured, dict) else extract_json(res.text)
        if isinstance(data, dict) and data.get("body"):
            # never lose issue links or code blocks
            keep = re.findall(r"(?:Fixes|Closes|Resolves|Related to) #\d+", body) + re.findall(r"```", body)
            if all(k in data["body"] for k in set(keep)) and data["body"].count("```") == body.count("```"):
                title, body = data.get("title") or title, data["body"]
            else:
                log.warn(STAGE, f"{cid}: humanized body dropped a link or code block; keeping original wording")
    except Exception as e:
        log.warn(STAGE, f"{cid}: humanize failed ({e}); stripping dashes only")
    db.update("changes", "id", cid, {"pr_title": _strip_dashes(title), "pr_body": _strip_dashes(body)})


def open_pr(cid: str) -> str:
    humanize(cid)
    c = db.row("SELECT * FROM changes WHERE id=?", (cid,))
    rc, me = _gh("api", "user", "-q", ".login")
    me = me.strip()
    rc, base = _gh("api", f"repos/{c['repo']}", "-q", ".default_branch")
    base = base.strip()
    # never open a second PR for the same branch
    rc, existing = _gh("api", f"repos/{c['repo']}/pulls?head={me}:{c['branch']}&state=all", "-q", ".[0].html_url")
    if existing.strip():
        url = existing.strip()
    else:
        payload = json.dumps({"title": c["pr_title"], "body": c["pr_body"] or "", "head": f"{me}:{c['branch']}",
                              "base": base, "maintainer_can_modify": True})
        r = subprocess.run(["gh", "api", "-X", "POST", f"repos/{c['repo']}/pulls", "--input", "-", "-q", ".html_url"],
                           input=payload, capture_output=True, text=True, env=_gh_env(), timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"PR create failed: {(r.stderr or r.stdout)[-300:]}")
        url = r.stdout.strip()
    db.update("changes", "id", cid, {"status": "submitted", "pr_url": url, "status_note": f"PR opened automatically {time.strftime('%Y-%m-%d %H:%M')}", "updated_at": time.time()})
    db.update("opportunities", "id", c["opportunity_id"], {"status": "submitted", "updated_at": time.time()})
    log.ok(STAGE, f"opened {url}", repo=c["repo"])
    return url


TEXT_RULES = ("template", "PR body", "pr body", "checkbox", "Disclosure", "disclosure", "PR title", "title format", "sections")

PR_TEXT_PROMPT = """You are fixing ONLY the pull request description for a change to __REPO__ that is otherwise ready.
Do not touch any code. Read the repo's PR template (.github/PULL_REQUEST_TEMPLATE.md or similar), CONTRIBUTING and any
AI-use policy so the description satisfies them exactly.

Compliance failures to fix:
__FAILS__

What was actually done (use this for checkboxes and testing sections, never claim more):
- tests run: __TESTS__
- diff: __STATS__

Current title: __TITLE__
Current description:
__BODY__

Rules:
- Keep every heading from the template, in order, with real content under each (or "N/A" if the template allows it).
- Tick a checkbox only if it is factually true from the info above. Delete boxes that don't apply when the template
  allows that. If a box is a personal attestation only the human account owner can truthfully make (for example
  "I have reviewed and understand every line", "I have tested this on my machine", signing something), leave it
  unticked and list it in needs_human.
- Include AI-use disclosure exactly in the form the repo asks for (if it asks "how AI helped", say it plainly: the
  change was drafted with an AI coding assistant and checked by running the tests listed).
- Casual first-person voice, short sentences, no em dashes, no "This PR", no filler words.
Answer with JSON: {"title": "...", "body": "...", "needs_human": ["box text", ...]}"""


def fix_pr_text(cid: str, fails: list[dict]) -> bool:
    """Text-only fix for template / checkbox / disclosure failures. True if the gate is worth re-running."""
    from .claude_runner import run_claude, extract_json
    from .analyzer import live_path
    from .compliance import check_change
    c = db.parse_json_fields(db.row("SELECT * FROM changes WHERE id=?", (cid,)), ["tests_run", "diff_stats"])
    tests = "; ".join(f"`{t.get('command')}` -> {t.get('result')}" for t in (c.get("tests_run") or [])) or "none recorded"
    prompt = (PR_TEXT_PROMPT.replace("__REPO__", c["repo"]).replace("__TESTS__", tests[:2000])
              .replace("__STATS__", json.dumps(c.get("diff_stats") or {})[:500]).replace("__TITLE__", c["pr_title"] or "")
              .replace("__BODY__", (c["pr_body"] or "")[:9000])
              .replace("__FAILS__", "\n".join(f"- {f['rule']}: {str(f.get('evidence',''))[:400]} (fix: {f.get('fix','')})" for f in fails)))
    res = run_claude(prompt, live_path(c["repo"]), model="sonnet", max_turns=15, stage=STAGE, repo=c["repo"],
                     allowed_tools=["Read", "Grep", "Glob", "LS"], max_budget_usd=2.0, timeout_s=900,
                     json_schema={"type": "object", "required": ["title", "body", "needs_human"],
                                  "properties": {"title": {"type": "string"}, "body": {"type": "string"},
                                                 "needs_human": {"type": "array", "items": {"type": "string"}}}})
    data = res.structured if isinstance(res.structured, dict) else extract_json(res.text)
    if not isinstance(data, dict) or not data.get("body"):
        log.warn(STAGE, f"{cid}: PR text fix failed: {res.error[:200]}")
        return False
    upd = {"pr_title": _strip_dashes(data.get("title") or c["pr_title"]), "pr_body": _strip_dashes(data["body"])}
    if data.get("needs_human"):
        upd["status_note"] = (c.get("status_note") or "") + "\nHOLD: template asks you personally to confirm: " + "; ".join(data["needs_human"])[:300]
        db.update("changes", "id", cid, upd)
        log.info(STAGE, f"{cid}: held for your checkbox: {data['needs_human']}")
        return False
    db.update("changes", "id", cid, upd)
    log.info(STAGE, f"{cid}: PR text rewritten for {len(fails)} template/disclosure failure(s) (${res.cost_usd:.2f})")
    check_change(cid, agent=True)
    return True


def try_open(cid: str, allow_fix: bool = True) -> dict:
    ok, why, fails = gate(cid)
    only_compliance = bool(fails) and all("compliance failure" in w for w in why)
    if not ok and allow_fix and only_compliance:
        text_only = all(any(k in f["rule"] for k in TEXT_RULES) for f in fails)
        if text_only:
            if fix_pr_text(cid, fails):
                ok, why, fails = gate(cid, recheck=False)
        elif fix_compliance(cid, fails):
            ok, why, fails = gate(cid, recheck=False)
    if not ok:
        log.info(STAGE, f"{cid} not opened: {' | '.join(why)}")
        return {"change": cid, "opened": False, "why": why}
    if not config.setting("AUTO_OPEN"):
        return {"change": cid, "opened": False, "why": ["AUTO_OPEN is off"]}
    # a genuine not-yet-public security fix must be disclosed privately, never opened as a public PR
    if config.setting("DISCLOSE_CHECK"):
        opp = db.row("SELECT kind FROM opportunities o JOIN changes c ON c.opportunity_id=o.id WHERE c.id=?", (cid,))
        if opp and (opp.get("kind") or "") == "security":
            try:
                from .disclose import handle_security
                d = handle_security(cid)
                if d["decision"] != "public_pr":
                    return {"change": cid, "opened": False, "why": [f"security: routed to {d['decision']} disclosure, not a public PR"]}
            except Exception as e:
                log.warn(STAGE, f"{cid}: disclosure check failed, holding: {e}")
                return {"change": cid, "opened": False, "why": [f"disclosure check failed: {e}"]}
    return {"change": cid, "opened": True, "url": open_pr(cid)}


def backlog() -> list[str]:
    """Prepared/ready changes, best first, that are not blocked by the cheap checks."""
    rows = db.rows("SELECT c.id, c.repo, c.review_score, c.compliance, r.median_merge_days, r.ext_merge_ratio FROM changes c "
                   "LEFT JOIN repos r ON r.full_name=c.repo WHERE c.status IN ('prepared','ready') ORDER BY c.review_score DESC")
    out = []
    for r in rows:
        comp = json.loads(r["compliance"]) if r["compliance"] else {}
        ml = float((comp.get("agent") or {}).get("merge_likelihood") or 0)
        # merged PRs are what count (Pull Shark tiers): prefer repos that merge outside PRs often and fast
        speed = 1.0 / (1.0 + float(r["median_merge_days"] or 7) / 7)
        out.append(((r["review_score"] or 0) * max(ml, 1) * (0.5 + float(r["ext_merge_ratio"] or 0.3)) * (0.5 + speed), r["id"]))
    return [cid for _, cid in sorted(out, reverse=True)]


def blocker(c: dict, open_repos: set[str] | None = None) -> str:
    """First hard reason a built change can't be opened as a PR right now, or '' if nothing blocks it.

    Cheap (no network): used by the dashboard. try_open()/gate() still re-check live state before opening.
    """
    if c.get("status") not in ("prepared", "ready"):
        return ""
    repo = db.parse_json_fields(db.row("SELECT * FROM repos WHERE full_name=?", (c["repo"],)), ["raw"]) or {}
    raw = _raw(repo) if repo else {}
    full = c["repo"]
    from .backoff import is_backed_off
    if is_backed_off(full):
        return "a maintainer asked us not to send PRs here (backed off)"
    if full in NON_GITHUB:
        return "project doesn't take GitHub PRs (Gerrit / mailing list / Bugzilla flow)"
    if raw.get("ai_prohibited"):
        return "project bans AI-assisted PRs"
    if raw.get("ai_prose_human"):
        return "project requires the PR text to be written by you"
    if repo.get("cla_required"):
        fam = CLA_FAMILY.get(full.split("/")[0])
        if not fam or fam not in config.setting("CLA_SIGNED"):
            return f"CLA not signed yet ({fam or 'unknown family'})"
    if full in (open_repos if open_repos is not None else _open_repos()):
        return "you already have an open PR in this repo"
    note = c.get("status_note") or ""
    if "SECURITY-DISCLOSURE" in note:
        return "security finding: needs private disclosure, not a public PR (see data/disclose_state.json)"
    for marker in ("HOLD:", "HOLD ", "BLOCKED"):
        i = note.rfind(marker)
        if i >= 0:
            return "on hold: " + note[i:].split("\n")[0][len(marker):].strip(" :")[:160]
    if c.get("review_verdict") != "approve" or float(c.get("review_score") or 0) < float(config.setting("OPEN_MIN_REVIEW")):
        return f"reviewer asked for revisions ({c.get('review_verdict')} {c.get('review_score')}/10)"
    return ""


def opened_on(day: str) -> list[str]:
    """PR urls we opened on `day` (YYYY-MM-DD, local), automatically or by hand. GitHub is the source of truth."""
    # -user:@me leaves out PRs in our own repos (daily-lab etc.); only upstream contributions count
    rc, out = _gh("api", "-X", "GET", "search/issues", "-f", f"q=author:@me is:pr created:{day} -user:@me", "-q", ".items[].html_url")
    if rc == 0:
        return [u for u in out.split() if u.startswith("https://")]
    return [r["pr_url"] for r in db.rows("SELECT pr_url FROM changes WHERE status_note LIKE ?", (f"%PR opened automatically {day}%",))]


def open_quota(want: int, only: list[str] | None = None) -> list[dict]:
    """Try changes best-first until `want` PRs are opened. Returns every attempt."""
    tried = []
    opened = 0
    open_repos = _open_repos()
    cap = int(config.setting("DAILY_TARGET_PRS")) - len(opened_on(time.strftime("%Y-%m-%d")))
    want = min(want, max(0, cap))     # hard daily cap, whoever calls this
    for cid in (only if only is not None else backlog()):
        if opened >= want:
            break
        c = db.row("SELECT * FROM changes WHERE id=?", (cid,))
        if not c or blocker(c, open_repos):
            continue
        try:
            r = try_open(cid)
        except Exception as e:
            r = {"change": cid, "opened": False, "why": [f"error: {str(e)[:200]}"]}
        r["repo"] = c["repo"]
        tried.append(r)
        if r.get("opened"):
            opened += 1
            open_repos.add(c["repo"])
    return tried
