"""Unattended daily run: scan -> scout -> build -> review -> compliance -> push to fork -> digest.

Opens PRs on its own for changes that pass every gate in osc/opener.py (user authorized 2026-10-02:
at least one new PR a day, casual human-sounding text). Anything that fails a gate waits on the fork.
Run with ``python -m osc.daily`` (see scripts/daily.sh and the launchd plist).
"""
from __future__ import annotations

import json
import os
import re
import signal
import smtplib
import ssl
import subprocess
import sys
import time
import traceback
from email.mime.text import MIMEText
from pathlib import Path

from . import config, db, log
from .housekeeping import PROJECT, apply_isolation, cleanup, report as disk_report

STAGE = "daily"
LOCK = PROJECT / "data" / "daily.lock"
REPORTS = PROJECT / "data" / "reports"
DENY = {"openai/codex", "pq-code-package/mlkem-native", "PQClean/PQClean", "huggingface/transformers",
        "huggingface/trl", "rust-lang/rust", "streamlit/streamlit", "microsoft/playwright", "n8n-io/n8n",
        "openai/openai-python", "openai/openai-agents-python", "openai/openai-node", "bojieli/ai-agent-book"}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _gh_env() -> dict:
    return {**os.environ, "GH_TOKEN": config.github_token(), "GIT_TERMINAL_PROMPT": "0"}


def _gh(*args: str, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, env=_gh_env(), timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


# ---------------------------------------------------------------- phase 1: PR states
def refresh_prs(since_ts: float) -> dict:
    """Update merged/closed statuses and sort new maintainer activity on our open PRs into
    good news (approvals, CI commands) and things that actually need a reply."""
    out = {"merged": [], "closed": [], "activity": [], "good": []}
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_ts))
    # merges recorded by hand (e.g. during a live session) since the last run still count
    for c in db.rows("SELECT repo, pr_url, pr_title FROM changes WHERE status='merged' AND updated_at>=? AND pr_url IS NOT NULL", (since_ts,)):
        m = re.search(r"/pull/(\d+)$", c["pr_url"] or "")
        if m:
            out["merged"].append(f"{c['repo']}#{m.group(1)} {c['pr_title']}")
    for c in db.rows("SELECT id, repo, pr_url, pr_title FROM changes WHERE status='submitted' AND pr_url IS NOT NULL"):
        m = re.search(r"/pull/(\d+)$", c["pr_url"] or "")
        if not m:
            continue
        num = m.group(1)
        rc, js = _gh("pr", "view", num, "--repo", c["repo"], "--json", "state,mergedAt")
        if rc != 0:
            continue
        st = json.loads(js)
        if st.get("state") == "MERGED":
            db.update("changes", "id", c["id"], {"status": "merged", "updated_at": time.time()})
            out["merged"].append(f"{c['repo']}#{num} {c['pr_title']}")
            continue
        if st.get("state") == "CLOSED":
            db.update("changes", "id", c["id"], {"status": "closed", "updated_at": time.time()})
            out["closed"].append(f"{c['repo']}#{num} {c['pr_title']}")
            continue
        items = []   # (timestamp, author, kind, text)
        for path, jq in ((f"repos/{c['repo']}/issues/{num}/comments?per_page=100",
                          '.[] | [.created_at, .user.login, .user.type, "comment", (.body|gsub("\n";" ")|.[0:240])] | @json'),
                         (f"repos/{c['repo']}/pulls/{num}/comments?per_page=100",
                          '.[] | [.created_at, .user.login, .user.type, "inline", ((.path // "") + ": " + (.body|gsub("\n";" ")|.[0:220]))] | @json'),
                         (f"repos/{c['repo']}/pulls/{num}/reviews?per_page=100",
                          '.[] | [.submitted_at, .user.login, .user.type, .state, ((.body // "")|gsub("\n";" ")|.[0:240])] | @json')):
            rc, raw = _gh("api", path, "--jq", jq)
            for ln in raw.splitlines() if rc == 0 else []:
                try:
                    items.append(tuple(json.loads(ln)))
                except (ValueError, TypeError):
                    pass
        ours = max((t for t, who, *_ in items if who == "stanleys12" and t), default="")
        new = [(t, who, kind, text) for t, who, typ, kind, text in items
               if t and t > since and who != "stanleys12" and typ != "Bot" and not who.endswith("[bot]")
               and not who.lower().startswith(("codecov", "coveralls", "sonarcloud", "netlify", "vercel"))]
        good, needs = [], []
        for t, who, kind, text in new:
            line = f"{who} [{kind}]: {text}".rstrip(": ")
            if kind == "APPROVED" or (kind in ("comment", "COMMENTED") and text.strip().startswith("/")):
                good.append(line)                     # approval or a CI bot command
            elif t <= ours:
                continue                              # we already answered after it
            elif kind == "COMMENTED" and not text.strip():
                continue                              # review wrapper for inline comments, reported below
            else:
                needs.append(line)
        if needs:
            out["activity"].append({"pr": f"{c['repo']}#{num}", "url": c["pr_url"], "lines": needs[:6]})
        if good:
            out["good"].append({"pr": f"{c['repo']}#{num}", "url": c["pr_url"], "lines": good[:4]})
    return out


