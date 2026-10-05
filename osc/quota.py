"""Quota keeper: at least DAILY_MIN_PRS new PRs every day (user requirement 2026-10-03, standing for months).

launchd runs this hourly. If a PR was already opened today it exits in a second. Otherwise it keeps going,
cheapest first, until one opens or the day's rescue money (DAILY_RESCUE_USD) runs out:

1. open anything already built that passes the gates;
2. give near-miss builds (reviewer 6+/10, openable repo, not on hold) another revision round;
3. scout fresh repos and build until one passes; when the picker runs dry, look at repos scouted
   a few days ago again and refresh discovery.

Shares the daily run's lock and budget ledger, so it never overlaps the 03:30 / 13:30 runs.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback

from . import config, db, log
from .daily import DENY, LOCK, PROJECT, _pid_alive, _not_openable, build, candidates, needs_gpu, pick_repos, scout, send_digest
from .housekeeping import apply_isolation, cleanup

STAGE = "quota"
STATE_F = PROJECT / "data" / "daily_state.json"
LAST_HOUR = 24          # user 10-03: time is not a constraint, run around the clock
FIRST_HOUR = 0          # the shared lock keeps it from overlapping the 03:30 / 13:30 runs


def _state() -> dict:
    return json.loads(STATE_F.read_text()) if STATE_F.exists() else {}


def _charge(day: str, usd: float) -> float:
    """Record spend in the shared ledger; returns today's rescue spend."""
    st = _state()
    st.setdefault("ledger", {})[day] = round(float(st["ledger"].get(day, 0)) + usd, 2)
    st.setdefault("rescue", {})[day] = round(float(st["rescue"].get(day, 0)) + usd, 2)
    STATE_F.write_text(json.dumps(st))
    return st["rescue"][day]


def _left(day: str) -> float:
    return float(config.setting("DAILY_RESCUE_USD")) - float(_state().get("rescue", {}).get(day, 0))


def _met(day: str) -> list[str]:
    """Non-empty once today's target (DAILY_TARGET_PRS, user 10-03: as many as possible) is open."""
    from .opener import opened_on
    got = opened_on(day)
    return got if len(got) >= int(config.setting("DAILY_TARGET_PRS")) else []


def salvage(day: str, limit: int = 3) -> str | None:
    """One more revision round (opus) on recent near-misses in openable repos."""
    from .contributor import revise_change, prepare_submission
    from .compliance import check_change
    from .opener import open_quota, blocker
    rows = db.rows("SELECT * FROM changes WHERE status='needs_work' AND created_at>? AND review_score>=6 "
                   "ORDER BY review_score DESC", (time.time() - 5 * 86400,))
    tried = 0
    for c in rows:
        if tried >= limit or _left(day) <= 2 or _met(day):
            break
        repo = db.row("SELECT * FROM repos WHERE full_name=?", (c["repo"],)) or {}
        if c["repo"] in DENY or needs_gpu(repo) or _not_openable({"repo": c["repo"]}) or int(c.get("review_round") or 0) >= 5:
            continue
        note = c.get("status_note") or ""
        if "HOLD" in note or "BLOCKED" in note or "too small" in note:
            continue
        if blocker({**c, "status": "ready", "review_verdict": "approve", "review_score": 10}):
            continue   # blocked for a reason a revision can't fix (open PR in repo, CLA, ...)
        tried += 1
        log.info(STAGE, f"salvage {c['id']} {c['repo']} (review {c['review_score']})")
        try:
            r = revise_change(c["id"], model=config.setting("BUILDER_MODEL"))
            cost = float(r.get("cost") or 0)
            if r.get("status") == "ready":
                prepare_submission(c["id"])
                comp = check_change(c["id"], agent=True)
                cost += float(comp.get("cost_usd") or 0)
                _charge(day, cost)
                att = open_quota(1, only=[c["id"]])
                if att and att[0].get("opened"):
                    return att[0]["url"]
                log.info(STAGE, f"salvaged {c['id']} but not opened: {att[0].get('why') if att else 'blocked'}")
            else:
                _charge(day, cost)
        except Exception as e:
            log.warn(STAGE, f"salvage {c['id']} failed: {e}")
    return None


