"""Help-wanted lane (user idea 2026-10-06): projects anywhere on GitHub that ask for outside help.

Searches open, unassigned issues labeled "help wanted" / "contributions welcome" / "PRs welcome" that are not
stale and not crowded, across the languages we build in. Repos with several such issues, enough stars and
real activity are scored like any other repo (scanner.enrich_and_store) and tagged raw.help_wanted, so
daily.pick_repos can reserve DAILY_HELPWANTED_REPOS slots for them. The issue-level checks (claims, linked
PRs, age) still run in prefilter before anything is scouted or built.

  python -m osc.helpwanted          # refresh the list (about 40 search calls, a few minutes)
  python -m osc.helpwanted --show   # print the current list
"""
from __future__ import annotations

import collections
import json
import sys
import time

from . import config, db, log
from .gh import GitHub

STAGE = "helpwanted"
LABELS = ('"help wanted"', '"contributions welcome"', '"PRs welcome"', '"status: help wanted"', '"up-for-grabs"')
LANGS = ("Python", "TypeScript", "Go", "Rust", "C++", "C", "JavaScript")


def _iso(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


def search(gh: GitHub) -> dict[str, int]:
    """repo -> number of fresh, unassigned help-wanted issues found."""
    counts: collections.Counter = collections.Counter()
    for label in LABELS:
        for lang in LANGS:
            q = (f"label:{label} is:issue is:open no:assignee archived:false language:{lang} "
                 f"comments:<8 created:>={_iso(150)} updated:>={_iso(45)}")
            try:
                for it in gh.search_issues(q, per_page=100):
                    repo = it["repository_url"].split("/repos/", 1)[1]
                    counts[repo] += 1
            except Exception as e:
                log.warn(STAGE, f"search failed [{label} {lang}]: {str(e)[:120]}")
            if not getattr(gh, "last_was_cache", False):
                time.sleep(2.2)            # search API: 30 requests a minute
    return dict(counts)


def refresh() -> dict:
    from .scanner import enrich_and_store
    db.init()
    gh = GitHub()
    counts = search(gh)
    keep = {r: n for r, n in counts.items() if n >= int(config.setting("HELPWANTED_MIN_ISSUES"))}
    log.info(STAGE, f"{len(counts)} repos with fresh help-wanted issues; {len(keep)} with at least {config.setting('HELPWANTED_MIN_ISSUES')}")
    found = {r.lower(): {"full_name": r, "domains": {"help-wanted"}, "seed_domains": set(), "source": "help-wanted"} for r in keep}
    enrich_and_store(gh, found)
    tagged = 0
    for r, n in keep.items():
        row = db.parse_json_fields(db.row("SELECT full_name, raw, stars FROM repos WHERE lower(full_name)=lower(?)", (r,)), ["raw"])
        if not row:
            continue
        raw = row["raw"] if isinstance(row["raw"], dict) else {}
        raw["help_wanted"] = {"issues": n, "ts": time.time()}
        db.update("repos", "full_name", row["full_name"], {"raw": raw})
        tagged += 1
    db.kv_set("helpwanted_last", {"ts": time.time(), "repos": tagged})
    log.ok(STAGE, f"tagged {tagged} help-wanted repos")
    return {"repos_with_issues": len(counts), "kept": len(keep), "tagged": tagged}


def ranked(max_age_days: float = 7) -> list[dict]:
    """Tagged repos, most help requests first, with the repo's own score as the tie-breaker."""
    out = []
    for r in db.rows("SELECT full_name, stars, score, raw, archived, status, analyzed_at, commits_90d, ext_merge_ratio FROM repos WHERE raw LIKE '%help_wanted%'"):
        raw = json.loads(r["raw"] or "{}")
        hw = raw.get("help_wanted") or {}
        if time.time() - hw.get("ts", 0) > max_age_days * 86400:
            continue
        if (r["stars"] or 0) < float(config.setting("STRONG_MIN_STARS")) or r["archived"]:
            continue                        # same bar as auto-open; tiny repos with many self-filed issues are noise
        out.append({**r, "raw": raw, "hw_issues": hw.get("issues", 0)})
    return sorted(out, key=lambda r: (r["hw_issues"], r["score"] or 0), reverse=True)


if __name__ == "__main__":
    if "--show" in sys.argv:
        for r in ranked()[:40]:
            print(f"{r['hw_issues']:3d}  {r['stars'] or 0:7d}*  score {r['score'] or 0:.2f}  {r['full_name']}")
    else:
        print(json.dumps(refresh(), indent=1))