# ---------------------------------------------------------------- phase 2: pick repos
GPU_HINTS = ("cuda", "gpu", "rocm", "tensorrt", "triton-inference", "cutlass")


def needs_gpu(r: dict) -> bool:
    """Repos whose whole point is GPU acceleration: nothing can be built or tested on this machine
    (no NVIDIA GPU). LLM serving repos with CPU-testable Python paths (vLLM, sglang) stay eligible."""
    topics = r.get("topics") or "[]"
    topics = topics if isinstance(topics, list) else json.loads(topics)
    gpu_topic = any(t.lower() in GPU_HINTS for t in topics)
    desc = (r.get("description") or "").lower()
    name = (r.get("full_name") or "").lower()
    gpu_desc = any(k in desc for k in ("gpu-accelerated", "gpu accelerated", "cuda", "on gpus", "for gpus", "for gpu", "nvidia gpus", "tensorrt"))
    # many CUDA-library topics (cudnn, cublas, nccl, ...) means the code is GPU-bound end to end
    cuda_libs = {"cuda", "cudnn", "cublas", "cusolver", "cusparse", "curand", "nccl", "nvrtc", "nvtx", "cutensor", "cutlass", "rocm", "hip"}
    gpu_desc = gpu_desc or sum(t.lower() in cuda_libs for t in topics) >= 3
    gpu_desc = gpu_desc or any(k in name for k in ("tensorrt", "cutlass", "cudnn", "cuda", "flashinfer", "rapidsai/"))
    lang = (r.get("language") or "").lower()
    return gpu_desc or (gpu_topic and lang in ("cuda", "c++", "c"))


def _not_openable(r: dict) -> str:
    from .opener import strong_repo
    full = r.get("full_name") or r.get("repo") or ""
    row = r if "stars" in r else (db.parse_json_fields(db.row("SELECT * FROM repos WHERE full_name=?", (full,)), ["raw"]) or {})
    return strong_repo(row)


def pick_repos(n: int, reanalyze_days: float | None = None) -> list[str]:
    """Best repos to scout now: up to DAILY_HELPWANTED_REPOS projects that are asking for outside help,
    the rest from the ranked domain lists."""
    from .scanner import rank
    domains = config.setting("DAILY_DOMAINS")
    reanalyze = float(reanalyze_days if reanalyze_days is not None else config.setting("DAILY_REANALYZE_DAYS")) * 86400
    now = time.time()
    busy = {r["repo"] for r in db.rows("SELECT DISTINCT repo FROM changes WHERE status IN ('submitted','ready','prepared','approved')")}
    seen: set[str] = set()

    def ok(r: dict) -> bool:
        full = r["full_name"]
        if full in seen or full in DENY or full in busy or r.get("archived"):
            return False
        seen.add(full)
        raw = r.get("raw") if isinstance(r.get("raw"), dict) else json.loads(r.get("raw") or "{}")
        if raw.get("ai_prohibited") or r.get("status") == "skipped" or needs_gpu(r):
            return False
        if config.setting("AUTO_OPEN") and _not_openable(r):
            return False   # a PR there could never be opened automatically (CLA, weak repo, human-written text rule)
        return (r.get("analyzed_at") or 0) <= now - reanalyze

    helped: list[str] = []
    try:
        from .helpwanted import ranked
        for r in ranked():
            if len(helped) >= int(config.setting("DAILY_HELPWANTED_REPOS")):
                break
            full = db.row("SELECT * FROM repos WHERE full_name=?", (r["full_name"],))
            if full and ok(full):
                helped.append(r["full_name"])
    except Exception as e:
        log.warn(STAGE, f"help-wanted pick failed: {e}")
    picked = []
    for dom in domains:
        for r in rank(limit=60, domain=dom):
            if ok(r):
                picked.append((r.get("score") or 0, r["full_name"]))
    picked.sort(reverse=True)
    if helped:
        log.info(STAGE, f"help-wanted repos this run: {helped}")
    return (helped + [f for _, f in picked])[:n]


