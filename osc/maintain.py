"""Maintenance of the owner's own public repos (user requirement 2026-10-04).

One repo per run, the one maintained longest ago. An agent works in a separate clone (never in the live
project directory, which may be running), looks for one real improvement, and commits it with the AI
co-author trailer. The project's own checks run again here; only a passing change is pushed. Afterwards
the live directory is fast-forwarded if it has no local edits, so running jobs pick up the fix.

No real improvement means no commit. This exists to keep the projects working, not to fill the graph.

  python -m osc.maintain --dry        # show the rotation
  python -m osc.maintain [--repo NAME]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, log
from .claude_runner import extract_json, run_claude
from .daily import PROJECT, send_digest

STAGE = "maintain"
HOME = Path.home()
CLONES = PROJECT / ".maint"
PY = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"   # the interpreter with pytest and the research deps; launchd PATH finds Homebrew's first
STATE_F = PROJECT / "data" / "maintain_state.json"

# name -> live directory, check command (run in the clone), what the project is
REPOS = {
    "skilljail": (HOME / "skilljail", f"{PY} -m pytest -q tests",
                  "Kernel-enforced least privilege for AI agent skills: manifest -> Seatbelt jail + egress proxy bound to skill activation."),
    "vantage": (HOME / "vantage", "{live}/.venv/bin/python tests.py",
                "Daily stock/ETF research desk: factors, SEC EDGAR/13F data, bounded LLM reasoning, scorecard vs SPY, small web UI."),
    "trading-bot": (HOME / "trading-bot", f"{PY} -m pytest -q qr/tests",
                    "Quant research framework with pre-registered experiments; paper execution only, live trading blocked in code."),
    "highlight-answer": (HOME / "highlight-answer", "for f in extension/*.js server/*.js; do node --check \"$f\" || exit 1; done",
                         "Chrome extension plus a local helper server: highlight a question on a page, get the answer chosen or filled in."),
    "oss-contrib": (HOME / "oss-contrib", "{live}/.venv/bin/python -c \"import osc.daily, osc.responder, osc.discuss, osc.maintain, osc.server\"",
                    "Autonomous open-source contribution engine (scout, build, review, open PRs, answer feedback) with a FastAPI dashboard."),
}

SCHEMA = {
    "type": "object",
    "properties": {
        "changed": {"type": "boolean"},
        "area": {"type": "string"},
        "summary": {"type": "string"},
        "tests": {"type": "array", "items": {"type": "object", "properties": {"command": {"type": "string"}, "result": {"type": "string"}},
                                             "required": ["command", "result"]}},
    },
    "required": ["changed", "summary"],
}


def _sh(cmd: str | list, cwd: Path, timeout: int = 1800, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})


def _git(cwd: Path, *args: str) -> str:
    r = _sh(["git", *args], cwd)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr[-300:]}")
    return r.stdout


def _state() -> dict:
    try:
        return json.loads(STATE_F.read_text())
    except Exception:
        return {"last": {}, "history": []}


def _gh_env() -> dict:
    return {"GH_TOKEN": config.github_token(), "GIT_TERMINAL_PROMPT": "0"}


def _clone(name: str) -> Path:
    d = CLONES / name
    url = f"https://github.com/stanleys12/{name}.git"
    helper = '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
    if not d.exists():
        CLONES.mkdir(parents=True, exist_ok=True)
        r = _sh(["git", "-c", f"credential.helper={helper}", "clone", "-q", url, str(d)], CLONES, env=_gh_env())
        if r.returncode != 0:
            raise RuntimeError(f"clone failed: {r.stderr[-300:]}")
    _sh(["git", "-c", f"credential.helper={helper}", "fetch", "-q", "origin"], d, env=_gh_env())
    _git(d, "checkout", "-q", "-f", "main")
    _git(d, "reset", "-q", "--hard", "origin/main")
    _git(d, "clean", "-fdq", "-e", ".venv", "-e", "node_modules")
    return d


def pick(st: dict, only: str | None = None) -> str:
    if only:
        return only
    return min(REPOS, key=lambda n: st["last"].get(n, 0))


def run(dry: bool = False, only: str | None = None) -> dict:
    st = _state()
    name = pick(st, only)
    if dry:
        return {"next": name, "rotation": {n: time.strftime("%Y-%m-%d", time.localtime(st["last"].get(n, 0))) if st["last"].get(n) else "never" for n in REPOS}}
    live, test, about = REPOS[name]
    test = test.replace("{live}", str(live))
    st["last"][name] = time.time()
    rec = {"ts": time.time(), "repo": name, "changed": False, "summary": "", "commit": "", "cost": 0.0}
    try:
        d = _clone(name)
        before = _sh(test, d)
        old = _git(d, "rev-parse", "HEAD").strip()
        recent = "\n".join(f"- {h['repo']}: {h.get('area', '')} ({time.strftime('%m-%d', time.localtime(h['ts']))})"
                           for h in st["history"][-12:] if h.get("changed") and h["repo"] == name) or "(none yet)"
        leads = [h["summary"] for h in st["history"][-20:] if h["repo"] == name and h["summary"].startswith("dropped:") and not h.get("lead_done")]
        if leads:
            recent += ("\n\nAn earlier run found this but its change was not kept (a problem in the pipeline, not necessarily in the fix). "
                       "Re-check it first; if it is still real, it is today's work:\n" + "\n".join(f"- {l[:900]}" for l in leads[-2:]))
            for h in st["history"][-20:]:
                if h["repo"] == name and h["summary"].startswith("dropped:"):
                    h["lead_done"] = True
        p = (config.PROMPTS_DIR / "maintain.md").read_text()
        for k, v in {"__NAME__": name, "__URL__": f"https://github.com/stanleys12/{name}", "__ABOUT__": about,
                     "__LOG__": _git(d, "log", "-15", "--format=%h %ad %s", "--date=short"), "__RECENT__": recent,
                     "__TEST__": test + ("" if before.returncode == 0 else f"   (CURRENTLY FAILING: {(before.stdout + before.stderr)[-600:]})")}.items():
            p = p.replace(k, v)
        res = run_claude(p, d, model=config.setting("MAINTAIN_MODEL"), max_turns=120, stage=STAGE, repo=name, skip_permissions=True,
                         settings_json={"includeCoAuthoredBy": False}, json_schema=SCHEMA, max_budget_usd=float(config.setting("MAINTAIN_BUDGET_USD")),
                         disallowed_tools=["Bash(git push:*)", "Bash(git remote:*)", "Bash(git config:*)", "Bash(git rebase:*)", "Bash(git reset:*)",
                                           "Bash(gh:*)", "Read(**/.env)", "Edit(**/.env)", "Write(**/.env)"],
                         timeout_s=3600, append_system_prompt="Never push, never touch .env files or secrets, never modify files outside this directory.")
        rec["cost"] = round(res.cost_usd, 2)
        a = res.structured if isinstance(res.structured, dict) else extract_json(res.text) or {}
        rec["summary"], rec["area"] = a.get("summary", res.error[:300]), a.get("area", "")
        new = _git(d, "log", "--format=%H", f"{old}..HEAD").split()
        if _git(d, "status", "--porcelain").strip():
            _git(d, "reset", "-q", "--hard", "HEAD")                # uncommitted leftovers are dropped
        if not new:
            log.info(STAGE, f"{name}: nothing worth changing today. {rec['summary'][:200]}", repo=name)
            return rec
        msgs = _git(d, "log", "--format=%B", f"{old}..HEAD")
        touched = _git(d, "diff", "--name-only", old, "HEAD").split()
        bad = [f for f in touched if f.endswith(".env") or "/.env" in f or f.startswith(("data/", "reports/"))]
        after = _sh(test, d)
        problem = ("touched forbidden files: " + ", ".join(bad) if bad else
                   "checks fail after the change: " + (after.stdout + after.stderr)[-400:] if after.returncode != 0 else
                   "commit is missing the Co-Authored-By trailer" if "co-authored-by: claude" not in msgs.lower() else "")
        if problem:
            log.warn(STAGE, f"{name}: change dropped, {problem}", repo=name)
            rec["summary"] = f"dropped: {problem}. Agent said: {rec['summary']}"
            _git(d, "reset", "-q", "--hard", old)
            return rec
        helper = '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
        r = _sh(["git", "-c", f"credential.helper={helper}", "push", "-q", "origin", "HEAD:main"], d, env=_gh_env())
        if r.returncode != 0:
            raise RuntimeError(f"push failed: {r.stderr[-300:].replace(config.github_token(), '***')}")
        rec.update(changed=True, commit=_git(d, "log", "-1", "--format=%h %s").strip())
        log.ok(STAGE, f"{name}: pushed {rec['commit']}", repo=name)
        # bring the live project along when that is safe; otherwise say so
        if live.exists() and not _sh(["git", "status", "--porcelain", "--untracked-files=no"], live).stdout.strip():
            ff = _sh(["git", "-c", f"credential.helper={helper}", "pull", "-q", "--ff-only", "origin", "main"], live, env=_gh_env())
            rec["live"] = "updated" if ff.returncode == 0 else f"not updated: {ff.stderr[-160:]}"
        else:
            rec["live"] = "not updated: local edits in the live directory"
        return rec
    except Exception as e:
        rec["summary"] = f"error: {e}"[:400]
        log.error(STAGE, f"{name}: {e}", repo=name)
        return rec
    finally:
        st["history"] = (st["history"] + [rec])[-200:]
        STATE_F.write_text(json.dumps(st, indent=1))
        if rec.get("changed"):
            send_digest(f"[maintain] {name}: {rec['commit']}", f"{rec['summary']}\n\nhttps://github.com/stanleys12/{name}/commits/main\nLive directory: {rec.get('live')}\nCost ${rec['cost']}\n")


if __name__ == "__main__":
    a = sys.argv[1:]
    print(json.dumps(run(dry="--dry" in a, only=a[a.index("--repo") + 1] if "--repo" in a else None), indent=1, default=str))