def hunt(day: str) -> str | None:
    """Scout + build rounds until something opens, money runs out, or the day ends."""
    focus = (PROJECT / "prompts" / "focus_daily.md").read_text()
    widened = rescanned = False
    tried: set[str] = set()
    start_day = day
    while not _met(day) and _left(day) > 3 and time.strftime("%Y-%m-%d") == start_day:
        repos = pick_repos(int(config.setting("DAILY_MAX_REPOS")), reanalyze_days=3 if widened else None)
        sc = scout(repos, focus) if repos else {}
        _charge(day, sum(x.get("cost", 0) for x in sc.values()))
        cands = [o for o in candidates(repos) if o["id"] not in tried]
        if not cands:
            if not widened:
                widened = True
                log.info(STAGE, "picker dry; widening to repos scouted 3+ days ago")
                continue
            if not rescanned:
                rescanned = True
                log.info(STAGE, "still dry; refreshing discovery")
                try:
                    from .scanner import run_scan
                    run_scan(config.setting("DAILY_DOMAINS"), use_search=True)
                except Exception as e:
                    log.warn(STAGE, f"scan failed: {e}")
                continue
            log.warn(STAGE, "no candidates left today")
            return None
        tried |= {o["id"] for o in cands[:3]}
        out = build(cands, _left(day), 3, float(config.setting("MIN_FREE_GB")), open_left=1)
        _charge(day, sum(b.get("cost", 0) for b in out))
        for b in out:
            if b.get("opened"):
                return b["opened"]
    return None


def run() -> dict:
    db.init()
    apply_isolation()
    day = time.strftime("%Y-%m-%d")
    if not config.setting("AUTO_OPEN"):
        return {"skipped": "AUTO_OPEN off"}
    got = _met(day)
    if got:
        return {"met": got}
    if time.localtime().tm_hour >= LAST_HOUR:
        return {"skipped": "too late today"}
    if time.localtime().tm_hour < FIRST_HOUR and "--now" not in sys.argv:
        return {"skipped": "daily runs go first"}
    if LOCK.exists():
        try:
            other = int(LOCK.read_text().strip() or 0)
        except ValueError:
            other = 0
        if other > 0 and _pid_alive(other) and time.time() - LOCK.stat().st_mtime < 6 * 3600:
            log.info(STAGE, f"daily/quota run already active (pid {other}); next hour")
            return {"skipped": "lock"}
        LOCK.unlink(missing_ok=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    t0 = time.time()
    url = None
    try:
        log.warn(STAGE, f"no PR opened yet on {day}; quota hunt (rescue money left ${_left(day):.0f})")
        from .opener import open_quota
        from .opener import opened_on
        att = open_quota(max(1, int(config.setting("DAILY_TARGET_PRS")) - len(opened_on(day))))
        url = next((a["url"] for a in att if a.get("opened")), None)
        url = url or salvage(day)
        url = url or hunt(day)
        cleanup(dry_run=False)
        spent = float(_state().get("rescue", {}).get(day, 0))
        if url:
            log.ok(STAGE, f"quota met: {url}")
            send_digest(f"[oss-contrib] {day}: opened {url.split('github.com/')[-1]}",
                        f"Today's PR is up: {url}\n\nRescue spend today: ${spent:.1f}\n")
        else:
            log.warn(STAGE, f"still no PR after {round(time.time()-t0)}s; rescue spend ${spent:.1f}")
            from .opener import opened_on
            if not opened_on(day) and (_left(day) <= 3 or time.localtime().tm_hour >= 23):
                send_digest(f"[oss-contrib] {day}: no PR today",
                            f"Ran out of {'money' if _left(day) <= 3 else 'time'} without a PR that passes the gates.\n"
                            f"Rescue spend ${spent:.1f}. See {PROJECT}/logs/daily-{day}.log\n")
        return {"opened": url}
    except Exception:
        log.error(STAGE, "quota run crashed:\n" + traceback.format_exc())
        raise
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    print(json.dumps(run(), default=str))