# ---------------------------------------------------------------- phase 3/4: scout + build
def scout(repos: list[str], focus: str) -> dict:
    """Scout only repos whose tracker has unclaimed, maintainer-confirmed issues (a free API check)."""
    import concurrent.futures as cf
    from .analyzer import analyze_repo
    from .prefilter import GitHubUnavailable, shortlist_issues, shortlist_text
    res = {}

    def one(r: str) -> tuple[str, dict]:
        t0 = time.time()
        try:
            try:
                short = shortlist_issues(r)
            except GitHubUnavailable as e:
                return r, {"ok": False, "error": f"GitHub API unavailable ({str(e)[:80]}); left for tomorrow", "cost": 0, "s": round(time.time() - t0)}
            if not short:
                db.update("repos", "full_name", r, {"analyzed_at": time.time(), "status_note": "daily: no unclaimed confirmed issues; scout skipped"})
                return r, {"ok": True, "opps": 0, "cost": 0, "s": round(time.time() - t0), "skipped": "no unclaimed confirmed issues"}
            a = analyze_repo(r, focus=focus + "\n\n" + shortlist_text(short))
            return r, {"ok": True, "opps": len(a.get("opportunities", [])), "cost": a.get("cost_usd") or 0, "s": round(time.time() - t0), "shortlist": len(short)}
        except Exception as e:  # keep going, one bad repo must not end the day
            return r, {"ok": False, "error": str(e)[:200], "cost": 0, "s": round(time.time() - t0)}

    with cf.ThreadPoolExecutor(int(config.setting("SCOUT_PARALLEL"))) as ex:
        for r, out in ex.map(one, repos):
            res[r] = out
            log.info(STAGE, f"scout {r}: {out}")
    return res


def candidates(repos: list[str]) -> list[dict]:
    """Best opportunity per repo: tonight's scouted repos first, then the backlog of unbuilt
    opportunities from earlier scouts (no scouting cost) that still clear the same floors."""
    minp, mina = float(config.setting("DAILY_MIN_PRIORITY")), float(config.setting("DAILY_MIN_ACCEPT"))
    busy = {r["repo"] for r in db.rows("SELECT DISTINCT repo FROM changes WHERE status IN ('submitted','ready','prepared','approved','building')")}
    max_age = time.time() - float(config.setting("DAILY_BACKLOG_MAX_AGE_DAYS")) * 86400

    def ok(o) -> bool:
        return (o["kind"] not in ("docs", "tests", "chore") and (o["priority"] or 0) >= minp
                and (o["accept_likelihood"] or 0) >= mina and o["repo"] not in busy
                and not (config.setting("AUTO_OPEN") and _not_openable({"repo": o["repo"]})))

    fresh, backlog, seen = [], [], set()
    for r in repos:
        for o in db.rows("SELECT id, repo, title, kind, priority, accept_likelihood, confidence FROM opportunities "
                         "WHERE repo=? AND status='proposed' ORDER BY priority DESC", (r,)):
            if ok(o):
                fresh.append(o); seen.add(r)
                break   # one per repo
    for o in db.rows("SELECT o.id, o.repo, o.title, o.kind, o.priority, o.accept_likelihood, o.confidence, "
                     "r.full_name, r.topics, r.description, r.language FROM opportunities o JOIN repos r ON r.full_name=o.repo "
                     "WHERE o.status='proposed' AND o.created_at>=? ORDER BY o.priority*o.accept_likelihood DESC", (max_age,)):
        if o["repo"] in seen or o["repo"] in DENY or not ok(o) or needs_gpu(o):
            continue
        if re.search(r"\b(cuda|gpu|nccl|deepspeed|rocm|triton kernel|fsdp)\b", o["title"] or "", re.I):
            continue   # the fix itself needs a GPU to test
        seen.add(o["repo"])
        backlog.append(o)
    key = lambda o: (o["priority"] or 0) * (o["accept_likelihood"] or 0)
    fresh.sort(key=key, reverse=True)
    if backlog:
        log.info(STAGE, f"backlog candidates: {[o['repo'] for o in backlog[:8]]}")
    return fresh + backlog


