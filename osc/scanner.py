"""Stage 1 — discovery + scoring of candidate repositories.

Sources: curated seeds (config.DOMAINS), GitHub topic search, GitHub keyword search.
Every candidate is enriched with one GraphQL profile call and scored on 7 transparent
components stored in `score_parts` so the UI can show *why* a repo ranks where it does.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
import time

from . import config, db, log
from .gh import GitHub, repo_profile

STAGE = "scan"


def _iso_days_ago(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_since(iso: str | None) -> float:
    if not iso:
        return 9999
    t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


# ------------------------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------------------------
def discover(gh: GitHub, domains: list[str] | None = None, use_search: bool = True) -> dict[str, dict]:
    """Return {full_name: {"domains": set, "source": str}}."""
    min_stars = config.setting("MIN_STARS")
    pushed = _iso_days_ago(config.setting("PUSHED_WITHIN_DAYS"))[:10]
    per_query = config.setting("SEARCH_PER_QUERY")
    domains = domains or list(config.DOMAINS)
    found: dict[str, dict] = {}

    def add(full, domain, source):
        full = full.strip()
        key = full.lower()
        if key not in found:
            found[key] = {"full_name": full, "domains": set(), "seed_domains": set(), "source": source}
        found[key]["domains"].add(domain)
        if source == "seed":
            found[key]["seed_domains"].add(domain)

    seeds = config.domain_seeds()
    for d in domains:
        for s in seeds.get(d, []):
            add(s, d, "seed")
    log.info(STAGE, f"{len(found)} seeds loaded for domains {domains}")

    if use_search:
        for d in domains:
            spec = config.DOMAINS[d]
            queries = [f"topic:{t} stars:>={min_stars} pushed:>={pushed} archived:false" for t in spec["topics"]]
            queries += [f"{q} in:name,description,readme stars:>={min_stars} pushed:>={pushed} archived:false"
                        for q in config.SEARCH_QUERIES.get(d, [])]
            n0 = len(found)
            for q in queries:
                try:
                    for it in gh.search_repos(q, per_page=per_query, sort="stars"):
                        add(it["full_name"], d, "search")
                except Exception as e:
                    log.warn(STAGE, f"search failed [{q}]: {e}")
                if not getattr(gh, "last_was_cache", False):
                    time.sleep(2.1)
            log.info(STAGE, f"domain {d}: +{len(found) - n0} from {len(queries)} searches")
    return found


# ------------------------------------------------------------------------------------
# Enrichment + scoring
# ------------------------------------------------------------------------------------
def _domain_fit(profile: dict, domains: set[str], source: str, seed_domains: set[str] | None = None) -> tuple[float, str]:
    """Fit = seed membership OR topic/keyword overlap with the domain definitions.
    Seed membership boosts only the domain(s) the repo was curated under; ties go to the higher raw overlap."""
    if seed_domains is None:
        seed_domains = domains if source == "seed" else set()
    topics = {t["topic"]["name"].lower() for t in profile["repositoryTopics"]["nodes"]}
    text = f"{profile.get('description') or ''} {' '.join(topics)}".lower()
    best, best_d = 0.0, None
    for d, spec in config.DOMAINS.items():
        t_hit = len(topics & set(spec["topics"]))
        k_hit = sum(1 for k in spec["keywords"] if k in text)
        raw = min(1.0, 0.35 * t_hit + 0.15 * k_hit)
        fit = max(raw, 0.9 + 0.1 * raw) if d in seed_domains else raw
        if fit > best:
            best, best_d = fit, d
    if best_d is None:
        best_d = next(iter(domains)) if domains else "unknown"
    return best, best_d


def _pr_openness(nodes: list[dict]) -> tuple[float, float, int]:
    """External-contributor merge ratio + median days to merge, from last 60 closed/merged PRs."""
    ext = [n for n in nodes if n.get("authorAssociation") not in ("MEMBER", "OWNER", "COLLABORATOR")
           and n.get("author") and not (n["author"]["login"] or "").endswith("[bot]")]
    if not ext:
        return 0.0, 0.0, 0
    merged = [n for n in ext if n["state"] == "MERGED"]
    ratio = len(merged) / len(ext)
    days = []
    for n in merged:
        try:
            c = dt.datetime.strptime(n["createdAt"], "%Y-%m-%dT%H:%M:%SZ")
            m = dt.datetime.strptime(n["mergedAt"], "%Y-%m-%dT%H:%M:%SZ")
            days.append((m - c).total_seconds() / 86400)
        except Exception:
            pass
    return ratio, (statistics.median(days) if days else 0.0), len(ext)


def _contributing(profile: dict) -> tuple[bool, str, bool, bool]:
    text = ""
    for k in ("c1", "c2", "c3", "c4", "c5"):
        if profile.get(k) and profile[k].get("text"):
            text = profile[k]["text"]
            break
    low = text.lower()
    cla = bool(re.search(r"\bcla\b|contributor license agreement", low))
    dco = bool(re.search(r"\bdco\b|developer certificate of origin|signed-off-by|sign-off", low))
    return bool(text), text[:4000], cla, dco


AI_PROHIBIT = re.compile(r"(not written by an? (llm|ai|code agent|language model)|no (ai|llm)[- ](generated|written|assisted)|"
                         r"do not (submit|open|send) (ai|llm|agent)[- ]|(ai|llm|agent)[- ](generated|written|authored) (prs?|pull requests?|contributions?|code)[^.]{0,80}"
                         r"(not (allowed|accepted|permitted)|will (probably )?be closed|are (not welcome|prohibited|banned|rejected))|"
                         r"should not submit (ai|llm|agent)|we (do not|don't) accept (ai|llm)|pure code-agent prs? are not allowed|"
                         r"(will not|won't|do not|don't) review [^.]{0,40}(ai|llm|agent)[- ]generated[^.]{0,60}first[- ]time|"
                         r"paused accepting (pull requests|prs|contributions)|not (currently )?accepting (external|outside|community) (pull requests|prs|contributions)|"
                         r"(do not|don't|does not|doesn't) accept (external|outside|community|unsolicited)? ?(code )?(contributions|pull requests|prs)|"
                         r"unsolicited (prs?|pull requests?)[^.]{0,40}(will be|are|get) closed|(prs?|pull requests?) without (an )?(assigned|approved) issue[^.]{0,30}closed)", re.I)


AI_PROSE_HUMAN = re.compile(r"(prohibited|forbid|not allowed|never|must not|do not) [^.]{0,60}(ai|llm)[^.]{0,40}(write|written|generated)[^.]{0,60}(pull request descriptions?|pr descriptions?|descriptions?|posts|commit messages?|responses|replies)|"
                            r"(ai|llm)[- ](written|generated) (pr descriptions?|pull request descriptions?|commit messages?|reviewer responses?|responses|replies)|"
                            r"write (the )?(pr description|description|commit message)[^.]{0,40}(yourself|in your own words)", re.I)


def ai_prose_must_be_human(text: str) -> str:
    for sent in re.split(r"(?<=[.!?\n])\s+", text or ""):
        if AI_PROSE_HUMAN.search(sent):
            return sent.strip()[:300]
    return ""


def ai_prohibited(text: str) -> str:
    """Return the sentence that forbids AI-written PRs, or '' if none."""
    for sent in re.split(r"(?<=[.!?\n])\s+", text or ""):
        m = AI_PROHIBIT.search(sent)
        if not m:
            continue
        # "no fully autonomous agents" / "pure agent PRs" bans unattended agents, not AI assistance with a human
        if re.search(r"\b(fully|purely|entirely|pure|autonomous(ly)?|unattended|unsupervised|without (meaningful )?human)\b", sent[max(0, m.start() - 40):m.end() + 40], re.I):
            continue
        return sent.strip()[:300]
    return ""


def _ai_policy(text: str) -> str:
    """Pull sentences from CONTRIBUTING that talk about AI/LLM-generated contributions."""
    hits = []
    for sent in re.split(r"(?<=[.!?\n])\s+", text or ""):
        clean = re.sub(r"https?://\S+|\S+\.(ai|com|org|io)\b", " ", sent)   # URLs/domains must not count as "AI"
        if re.search(r"\b(ai|llms?|chatgpt|copilot|claude|generative|machine[- ]generated|ai[- ]generated|ai[- ]assisted|ai[- ]tools?)\b", clean, re.I) \
           and re.search(r"contribut|pull request|\bprs?\b|disclos|policy|allowed|accept|trailer|co-authored", clean, re.I):
            hits.append(sent.strip()[:300])
    return " … ".join(hits[:4])


def score_profile(profile: dict, domains: set[str], source: str, seed_domains: set[str] | None = None) -> tuple[float, dict, dict]:
    W = config.setting("SCORE_WEIGHTS")
    LW = config.setting("LANG_WEIGHTS")
    stars = profile["stargazerCount"]
    fit, primary = _domain_fit(profile, domains, source, seed_domains)
    popularity = max(0.0, min(1.0, (math.log10(max(stars, 1)) - 2.5) / 2.5))   # 316→0, 100k→1
    pushed_days = _days_since(profile.get("pushedAt"))
    commits_90d = (profile.get("defaultBranchRef") or {}).get("target", {}).get("history", {}).get("totalCount", 0)
    activity = (1.0 if pushed_days <= 7 else 0.7 if pushed_days <= 30 else 0.3 if pushed_days <= 90 else 0.0)
    activity = 0.6 * activity + 0.4 * min(1.0, commits_90d / 150)
    ratio, med_days, sample = _pr_openness(profile["recentPRs"]["nodes"])
    openness = ratio * (1.0 if sample >= 8 else 0.6) * (1.0 if med_days <= 14 else 0.8 if med_days <= 45 else 0.6)
    gfi = profile["gfi1"]["totalCount"] + profile["gfi2"]["totalCount"]
    hw = profile["hw1"]["totalCount"] + profile["hw2"]["totalCount"]
    has_c, c_excerpt, cla, dco = _contributing(profile)
    approach = 0.5 * min(1.0, (gfi + hw) / 12) + 0.3 * has_c + 0.2 * (1.0 if profile["hasIssuesEnabled"] else 0)
    lang = (profile.get("primaryLanguage") or {}).get("name") or ""
    language_fit = LW.get(lang, 0.3)
    open_prs = profile["openPRs"]["totalCount"]
    soft, hard = config.setting("MAX_OPEN_PRS_SOFT"), config.setting("MAX_OPEN_PRS_HARD")
    crowding = 1.0 if open_prs <= soft else max(0.0, 1 - (open_prs - soft) / (hard - soft))
    parts = {"domain_fit": round(fit, 3), "popularity": round(popularity, 3), "activity": round(activity, 3),
             "openness": round(openness, 3), "approachability": round(approach, 3),
             "language_fit": round(language_fit, 3), "crowding": round(crowding, 3)}
    total = sum(W[k] * parts[k] for k in W)
    if profile["isArchived"] or profile["isMirror"]:
        total *= 0.1
    # anti-noise: awesome-lists / docs repos, very young star-rockets, repos with no code language
    name_low = profile["nameWithOwner"].lower()
    topic_names = {t["topic"]["name"].lower() for t in profile["repositoryTopics"]["nodes"]}
    if "awesome" in name_low or topic_names & {"awesome", "awesome-list", "curated-list", "cheatsheet", "roadmap", "tutorial", "course"}:
        total *= 0.3
    if not lang or lang in ("Markdown", "HTML", "Jupyter Notebook", "TeX"):
        total *= 0.5
    age = _days_since(profile.get("createdAt"))
    if age < 180:
        total *= 0.5   # too young to be "well known"; also where star-farming shows up
    elif age < 365:
        total *= 0.75
    if commits_90d < 10:
        total *= 0.6
    if pushed_days > config.setting("PUSHED_WITHIN_DAYS"):
        total *= 0.5
    if stars < config.setting("MIN_STARS"):
        total *= 0.5
    langs = {e["node"]["name"]: e["size"] for e in profile["languages"]["edges"]}
    meta = {
        "primary": primary, "stars": stars, "pushed_days": round(pushed_days, 1), "commits_90d": commits_90d,
        "ext_merge_ratio": round(ratio, 3), "median_merge_days": round(med_days, 1), "ext_pr_sample": sample,
        "gfi": gfi, "hw": hw, "bugs": profile["bugs"]["totalCount"], "open_prs": open_prs,
        "open_issues": profile["openIssues"]["totalCount"], "language": lang, "languages": langs,
        "has_contributing": has_c, "contributing_excerpt": c_excerpt, "cla": cla, "dco": dco,
        "ai_policy": _ai_policy(next((profile[k]["text"] for k in ("c1", "c2", "c3", "c4", "c5") if profile.get(k) and profile[k].get("text")), "")),
        "pr_template": ((profile.get("prTemplate") or profile.get("prTemplate2") or {}).get("text") or "")[:3000],
    }
    return round(total, 4), parts, meta


def enrich_and_store(gh: GitHub, found: dict[str, dict], limit: int | None = None) -> int:
    since = _iso_days_ago(90)
    items = list(found.values())
    if limit:
        items = items[:limit]
    n_ok = 0
    fresh = time.time() - 2 * 86400
    for i, it in enumerate(items, 1):
        full = it["full_name"]
        prev = db.row("SELECT scanned_at FROM repos WHERE full_name=?", (full,))
        if prev and (prev["scanned_at"] or 0) > fresh:
            n_ok += 1
            continue    # scored within the last 2 days; skip the API calls
        try:
            p = repo_profile(gh, full, since)
        except Exception as e:  # one flaky repo must not abort the whole scan
            log.warn(STAGE, f"{full}: profile failed: {str(e)[:120]}", repo=full)
            continue
        if not p:
            log.warn(STAGE, f"{full}: not found / inaccessible", repo=full)
            continue
        score, parts, meta = score_profile(p, it["domains"], it["source"], it.get("seed_domains"))
        existing = db.parse_json_fields(db.row("SELECT status, analyzed_at, raw FROM repos WHERE full_name=?", (p["nameWithOwner"],)), ["raw"])
        kept_raw = {k: v for k, v in ((existing or {}).get("raw") or {}).items() if k in ("ai_policy", "has_agents_md")} if isinstance((existing or {}).get("raw"), dict) else {}
        row = {
            "full_name": p["nameWithOwner"], "owner": p["nameWithOwner"].split("/")[0],
            "name": p["nameWithOwner"].split("/")[1], "url": p["url"], "description": p.get("description"),
            "domain": meta["primary"], "domains": sorted(it["domains"]), "source": it["source"],
            "stars": meta["stars"], "forks": p["forkCount"], "language": meta["language"], "languages": meta["languages"],
            "license": (p.get("licenseInfo") or {}).get("spdxId"),
            "topics": [t["topic"]["name"] for t in p["repositoryTopics"]["nodes"]],
            "pushed_at": p["pushedAt"], "created_at": p["createdAt"], "disk_kb": p["diskUsage"],
            "archived": int(p["isArchived"]), "open_issues": meta["open_issues"], "gfi_issues": meta["gfi"],
            "hw_issues": meta["hw"], "bug_issues": meta["bugs"], "open_prs": meta["open_prs"],
            "commits_90d": meta["commits_90d"], "ext_merge_ratio": meta["ext_merge_ratio"],
            "median_merge_days": meta["median_merge_days"], "ext_pr_sample": meta["ext_pr_sample"],
            "has_contributing": int(meta["has_contributing"]), "contributing_excerpt": meta["contributing_excerpt"],
            "cla_required": int(meta["cla"]), "dco_required": int(meta["dco"]),
            "score": score, "score_parts": parts, "scanned_at": time.time(),
            "raw": {"pr_template": meta["pr_template"], "default_branch": (p.get("defaultBranchRef") or {}).get("name"),
                    "ai_policy": meta["ai_policy"], **kept_raw},   # analyzer-derived policy (AGENTS.md) wins over the CONTRIBUTING guess
        }
        if not existing:
            row["status"] = "candidate"
        db.upsert("repos", row, "full_name")
        n_ok += 1
        if i % 25 == 0:
            log.info(STAGE, f"enriched {i}/{len(items)} (api calls {gh.calls})")
    return n_ok


def run_scan(domains: list[str] | None = None, use_search: bool = True, limit: int | None = None) -> dict:
    db.init()
    gh = GitHub()
    t0 = time.time()
    log.info(STAGE, f"scan start: domains={domains or 'all'} search={use_search}")
    found = discover(gh, domains, use_search)
    log.info(STAGE, f"{len(found)} unique candidates discovered; enriching via GraphQL")
    n = enrich_and_store(gh, found, limit)
    top = db.rows("SELECT full_name, score, domain, stars FROM repos ORDER BY score DESC LIMIT 15")
    log.ok(STAGE, f"scan done: {n} repos scored in {time.time()-t0:.0f}s ({gh.calls} API calls)",
           data={"top": top})
    db.kv_set("last_scan", {"ts": time.time(), "n": n, "domains": domains})
    return {"scored": n, "top": top}


def rank(limit: int = 50, domain: str | None = None, min_score: float = 0.0) -> list[dict]:
    sql = "SELECT * FROM repos WHERE score >= ?"
    params: list = [min_score]
    if domain:
        sql += " AND domain=?"
        params += [domain]
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(limit)
    return [db.parse_json_fields(r, ["domains", "languages", "topics", "score_parts", "raw"]) for r in db.rows(sql, params)]
