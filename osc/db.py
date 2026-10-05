"""SQLite state store. One connection per call (thread-safe enough for our worker + web server)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  full_name TEXT PRIMARY KEY,
  owner TEXT, name TEXT, url TEXT, description TEXT,
  domain TEXT, domains TEXT,            -- primary domain, json list of all matched
  source TEXT,                          -- seed | topic-search | query-search
  stars INTEGER, forks INTEGER, language TEXT, languages TEXT, license TEXT, topics TEXT,
  pushed_at TEXT, created_at TEXT, disk_kb INTEGER, archived INTEGER,
  open_issues INTEGER, gfi_issues INTEGER, hw_issues INTEGER, bug_issues INTEGER, open_prs INTEGER,
  commits_90d INTEGER,
  ext_merge_ratio REAL, median_merge_days REAL, ext_pr_sample INTEGER,
  has_contributing INTEGER, contributing_excerpt TEXT, cla_required INTEGER, dco_required INTEGER,
  score REAL, score_parts TEXT,
  status TEXT DEFAULT 'candidate',      -- candidate | selected | analyzing | analyzed | skipped | error
  status_note TEXT,
  scanned_at REAL, analyzed_at REAL,
  raw TEXT
);
CREATE TABLE IF NOT EXISTS issues (
  id TEXT PRIMARY KEY,                  -- repo#number
  repo TEXT, number INTEGER, title TEXT, url TEXT, labels TEXT, state TEXT,
  created_at TEXT, updated_at TEXT, comments INTEGER, reactions INTEGER,
  assignees INTEGER, linked_prs INTEGER, body_excerpt TEXT, author_assoc TEXT,
  claimable INTEGER, triage_score REAL, fetched_at REAL
);
CREATE TABLE IF NOT EXISTS opportunities (
  id TEXT PRIMARY KEY,
  repo TEXT, kind TEXT, title TEXT, summary TEXT, rationale TEXT, approach TEXT,
  evidence TEXT, related_issues TEXT, related_prs TEXT,
  scope TEXT, risk TEXT, confidence REAL, accept_likelihood REAL, priority REAL,
  duplicate_check TEXT, tests_plan TEXT,
  status TEXT DEFAULT 'proposed',       -- proposed | queued | building | built | rejected | needs_work | ready | prepared | submitted
  status_note TEXT, created_at REAL, updated_at REAL, raw TEXT
);
CREATE TABLE IF NOT EXISTS changes (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT, repo TEXT, branch TEXT, base_sha TEXT, head_sha TEXT,
  pr_title TEXT, pr_body TEXT, summary TEXT, why TEXT, files_changed TEXT,
  diff TEXT, diff_stats TEXT, tests_run TEXT, limitations TEXT,
  review TEXT, review_score REAL, review_verdict TEXT, review_round INTEGER, quality TEXT,
  builder_log TEXT, cost_usd REAL, model TEXT,
  status TEXT,                          -- building | built | reviewed | ready | needs_work | rejected | prepared | submitted
  status_note TEXT, fork_url TEXT, pr_url TEXT,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, target TEXT, params TEXT,
  status TEXT DEFAULT 'queued',         -- queued | running | done | failed | cancelled
  created_at REAL, started_at REAL, finished_at REAL, error TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, level TEXT, stage TEXT, repo TEXT, message TEXT, data TEXT, job_id INTEGER
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_issues_repo ON issues(repo);
CREATE INDEX IF NOT EXISTS idx_opps_repo ON opportunities(repo);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init():
    with tx() as con:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(changes)")}
        if "quality" not in cols:
            con.execute("ALTER TABLE changes ADD COLUMN quality TEXT")
        if "handoff" not in cols:
            con.execute("ALTER TABLE changes ADD COLUMN handoff TEXT")
        if "compliance" not in cols:
            con.execute("ALTER TABLE changes ADD COLUMN compliance TEXT")


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _j(v):
    if v is None or isinstance(v, (str, int, float)):
        return v
    return json.dumps(v, ensure_ascii=False)


def upsert(table: str, row: dict, key: str):
    row = {k: _j(v) for k, v in row.items()}
    cols = ", ".join(row)
    ph = ", ".join("?" for _ in row)
    upd = ", ".join(f"{k}=excluded.{k}" for k in row if k != key)
    with tx() as con:
        con.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT({key}) DO UPDATE SET {upd}", list(row.values()))


def update(table: str, key_col: str, key: str, patch: dict):
    patch = {k: _j(v) for k, v in patch.items()}
    if not patch:
        return
    sets = ", ".join(f"{k}=?" for k in patch)
    with tx() as con:
        con.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", [*patch.values(), key])


def rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, list(params)).fetchall()]
    finally:
        con.close()


def row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = rows(sql, params)
    return r[0] if r else None


def parse_json_fields(d: dict | None, fields: Iterable[str]) -> dict | None:
    if not d:
        return d
    for f in fields:
        v = d.get(f)
        if isinstance(v, str):
            try:
                d[f] = json.loads(v)
            except Exception:
                pass
    return d


def kv_get(k: str, default=None):
    r = row("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def kv_set(k: str, v):
    upsert("kv", {"k": k, "v": json.dumps(v)}, "k")


def log_event(level: str, stage: str, message: str, repo: str | None = None, data: dict | None = None,
              job_id: int | None = None):
    with tx() as con:
        con.execute("INSERT INTO events (ts, level, stage, repo, message, data, job_id) VALUES (?,?,?,?,?,?,?)",
                    (time.time(), level, stage, repo, message, json.dumps(data) if data else None, job_id))