def build(opps: list[dict], budget_left: float, max_builds: int, min_free: float, open_left: int = 0) -> list[dict]:
    """Build best-first. With open_left > 0, each prepared change is tried for opening right away and
    building stops once that many PRs are open."""
    from .contributor import build_opportunity, prepare_submission
    from .compliance import check_change
    from .housekeeping import _free_gb
    from .opener import open_quota
    done = []
    started = 0
    quota = open_left > 0
    for o in opps:
        if started >= max_builds:
            break
        if quota and open_left <= 0:
            log.info(STAGE, "today's PR target reached; stopping builds")
            break
        if budget_left <= 0:
            log.warn(STAGE, "daily budget exhausted; stopping builds")
            break
        from .housekeeping import ensure_space
        if not ensure_space(min_free + 3, keep={o["repo"]}):
            log.warn(STAGE, f"free disk below {min_free + 3} GB even after reclaiming; stopping builds")
            break
        t0 = time.time()
        try:
            from .prefilter import claim_check
            issues = json.loads(db.row("SELECT related_issues FROM opportunities WHERE id=?", (o["id"],))["related_issues"] or "[]")
            why = claim_check(o["repo"], [n for n in issues if isinstance(n, int)])
            if why:
                db.update("opportunities", "id", o["id"], {"status": "rejected", "status_note": f"claimed before build: {why}", "updated_at": time.time()})
                done.append({"repo": o["repo"], "opportunity": o["id"], "status": "skipped", "cost": 0, "title": o["title"], "error": f"claimed: {why}"})
                log.info(STAGE, f"skip {o['repo']}: {why}")
                continue
            started += 1
            ch = build_opportunity(o["id"])
            cid = ch["id"]
            row = db.row("SELECT status, cost_usd, pr_title FROM changes WHERE id=?", (cid,))
            cost = row["cost_usd"] or 0
            budget_left -= cost
            entry = {"repo": o["repo"], "change": cid, "status": row["status"], "cost": round(cost, 1), "title": row["pr_title"], "s": round(time.time() - t0)}
            if row["status"] == "ready":
                prepare_submission(cid)
                comp = check_change(cid, agent=True)
                entry["merge_likelihood"] = comp.get("merge_likelihood")
                entry["fails"] = comp.get("fails")
                entry["status"] = "prepared"
                budget_left -= float(comp.get("cost_usd") or 0)
                if open_left > 0 and config.setting("AUTO_OPEN"):
                    att = open_quota(1, only=[cid])
                    if att and att[0].get("opened"):
                        entry["opened"] = att[0]["url"]
                        entry["status"] = "submitted"
                        open_left -= 1
                    elif att:
                        entry["not_opened"] = att[0].get("why")
            done.append(entry)
        except Exception as e:
            done.append({"repo": o["repo"], "opportunity": o["id"], "error": str(e)[:300]})
        log.info(STAGE, f"build {o['repo']}: {done[-1]}")
    return done


def _quota_unmet(day: str, state: dict) -> bool:
    """True when today's PR minimum isn't met and rescue money is left."""
    if not config.setting("AUTO_OPEN"):
        return False
    if float(state.get("rescue", {}).get(day, 0)) >= float(config.setting("DAILY_RESCUE_USD")):
        return False
    try:
        from .opener import opened_on
        return len(opened_on(day)) < int(config.setting("DAILY_MIN_PRS"))
    except Exception:
        return False


# ---------------------------------------------------------------- digest
def _aura_env(key: str) -> str:
    try:
        m = re.search(rf"^{key}=(.*)$", Path(os.environ.get("OSC_MAIL_ENV", Path.home() / "aura" / ".env")).read_text(), re.M)
        return m.group(1).strip() if m else ""
    except OSError:
        return ""


