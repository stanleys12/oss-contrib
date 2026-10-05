"""FastAPI backend: REST for the dashboard + SSE event stream + background worker."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db
from .analyzer import osc_dir
from .pipeline import Worker, enqueue, cancel
from .scanner import rank

app = FastAPI(title="osc — open source contribution engine")
worker = Worker()


@app.on_event("startup")
def _start():
    db.init()
    worker.start()
    from .today import start_poller
    start_poller()


def _jf(rows, fields):
    return [db.parse_json_fields(r, fields) for r in rows]


REPO_JSON = ["domains", "languages", "topics", "score_parts", "raw"]
OPP_JSON = ["evidence", "related_issues", "related_prs", "raw"]
CHG_JSON = ["files_changed", "diff_stats", "tests_run", "review", "quality", "handoff", "compliance"]


@app.get("/")
def index():
    return FileResponse(config.UI_DIR / "index.html")



def _ready_split() -> dict:
    """Built-and-pushed changes split into ones that can be opened now vs ones blocked (CLA, Gerrit, hold, ...)."""
    from .opener import blocker, _open_repos
    orepos = _open_repos()
    rows = db.rows("SELECT id, repo, status, status_note, review_verdict, review_score FROM changes WHERE status IN ('ready','prepared')")
    blocked = sum(1 for c in rows if blocker(c, orepos))
    return {"changes_ready": len(rows) - blocked, "changes_blocked": blocked}


@app.get("/api/overview")
def overview():
    counts = {
        "repos": db.row("SELECT COUNT(*) c FROM repos")["c"],
        "repos_analyzed": db.row("SELECT COUNT(*) c FROM repos WHERE status='analyzed'")["c"],
        "opportunities": db.row("SELECT COUNT(*) c FROM opportunities")["c"],
        "opps_proposed": db.row("SELECT COUNT(*) c FROM opportunities WHERE status='proposed'")["c"],
        "changes": db.row("SELECT COUNT(*) c FROM changes")["c"],
        **_ready_split(),
        "changes_submitted": db.row("SELECT COUNT(*) c FROM changes WHERE status='submitted'")["c"],
        "cost_usd": round(db.row("SELECT COALESCE(SUM(cost_usd),0) c FROM changes")["c"], 2),
    }
    by_domain = db.rows("SELECT domain, COUNT(*) n, ROUND(AVG(score),3) avg_score FROM repos GROUP BY domain ORDER BY n DESC")
    current = db.row("SELECT * FROM jobs WHERE status='running' ORDER BY id DESC LIMIT 1")
    queued = db.rows("SELECT id, kind, target, created_at FROM jobs WHERE status='queued' ORDER BY id")
    recent_jobs = db.rows("SELECT id, kind, target, status, created_at, started_at, finished_at, error FROM jobs ORDER BY id DESC LIMIT 12")
    return {"counts": counts, "by_domain": by_domain, "current_job": current, "queued": queued, "recent_jobs": recent_jobs,
            "last_scan": db.kv_get("last_scan"), "worker_alive": worker.is_alive(), "settings": config.all_settings()}


@app.get("/api/today")
def today():
    from .today import summary
    return summary()


@app.get("/api/badges")
def badges_get():
    from .badges import get
    return get()


@app.post("/api/badges/refresh")
def badges_refresh():
    from .badges import get
    return get(refresh=True)


@app.post("/api/today/poll")
def today_poll():
    from .today import poll, summary
    poll()
    return summary()


@app.get("/api/repos")
def repos(limit: int = 300, domain: str | None = None, status: str | None = None, q: str | None = None):
    rs = rank(limit=limit, domain=domain)
    if status:
        rs = [r for r in rs if r["status"] == status]
    if q:
        ql = q.lower()
        rs = [r for r in rs if ql in r["full_name"].lower() or ql in (r.get("description") or "").lower()]
    for r in rs:
        raw = r.pop("raw", None) or {}
        r["raw"] = {"ai_prohibited": raw.get("ai_prohibited"), "ai_policy": raw.get("ai_policy")}
        r.pop("contributing_excerpt", None)
    return rs


@app.get("/api/repos/{owner}/{name}")
def repo_detail(owner: str, name: str):
    full = f"{owner}/{name}"
    r = db.row("SELECT * FROM repos WHERE full_name=?", (full,))
    if not r:
        raise HTTPException(404)
    r = db.parse_json_fields(r, REPO_JSON)
    issues = db.rows("SELECT * FROM issues WHERE repo=? ORDER BY claimable DESC, triage_score DESC LIMIT 60", (full,))
    opps = _jf(db.rows("SELECT * FROM opportunities WHERE repo=? ORDER BY priority DESC", (full,)), OPP_JSON)
    changes = db.rows("SELECT id, opportunity_id, branch, status, review_score, review_verdict, pr_title, diff_stats, cost_usd, created_at FROM changes WHERE repo=? ORDER BY created_at DESC", (full,))
    out = osc_dir(full)
    signals, summary = None, None
    for fn, var in (("signals.json", "signals"), ("analysis_summary.json", "summary")):
        p = out / fn
        if p.exists():
            try:
                val = json.loads(p.read_text())
                if var == "signals":
                    signals = val
                else:
                    summary = val
            except Exception:
                pass
    return {"repo": r, "issues": _jf(issues, ["labels"]), "opportunities": opps, "changes": _jf(changes, ["diff_stats"]),
            "signals": signals, "analysis_summary": summary}


@app.get("/api/opportunities")
def opportunities(repo: str | None = None, status: str | None = None):
    sql, params = "SELECT * FROM opportunities", []
    where = []
    if repo:
        where.append("repo=?"); params.append(repo)
    if status:
        where.append("status=?"); params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = _jf(db.rows(sql + " ORDER BY priority DESC, created_at DESC", params), OPP_JSON)
    for o in rows:
        o.pop("raw", None)
        rp = db.row("SELECT stars, domain, language FROM repos WHERE full_name=?", (o["repo"],))
        o["repo_meta"] = rp
    return rows


@app.get("/api/opportunities/{oid}")
def opportunity(oid: str):
    o = db.row("SELECT * FROM opportunities WHERE id=?", (oid,))
    if not o:
        raise HTTPException(404)
    o = db.parse_json_fields(o, OPP_JSON)
    o["changes"] = _jf(db.rows("SELECT id, branch, status, review_score, review_verdict, diff_stats, cost_usd, created_at FROM changes WHERE opportunity_id=? ORDER BY created_at DESC", (oid,)), ["diff_stats"])
    return o


class StatusPatch(BaseModel):
    status: str
    note: str | None = None
    pr_url: str | None = None


@app.post("/api/opportunities/{oid}/status")
def opp_status(oid: str, p: StatusPatch):
    db.update("opportunities", "id", oid, {"status": p.status, "status_note": p.note, "updated_at": time.time()})
    return {"ok": True}


@app.get("/api/changes")
def changes(status: str | None = None):
    sql, params = "SELECT id, opportunity_id, repo, branch, pr_title, status, status_note, review_score, review_verdict, review_round, diff_stats, cost_usd, model, fork_url, pr_url, created_at, updated_at, json_extract(quality,'$.score') AS quality_score FROM changes", []
    if status:
        sql += " WHERE status=?"; params.append(status)
    rows = _jf(db.rows(sql + " ORDER BY created_at DESC", params), ["diff_stats"])
    from .opener import blocker, _open_repos
    orepos = _open_repos()
    for c in rows:
        o = db.row("SELECT kind, title FROM opportunities WHERE id=?", (c["opportunity_id"],))
        c["opportunity"] = o
        c["blocker"] = blocker(c, orepos)
    return rows


@app.get("/api/changes/{cid}")
def change(cid: str):
    c = db.row("SELECT * FROM changes WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404)
    c = db.parse_json_fields(c, CHG_JSON)
    c["opportunity"] = db.parse_json_fields(db.row("SELECT * FROM opportunities WHERE id=?", (c["opportunity_id"],)), OPP_JSON)
    c["repo_meta"] = db.parse_json_fields(db.row("SELECT full_name, stars, domain, language, cla_required, dco_required, has_contributing, url, raw FROM repos WHERE full_name=?", (c["repo"],)), ["raw"])
    out = osc_dir(c["repo"]) / cid
    c["artifacts"] = sorted(p.name for p in out.glob("*")) if out.exists() else []
    return c


@app.post("/api/changes/{cid}/status")
def change_status(cid: str, p: StatusPatch):
    patch = {"status": p.status, "status_note": p.note, "updated_at": time.time()}
    if p.pr_url:
        patch["pr_url"] = p.pr_url
    db.update("changes", "id", cid, patch)
    c = db.row("SELECT opportunity_id FROM changes WHERE id=?", (cid,))
    if c and p.status in ("submitted", "rejected", "merged"):
        db.update("opportunities", "id", c["opportunity_id"], {"status": p.status, "updated_at": time.time()})
    return {"ok": True}


class JobReq(BaseModel):
    kind: str
    target: str | None = None
    params: dict = {}


@app.post("/api/jobs")
def post_job(j: JobReq):
    if j.kind not in ("scan", "analyze", "analyze_top", "build", "auto", "prepare", "revise", "rereview", "comply"):
        raise HTTPException(400, "bad kind")
    if j.kind == "build":
        db.update("opportunities", "id", j.target, {"status": "queued", "updated_at": time.time()})
    return {"id": enqueue(j.kind, j.target, j.params)}


@app.get("/api/jobs")
def jobs(limit: int = 50):
    return db.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))


@app.post("/api/jobs/{jid}/cancel")
def job_cancel(jid: int):
    j = db.row("SELECT status FROM jobs WHERE id=?", (jid,))
    if not j:
        raise HTTPException(404)
    if j["status"] != "queued":
        raise HTTPException(400, "only queued jobs can be cancelled (running jobs finish their current step)")
    cancel(jid)
    return {"ok": True}


@app.get("/api/events")
def events(since: int = 0, limit: int = 300, stage: str | None = None, repo: str | None = None):
    sql, params = "SELECT * FROM events WHERE id > ?", [since]
    if stage:
        sql += " AND stage=?"; params.append(stage)
    if repo:
        sql += " AND repo=?"; params.append(repo)
    rows = db.rows(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
    return _jf(rows[::-1], ["data"])


@app.get("/api/events/stream")
async def stream(since: int = 0):
    async def gen():
        last = since or (db.row("SELECT COALESCE(MAX(id),0) m FROM events")["m"])
        yield "event: hello\ndata: {}\n\n"
        while True:
            rows = db.rows("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT 200", (last,))
            for r in rows:
                last = r["id"]
                yield f"data: {json.dumps(db.parse_json_fields(r, ['data']))}\n\n"
            if not rows:
                yield ": ping\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/playbook")
def playbook():
    """Everything the human needs to submit each ready/prepared change, in order of merge likelihood."""
    rows = _jf(db.rows("SELECT id, repo, branch, pr_title, pr_body, status, status_note, review_score, review_verdict, quality, handoff, fork_url, pr_url, head_sha, opportunity_id, json_extract(compliance,'$.merge_likelihood') AS merge_likelihood, json_extract(compliance,'$.fails') AS compliance_fails FROM changes WHERE status IN ('ready','prepared','submitted','needs_work') ORDER BY CASE status WHEN 'prepared' THEN 0 WHEN 'ready' THEN 1 WHEN 'needs_work' THEN 2 ELSE 3 END, review_score DESC"), ["quality", "handoff"])
    out = []
    for c in rows:
        repo = db.parse_json_fields(db.row("SELECT full_name, stars, domain, cla_required, dco_required, url, raw FROM repos WHERE full_name=?", (c["repo"],)), ["raw"]) or {}
        opp = db.parse_json_fields(db.row("SELECT title, kind, related_issues FROM opportunities WHERE id=?", (c["opportunity_id"],)), ["related_issues"]) or {}
        c["repo_meta"] = {k: repo.get(k) for k in ("stars", "domain", "cla_required", "dco_required", "url")}
        c["ai_policy"] = (repo.get("raw") or {}).get("ai_policy")
        c["ai_prohibited"] = (repo.get("raw") or {}).get("ai_prohibited")
        c["ai_prose_human"] = (repo.get("raw") or {}).get("ai_prose_human")
        c["default_branch"] = (repo.get("raw") or {}).get("default_branch") or "main"
        c["opportunity"] = opp
        c["quality_score"] = (c.get("quality") or {}).get("score")
        c.pop("quality", None)
        out.append(c)
    return out


@app.get("/api/settings")
def get_settings():
    return {"settings": config.all_settings(), "domains": {k: {"label": v["label"], "seeds": len(v["seeds"])} for k, v in config.DOMAINS.items()}}


@app.post("/api/settings")
def set_settings(patch: dict):
    return {"settings": config.save_settings(patch)}


@app.get("/api/artifact/{owner}/{name}/{cid}/{fname}")
def artifact(owner: str, name: str, cid: str, fname: str):
    p = osc_dir(f"{owner}/{name}") / cid / fname
    if not p.exists() or ".." in fname:
        raise HTTPException(404)
    return FileResponse(p)


app.mount("/ui", StaticFiles(directory=str(config.UI_DIR)), name="ui")
