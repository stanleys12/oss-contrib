"""Unified logging: stdout + events table (which the UI tails over SSE)."""
from __future__ import annotations

import sys
import threading
import time

from . import db

_ctx = threading.local()


def set_job(job_id: int | None):
    _ctx.job_id = job_id


def _job():
    return getattr(_ctx, "job_id", None)


def emit(level: str, stage: str, message: str, repo: str | None = None, data: dict | None = None):
    ts = time.strftime("%H:%M:%S")
    tag = f"[{stage}]" + (f"[{repo}]" if repo else "")
    print(f"{ts} {level.upper():5} {tag} {message}", file=sys.stderr, flush=True)
    try:
        db.log_event(level, stage, message, repo, data, _job())
    except Exception as e:  # never let logging kill a stage
        print(f"(event log failed: {e})", file=sys.stderr)


def info(stage, message, repo=None, data=None):
    emit("info", stage, message, repo, data)


def warn(stage, message, repo=None, data=None):
    emit("warn", stage, message, repo, data)


def error(stage, message, repo=None, data=None):
    emit("error", stage, message, repo, data)


def ok(stage, message, repo=None, data=None):
    emit("ok", stage, message, repo, data)