def send_digest(subject: str, body: str) -> bool:
    to = config.setting("DAILY_DIGEST_TO")
    if not to:
        log.warn(STAGE, "DAILY_DIGEST_TO is not set; digest written to disk only")
        return False
    sender, pw = _aura_env("WORKDAY_EMAIL") or to, _aura_env("GMAIL_APP_PASSWORD").replace(" ", "")
    if not pw:
        log.warn(STAGE, "no DAILY_DIGEST_TO or GMAIL_APP_PASSWORD (see OSC_MAIL_ENV); digest written to disk only")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"], msg["From"], msg["To"] = subject, sender, to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(sender, pw)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warn(STAGE, f"digest send failed: {type(e).__name__}: {e}")
        return False


def compose_digest(day: str, prs: dict, scouted: dict, built: list[dict], disk: dict, spent: float, skipped: str | None,
                   month_spent: float = 0.0, opened: list[dict] | None = None, today_prs: list[str] | None = None) -> str:
    L = [f"OSS contribution engine, daily run {day}", ""]
    if skipped:
        L += [f"RUN SKIPPED: {skipped}", ""]
    new = [o["url"] for o in (opened or []) if o.get("opened")] + [b["opened"] for b in built if b.get("opened")]
    L.append(f"PRS OPENED TODAY: {len(today_prs or [])} (daily minimum {config.setting('DAILY_MIN_PRS')})")
    L += [f"  - {u}{'  (this run)' if u in new else ''}" for u in (today_prs or [])]
    if not today_prs:
        L.append("  - none yet. Nothing passed every gate; see BUILT BUT NOT READY below.")
    L.append("")
    if prs["merged"]:
        L += ["MERGED since last run:"] + [f"  - {m}" for m in prs["merged"]] + [""]
    if prs["closed"]:
        L += ["CLOSED without merge:"] + [f"  - {m}" for m in prs["closed"]] + [""]
    if prs.get("good"):
        L.append("GOOD NEWS (approvals / maintainers re-running CI; no reply needed):")
        for a in prs["good"]:
            L.append(f"  - {a['pr']}  {a['url']}")
            L += [f"      {l}" for l in a["lines"]]
        L.append("")
    try:
        from .badges import report
        blines, bnews = report()
        if bnews:
            L += ["NEW BADGES / TIERS:"] + [f"  - {n}" for n in bnews] + [""]
            send_digest(f"[oss-contrib] new GitHub badge: {', '.join(bnews)}", "\n".join(bnews) + "\n\nhttps://github.com/stanleys12?tab=achievements\n")
        L += ["BADGES:"] + blines + [""]
    except Exception as e:
        log.warn(STAGE, f"badge report failed: {e}")
    if prs.get("followups"):
        L += ["ANSWERED AUTOMATICALLY since last run (replies posted, fixes pushed):"] + prs["followups"] + [""]
    if prs["activity"]:
        L.append("WAITING FOR A REPLY (answered automatically about 2 hours after the reviewer's last comment):")
        for a in prs["activity"]:
            L.append(f"  - {a['pr']}  {a['url']}")
            L += [f"      {l}" for l in a["lines"]]
        L.append("")
    ready = [b for b in built if b.get("status") == "prepared"]
    if ready:
        L.append("BUILT AND PUSHED TO YOUR FORK, NOT OPENED (a gate blocked it):")
        for b in ready:
            row = db.row("SELECT o.rationale AS rationale FROM changes c LEFT JOIN opportunities o ON o.id=c.opportunity_id WHERE c.id=?", (b["change"],)) or {}
            L.append(f"  - {b['repo']}: {b['title']}")
            L.append(f"      change {b['change']}  merge-likelihood {b.get('merge_likelihood')}/10  cost ${b['cost']}")
            why = (row.get("rationale") or "").strip().replace("\n", " ")
            if why:
                L.append(f"      why: {why[:400]}")
            oc = REPORTS / b["repo"].replace("/", "__") / b["change"] / "one_click_pr_url.txt"
            if oc.exists():
                L.append(f"      one-click compare: {oc.read_text().strip()}")
            L.append(f"      open with: say \"open {b['change']}\" in a Claude Code session, or use the dashboard http://127.0.0.1:8791")
        L.append("")
    other = [b for b in built if b.get("status") != "prepared"]
    if other:
        L.append("BUILT BUT NOT READY:")
        for b in other:
            L.append(f"  - {b.get('repo')}: {b.get('title') or b.get('opportunity')}  status={b.get('status') or 'error'}  {b.get('error','')[:160]}")
        L.append("")
    L.append("SCOUTED:")
    for r, s in scouted.items():
        what = s.get("skipped") or ('%d opportunities from %d pre-checked issues' % (s['opps'], s.get('shortlist', 0)) if s.get('ok') else 'failed: ' + s.get('error',''))
        L.append(f"  - {r}: {what} (${s.get('cost',0):.1f}, {s.get('s',0)}s)")
    L += ["", f"Spend today: ${spent:.1f} (cap ${float(config.setting('DAILY_BUDGET_USD')):.0f}); rolling 30 days: ${month_spent:.0f} (cap ${float(config.setting('MONTHLY_BUDGET_USD')):.0f})",
          f"Disk: {disk['free_gb']} GB free; workspace {disk['workspace_mb']//1024} GB; project cache {disk['cache_mb']//1024} GB",
          f"Playbook: {PROJECT}/data/reports/PLAYBOOK.md", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- main
def run(dry: bool = False) -> dict:
    db.init()
    apply_isolation()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            other = int(LOCK.read_text().strip() or 0)
        except ValueError:
            other = 0
        alive = other > 0 and _pid_alive(other)
        if alive and time.time() - LOCK.stat().st_mtime < 6 * 3600:
            log.warn(STAGE, f"another daily run is active (pid {other}); exiting")
            return {"skipped": "lock"}
        log.warn(STAGE, f"removing stale lock (pid {other} {'alive but >6h' if alive else 'not running'})")
        LOCK.unlink(missing_ok=True)
    LOCK.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    day = time.strftime("%Y-%m-%d")
    state_f = PROJECT / "data" / "daily_state.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    t_start = time.time()
    skipped = None
    scouted, built, opened = {}, [], []
    try:
        # 0. housekeeping before anything heavy
        hk = cleanup(dry_run=dry)
        # 1. PR states + activity
        prs = refresh_prs(state.get("last_run", t_start - 86400))
        if not dry and t_start - float((db.kv_get("helpwanted_last") or {}).get("ts", 0)) > 20 * 3600:
            try:
                from .helpwanted import refresh
                refresh()
            except Exception as e:
                log.warn(STAGE, f"help-wanted refresh failed: {e}")
        try:
            from .responder import digest_lines
            prs["followups"] = digest_lines(state.get("last_run", t_start - 86400))
        except Exception as e:
            log.warn(STAGE, f"follow-up summary failed: {e}")
        # (discovery rescan happens at the end so it cannot starve the prefilter of API quota)
        # 3. pick + scout (unless the rolling 30-day spend is over the monthly cap)
        ledger = state.setdefault("ledger", {})
        # both daily runs share one budget: the afternoon run only spends what the morning run left
        budget = float(config.setting("DAILY_BUDGET_USD")) - float(ledger.get(day, 0))
        month_spent = sum(v for d, v in ledger.items() if d >= time.strftime("%Y-%m-%d", time.gmtime(t_start - 30 * 86400)))
        if month_spent >= float(config.setting("MONTHLY_BUDGET_USD")):
            skipped = f"rolling 30-day spend ${month_spent:.0f} is over the ${float(config.setting('MONTHLY_BUDGET_USD')):.0f} cap; only PR states were refreshed"
        elif budget <= 1 and not _quota_unmet(day, state):
            skipped = f"today's ${float(config.setting('DAILY_BUDGET_USD')):.0f} budget is already spent; only PR states were refreshed"
        elif hk["free_gb"] < float(config.setting("MIN_FREE_GB")):
            skipped = f"only {hk['free_gb']} GB free on disk (need {config.setting('MIN_FREE_GB')}); nothing was cloned or built"
        else:
            from .opener import open_quota, opened_on
            target = int(config.setting("DAILY_TARGET_PRS"))
            if budget <= 1:
                log.warn(STAGE, "regular budget spent but no PR opened today; going straight to rescue")
            # 2. already-built changes first: opening them costs almost nothing
            if not dry and config.setting("AUTO_OPEN") and len(opened_on(day)) < target:
                opened += open_quota(target - len(opened_on(day)))
            repos = pick_repos(int(config.setting("DAILY_MAX_REPOS"))) if budget > 1 else []
            log.info(STAGE, f"repos picked: {repos}")
            if not dry and repos:
                focus = (PROJECT / "prompts" / "focus_daily.md").read_text()
                scouted = scout(repos, focus)
                spent = sum(s.get("cost", 0) for s in scouted.values())
                # 4. build the best candidates, opening each one that passes the gates
                built = build(candidates(repos), budget - spent, int(config.setting("DAILY_MAX_BUILDS")),
                              float(config.setting("MIN_FREE_GB")), open_left=max(0, target - len(opened_on(day))))
            # 4b. quota rescue: nothing opened yet today -> extra budget, more repos, keep building
            if not dry and config.setting("AUTO_OPEN"):
                rescue_used = float(state.setdefault("rescue", {}).get(day, 0))
                rounds = 0
                while (len(opened_on(day)) < int(config.setting("DAILY_MIN_PRS")) and rounds < int(config.setting("DAILY_RESCUE_ROUNDS"))
                       and rescue_used < float(config.setting("DAILY_RESCUE_USD"))):
                    rounds += 1
                    log.warn(STAGE, f"no PR opened yet today; rescue round {rounds} (rescue spend so far ${rescue_used:.1f})")
                    more = pick_repos(int(config.setting("DAILY_MAX_REPOS")))
                    sc = scout(more, (PROJECT / "prompts" / "focus_daily.md").read_text()) if more else {}
                    scouted.update(sc)
                    tried = {b.get("opportunity") for b in built} | {b.get("change") for b in built}
                    cands = [o for o in candidates(list(more)) if o["id"] not in tried]
                    left = float(config.setting("DAILY_RESCUE_USD")) - rescue_used - sum(x.get("cost", 0) for x in sc.values())
                    extra = build(cands, left, int(config.setting("DAILY_RESCUE_BUILDS")), float(config.setting("MIN_FREE_GB")), open_left=1)
                    built += extra
                    rescue_used += sum(x.get("cost", 0) for x in sc.values()) + sum(b.get("cost", 0) for b in extra)
                    state["rescue"][day] = round(rescue_used, 2)
                    if not extra and not sc:
                        break
        # 5. refresh discovery every few days, now that the paid work is done
        if not dry and t_start - state.get("last_scan", 0) > float(config.setting("DAILY_RESCAN_DAYS")) * 86400:
            try:
                from .scanner import run_scan
                run_scan(config.setting("DAILY_DOMAINS"), use_search=True)
                state["last_scan"] = t_start
            except Exception as e:
                log.warn(STAGE, f"scan failed: {e}")
        # 6. tidy again (rejected builds leave artifacts) and report
        cleanup(dry_run=dry)
        disk = disk_report()
        spent = sum(s.get("cost", 0) for s in scouted.values()) + sum(b.get("cost", 0) for b in built)
        ledger[day] = round(ledger.get(day, 0) + spent, 2)
        month_spent = sum(v for d, v in ledger.items() if d >= time.strftime("%Y-%m-%d", time.gmtime(t_start - 30 * 86400)))
        try:
            from .opener import opened_on
            today_prs = [] if dry else opened_on(day)
        except Exception:
            today_prs = []
        digest = compose_digest(day, prs, scouted, built, disk, spent, skipped, month_spent, opened, today_prs)
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / f"DAILY-{day}.md").write_text(digest)
        try:
            subprocess.run([sys.executable, str(PROJECT / "scripts" / "gen_playbook.py")], cwd=PROJECT, capture_output=True, timeout=300, env={**os.environ, "PYTHONPATH": str(PROJECT)})
        except Exception:
            pass
        n_ready = len(today_prs)
        sent = False if dry else send_digest(f"[oss-contrib] {day} {'am' if time.localtime().tm_hour < 12 else 'pm'}: {len(prs['merged'])} merged, {len(prs.get('good', []))} approved/progress, {len(prs['activity'])} need reply, {n_ready} opened today", digest)
        state["last_run"] = t_start
        state_f.write_text(json.dumps(state))
        log.info(STAGE, f"done in {round(time.time()-t_start)}s; digest {'sent' if sent else 'on disk'}; spend ${spent:.1f}")
        return {"scouted": scouted, "built": built, "prs": prs, "spent": spent, "sent": sent, "skipped": skipped}
    except Exception:
        log.error(STAGE, "daily run crashed:\n" + traceback.format_exc())
        send_digest(f"[oss-contrib] {day}: run crashed", traceback.format_exc())
        raise
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    print(json.dumps(run(dry="--dry" in sys.argv), indent=1, default=str)[:6000])
