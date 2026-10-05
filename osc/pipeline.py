"""Job queue + worker so the UI can enqueue stages and watch them run.

Job kinds:
  scan        params: {domains: [...], search: bool, limit: int|null}
  analyze     target: owner/name   params: {model}
  analyze_top params: {n: int, domain: str|null}   -> analyze the top-N un-analyzed repos
  build       target: opportunity id   params: {model, reviewer_model, rounds}
  auto        params: {n_repos, per_repo}  -> analyze top-N, then build best opportunity per repo
  prepare     target: change id
"""
from __future__ import annotations

import json
import threading
import time
import traceback

from . import config, db, log
from .analyzer import analyze_repo
from .contributor import build_opportunity, prepare_submission, revise_change, rereview_change
from .scanner import run_scan, rank


def enqueue(kind: str, target: str | None = None, params: dict | None = None) -> int:
    with db.tx() as con:
        cur = con.execute("INSERT INTO jobs (kind, target, params, status, created_at) VALUES (?,?,?,?,?)",
                          (kind, target, json.dumps(params or {}), "queued", time.time()))
        jid = cur.lastrowid
    log.info("queue", f"job #{jid} queued: {kind} {target or ''}")
    return jid


def cancel(job_id: int):
    db.update("jobs", "id", job_id, {"status": "cancelled", "finished_at": time.time()})


def pick_unanalyzed(n: int, domain: str | None = None) -> list[str]:
    cands = rank(limit=200, domain=domain)
    out = []
    for r in cands:
        if r["status"] in ("candidate", "selected", "error") and not r.get("archived"):
            out.append(r["full_name"])
        if len(out) >= n:
            break
    return out


def run_job(job: dict) -> dict:
    kind, target = job["kind"], job["target"]
    params = json.loads(job["params"] or "{}")
    if kind == "scan":
        return run_scan(params.get("domains"), params.get("search", True), params.get("limit"))
    if kind == "analyze":
        return analyze_repo(target, params.get("model"))
    if kind == "analyze_top":
        repos = pick_unanalyzed(int(params.get("n") or config.setting("TOP_N_TO_ANALYZE")), params.get("domain"))
        log.info("pipeline", f"analyze_top: {repos}")
        results = {}
        for r in repos:
            try:
                results[r] = analyze_repo(r, params.get("model"))
            except Exception as e:
                results[r] = {"error": str(e)[:300]}
        return results
    if kind == "build":
        return {k: v for k, v in build_opportunity(target, params.get("model"), params.get("reviewer_model"), params.get("rounds")).items() if k != "diff"}
    if kind == "auto":
        n_repos = int(params.get("n_repos") or 3)
        per_repo = int(params.get("per_repo") or 1)
        repos = pick_unanalyzed(n_repos, params.get("domain"))
        summary = {}
        for r in repos:
            try:
                analyze_repo(r)
            except Exception as e:
                summary[r] = {"analyze_error": str(e)[:300]}
                continue
            opps = db.rows("SELECT id, title, priority FROM opportunities WHERE repo=? AND status='proposed' ORDER BY priority DESC LIMIT ?", (r, per_repo))
            summary[r] = []
            for o in opps:
                try:
                    ch = build_opportunity(o["id"])
                    summary[r].append({"opportunity": o["title"], "change": ch["id"], "status": ch["status"]})
                except Exception as e:
                    summary[r].append({"opportunity": o["title"], "error": str(e)[:300]})
        return summary
    if kind == "prepare":
        return prepare_submission(target)
    if kind == "revise":
        return revise_change(target, params.get("model"))
    if kind == "rereview":
        return rereview_change(target)
    if kind == "comply":
        from .compliance import check_change
        r = check_change(target)
        return {"fails": r["fails"], "merge_likelihood": r.get("merge_likelihood")}
    raise RuntimeError(f"unknown job kind {kind}")


class Worker(threading.Thread):
    """Single worker: stages are heavy (clones, agents), so we run one job at a time."""

    def __init__(self):
        super().__init__(daemon=True, name="osc-worker")
        self.current: int | None = None
        self.stop_flag = False

    def run(self):
        db.init()
        # mark any jobs left 'running' by a previous process as failed
        for j in db.rows("SELECT id FROM jobs WHERE status='running'"):
            db.update("jobs", "id", j["id"], {"status": "failed", "error": "worker restarted", "finished_at": time.time()})
        while not self.stop_flag:
            job = db.row("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
            if not job:
                time.sleep(2)
                continue
            self.current = job["id"]
            db.update("jobs", "id", job["id"], {"status": "running", "started_at": time.time()})
            log.set_job(job["id"])
            log.info("queue", f"job #{job['id']} started: {job['kind']} {job['target'] or ''}")
            try:
                result = run_job(job)
                db.update("jobs", "id", job["id"], {"status": "done", "finished_at": time.time(), "result": json.dumps(result, default=str)[:20000]})
                log.ok("queue", f"job #{job['id']} done")
            except Exception as e:
                db.update("jobs", "id", job["id"], {"status": "failed", "finished_at": time.time(), "error": (str(e) + "\n" + traceback.format_exc())[-3000:]})
                log.error("queue", f"job #{job['id']} failed: {e}")
            finally:
                log.set_job(None)
                self.current = None
