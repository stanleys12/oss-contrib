"""Stage 2 — deep per-repo analysis → concrete contribution opportunities.

1. clone (shallow) into workspace/<owner>__<name>
2. fetch open issues (GraphQL) and triage for claimability
3. collect static signals (ruff/bandit/pip-audit for Python, TODO/FIXME hot spots, recent churn)
4. run the scout agent (headless Claude) which verifies leads and returns ranked opportunities
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path

from . import config, db, log
from .claude_runner import extract_json, run_claude
from .gh import GitHub, repo_issues, repo_open_prs
from .schemas import OPPORTUNITIES_SCHEMA

STAGE = "analyze"
CLAIM_LABELS = {"good first issue", "good-first-issue", "help wanted", "help-wanted", "bug", "contributions welcome",
                "contributions-welcome", "easy", "beginner", "starter", "up-for-grabs", "enhancement", "feature request",
                "feature-request", "good first contribution", "needs-fix", "confirmed"}
NEG_LABELS = {"wontfix", "won't fix", "invalid", "duplicate", "question", "discussion", "stale", "needs-triage",
              "needs triage", "roadmap", "epic", "tracking", "meta", "blocked", "needs-design", "rfc"}


def workspace_path(full_name: str) -> Path:
    return config.WORKSPACE_DIR / full_name.replace("/", "__")


def osc_dir(full_name: str) -> Path:
    d = config.REPORTS_DIR / full_name.replace("/", "__")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------------------------------
# 1. clone / refresh
# ------------------------------------------------------------------------------------
def assert_workspace_free(path: Path, max_age_s: int = 3 * 3600):
    """Refuse to touch a clone while a builder/reviewer/scout agent is running inside it."""
    lock = path / ".osc_agent_lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < max_age_s:
        raise RuntimeError(f"workspace {path.name} is in use by an agent ({lock.read_text().strip()}); wait for it to finish")


def live_path(full_name: str) -> Path:
    """workspace_path, restoring the clone first if housekeeping parked it to save disk."""
    from .housekeeping import restore
    restore(full_name)
    return workspace_path(full_name)


def ensure_clone(full_name: str, default_branch: str | None = None, fresh: bool = False, _restoring: bool = False) -> Path:
    path = workspace_path(full_name)
    if not path.exists() and not _restoring and not fresh:
        from .housekeeping import PARKED, restore
        if (PARKED / path.name / "meta.json").exists():
            restore(full_name)      # brings back the parked work branches, then falls through to a normal refresh
    if path.exists():
        assert_workspace_free(path)
    url = f"https://github.com/{full_name}.git"
    depth = str(config.setting("CLONE_DEPTH"))
    if fresh and path.exists():
        shutil.rmtree(path)
    if not path.exists():
        from .housekeeping import ensure_space
        ensure_space(float(config.setting("MIN_FREE_GB")), keep={full_name})
        log.info(STAGE, f"cloning (depth {depth})", repo=full_name)
        r = _run(["git", "clone", "--depth", depth, "--no-tags", url, str(path)], timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"clone failed: {r.stderr[-500:]}")
        # unshallow enough history for blame/log context without pulling everything
        _run(["git", "fetch", "--deepen=200", "--no-tags"], cwd=path, timeout=1800)
    else:
        log.info(STAGE, "refreshing clone", repo=full_name)
        _run(["git", "fetch", "--depth", "200", "--no-tags", "origin"], cwd=path, timeout=1800)
        br = default_branch or _default_branch(path)
        _run(["git", "checkout", "-f", br], cwd=path)
        _run(["git", "reset", "--hard", f"origin/{br}"], cwd=path)
        _run(["git", "clean", "-fdx", "-e", ".venv", "-e", "node_modules"], cwd=path)
    # never allow pushes to upstream from the workspace clone
    _run(["git", "remote", "set-url", "--push", "origin", "DISABLED_NO_PUSH"], cwd=path)
    # keep local tool dirs out of any commit, whatever the repo's .gitignore says
    excl = path / ".git" / "info" / "exclude"
    try:
        cur = excl.read_text() if excl.exists() else ""
        add = [l for l in (".venv/", "venv/", "node_modules/", ".osc_*", "*.egg-info/") if l not in cur]
        if add:
            excl.write_text(cur.rstrip("\n") + "\n" + "\n".join(add) + "\n")
    except Exception:
        pass
    return path


def _default_branch(path: Path) -> str:
    r = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=path)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/")[-1]
    r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    return r.stdout.strip() or "main"


# ------------------------------------------------------------------------------------
# 2. issues triage
# ------------------------------------------------------------------------------------
def _age_days(iso: str) -> float:
    t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    return (dt.datetime.utcnow() - t).total_seconds() / 86400


def triage_issues(gh: GitHub, full_name: str, max_issues: int = 150) -> list[dict]:
    raw = repo_issues(gh, full_name, max_issues=max_issues)
    out = []
    for n in raw:
        labels = [l["name"].lower() for l in n["labels"]["nodes"]]
        linked = 0
        for ti in n["timelineItems"]["nodes"]:
            pr = (ti.get("source") or ti.get("subject") or {})
            if pr.get("number"):
                linked += 1 if pr.get("state") in ("OPEN", "MERGED") else 0
        assignees = n["assignees"]["totalCount"]
        age = _age_days(n["createdAt"])
        upd = _age_days(n["updatedAt"])
        pos = len(set(labels) & CLAIM_LABELS)
        neg = len(set(labels) & NEG_LABELS)
        body = (n.get("body") or "")
        claimable = assignees == 0 and linked == 0 and neg == 0
        score = 0.0
        if claimable:
            score = (0.35 * min(1.0, pos / 2) + 0.15 * min(1.0, n["comments"]["totalCount"] / 6)
                     + 0.15 * min(1.0, n["reactions"]["totalCount"] / 5)
                     + 0.15 * (1.0 if 3 <= age <= 240 else 0.4)
                     + 0.10 * (1.0 if upd <= 60 else 0.3)
                     + 0.10 * (1.0 if 200 <= len(body) <= 6000 else 0.4))
            if any(k in labels for k in ("good first issue", "good-first-issue")):
                score += 0.15
            if "bug" in labels:
                score += 0.05
        row = {
            "id": f"{full_name}#{n['number']}", "repo": full_name, "number": n["number"], "title": n["title"],
            "url": n["url"], "labels": labels, "state": "open", "created_at": n["createdAt"],
            "updated_at": n["updatedAt"], "comments": n["comments"]["totalCount"],
            "reactions": n["reactions"]["totalCount"], "assignees": assignees, "linked_prs": linked,
            "body_excerpt": body[:1500], "author_assoc": n.get("authorAssociation"),
            "claimable": int(claimable), "triage_score": round(score, 3), "fetched_at": time.time(),
        }
        db.upsert("issues", row, "id")
        out.append(row)
    out.sort(key=lambda r: -r["triage_score"])
    return out


# ------------------------------------------------------------------------------------
# 3. static signals
# ------------------------------------------------------------------------------------
def _tool(name: str) -> str | None:
    p = config.VENV_BIN / name
    return str(p) if p.exists() else shutil.which(name)


def collect_signals(path: Path, language: str | None) -> dict:
    sig: dict = {"language": language}
    # churn: most-changed files recently
    r = _run(["git", "log", "--since=90 days ago", "--name-only", "--pretty=format:"], cwd=path)
    churn = Counter(l.strip() for l in r.stdout.splitlines() if l.strip())
    sig["churn_top"] = churn.most_common(25)
    # TODO/FIXME/XXX/HACK hot spots
    r = _run(["grep", "-rInE", r"\b(TODO|FIXME|XXX|HACK|BUG)\b", "--include=*.py", "--include=*.ts", "--include=*.tsx",
              "--include=*.js", "--include=*.rs", "--include=*.go", "--include=*.c", "--include=*.cc", "--include=*.cpp",
              "--include=*.cu", "--include=*.h", "--include=*.hpp", "--exclude-dir=node_modules", "--exclude-dir=.git",
              "--exclude-dir=third_party", "--exclude-dir=vendor", "--exclude-dir=.venv", "."], cwd=path, timeout=300)
    todo_lines = r.stdout.splitlines()
    sig["todo_count"] = len(todo_lines)
    sig["todo_sample"] = [l[:220] for l in todo_lines if re.search(r"(FIXME|BUG|HACK)", l)][:60] or [l[:220] for l in todo_lines[:40]]
    # language-specific analyzers
    py_files = list(path.rglob("*.py"))
    if py_files and (ruff := _tool("ruff")):
        r = _run([ruff, "check", "--select", "B,PLE,F821,F823,F841,E711,E712,E721,S102,S307,S602,S605,PLW0120,RUF006,ASYNC",
                  "--output-format", "concise", "--no-fix", "--exit-zero", "--quiet", "."], cwd=path, timeout=600)
        lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith("warning")]
        codes = Counter(l.split()[1].rstrip(":") for l in lines if len(l.split()) > 1)
        sig["ruff_count"] = len(lines)
        sig["ruff_by_code"] = codes.most_common(15)
        sig["ruff_sample"] = [l[:220] for l in lines if not re.search(r"(test|tests|examples?|docs?|benchmarks?)/", l)][:80]
    if py_files and (bandit := _tool("bandit")):
        r = _run([bandit, "-q", "-r", ".", "-ll", "-f", "json", "-x", "./tests,./test,./examples,./docs,./.venv,./node_modules,./benchmarks"],
                 cwd=path, timeout=900)
        try:
            data = json.loads(r.stdout)
            sig["bandit"] = [{"file": x["filename"], "line": x["line_number"], "id": x["test_id"], "sev": x["issue_severity"],
                              "conf": x["issue_confidence"], "text": x["issue_text"][:200]} for x in data.get("results", [])][:60]
        except Exception:
            sig["bandit"] = []
    reqs = [p for p in ("requirements.txt", "pyproject.toml") if (path / p).exists()]
    if reqs and (pa := _tool("pip-audit")):
        try:
            args = [pa, "-f", "json", "--progress-spinner", "off"]
            args += ["-r", "requirements.txt"] if "requirements.txt" in reqs else []
            r = _run(args, cwd=path, timeout=600)
            data = json.loads(r.stdout or "{}")
            vulns = [{"name": d["name"], "version": d["version"], "vulns": [v["id"] for v in d.get("vulns", [])]}
                     for d in data.get("dependencies", []) if d.get("vulns")]
            sig["pip_audit"] = vulns[:30]
        except Exception as e:
            sig["pip_audit_error"] = str(e)[:200]
    # test presence heuristic
    tests = [str(p.relative_to(path)) for p in path.rglob("*") if p.is_dir() and p.name in ("tests", "test", "__tests__", "spec")
             and ".git" not in p.parts and "node_modules" not in p.parts][:10]
    sig["test_dirs"] = tests
    sig["file_counts"] = {"py": len(py_files), "ts": len(list(path.rglob("*.ts"))), "rs": len(list(path.rglob("*.rs"))),
                          "cpp": len(list(path.rglob("*.cpp"))) + len(list(path.rglob("*.cc"))), "go": len(list(path.rglob("*.go")))}
    return sig


# ------------------------------------------------------------------------------------
# 4. scout agent
# ------------------------------------------------------------------------------------
def run_scout(full_name: str, repo_row: dict, path: Path, out_dir: Path, model: str | None = None, focus: str | None = None) -> dict:
    prompt = (config.PROMPTS_DIR / "scout.md").read_text()
    if focus:
        prompt = prompt.replace("## Inputs you have", "## Focus requested by the operator\n" + focus.strip() + "\n\n## Inputs you have", 1)
    prompt = (prompt.replace("__REPO__", full_name).replace("__STARS__", str(repo_row.get("stars")))
              .replace("__LANG__", str(repo_row.get("language"))).replace("__DOMAIN__", str(repo_row.get("domain")))
              .replace("__OSC_DIR__", str(out_dir)))
    res = run_claude(
        prompt, path, model=model or config.setting("SCOUT_MODEL"), max_turns=config.setting("SCOUT_MAX_TURNS"),
        stage="scout", repo=full_name, json_schema=OPPORTUNITIES_SCHEMA,
        allowed_tools=["Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "Bash(git log:*)", "Bash(git blame:*)",
                       "Bash(git show:*)", "Bash(git diff:*)", "Bash(git grep:*)", "Bash(gh issue:*)", "Bash(gh pr:*)",
                       "Bash(gh search:*)", "Bash(gh api:*)", "Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)",
                       "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(ls:*)", "Bash(find:*)", "Bash(grep:*)",
                       "Bash(rg:*)", "Bash(wc:*)", "Bash(sed -n:*)", "Bash(cargo test:*)", "Bash(go test:*)",
                       "Bash(npm test:*)", "Bash(npx:*)", f"Write({out_dir}/*)"],
        max_budget_usd=float(config.setting("SCOUT_BUDGET_USD")), transcript_path=out_dir / "scout.jsonl", timeout_s=int(config.setting("SCOUT_TIMEOUT_S")),
    )
    data = res.structured if isinstance(res.structured, dict) else None
    if not data:
        try:
            data = json.loads((out_dir / "opportunities.json").read_text())
        except Exception:
            data = extract_json(res.text)
    if not isinstance(data, dict) or "opportunities" not in data:
        raise RuntimeError(f"scout produced no parseable opportunities (err={res.error[:200]})")
    data["_cost_usd"] = res.cost_usd
    data["_turns"] = res.turns
    (out_dir / "opportunities.json").write_text(json.dumps(data, indent=2))
    return data


def store_opportunities(full_name: str, data: dict) -> list[str]:
    ids = []
    for o in data.get("opportunities", []):
        oid = f"{full_name.replace('/', '__')}--{uuid.uuid4().hex[:8]}"
        conf, acc = float(o.get("confidence", 0.5)), float(o.get("accept_likelihood", 0.5))
        kind_w = {"security": 1.15, "bug": 1.1, "robustness": 1.0, "feature": 0.95, "performance": 0.95, "tests": 0.85,
                  "docs-substantive": 0.7, "other": 0.8}.get(o.get("kind", "other"), 0.8)
        row = {
            "id": oid, "repo": full_name, "kind": o.get("kind"), "title": o.get("title"), "summary": o.get("summary"),
            "rationale": o.get("rationale"), "approach": o.get("approach"), "evidence": o.get("evidence", []),
            "related_issues": o.get("related_issues", []), "related_prs": o.get("related_prs", []),
            "scope": o.get("scope"), "risk": o.get("risk"), "confidence": conf, "accept_likelihood": acc,
            "priority": round(kind_w * (0.5 * conf + 0.5 * acc), 3), "duplicate_check": o.get("duplicate_check"),
            "tests_plan": o.get("tests_plan"), "status": "proposed", "created_at": time.time(),
            "updated_at": time.time(), "raw": o,
        }
        db.upsert("opportunities", row, "id")
        ids.append(oid)
    return ids


# ------------------------------------------------------------------------------------
# orchestration
# ------------------------------------------------------------------------------------
def analyze_repo(full_name: str, model: str | None = None, skip_scout: bool = False, focus: str | None = None) -> dict:
    db.init()
    repo = db.row("SELECT * FROM repos WHERE full_name=?", (full_name,))
    if not repo:
        raise RuntimeError(f"{full_name} not in repos table; scan it first (osc scan --repo {full_name})")
    repo = db.parse_json_fields(repo, ["raw"])
    db.update("repos", "full_name", full_name, {"status": "analyzing", "status_note": None})
    t0 = time.time()
    try:
        gh = GitHub()
        out = osc_dir(full_name)
        path = ensure_clone(full_name, (repo.get("raw") or {}).get("default_branch"))
        log.info(STAGE, "fetching + triaging open issues", repo=full_name)
        issues = triage_issues(gh, full_name)
        claimable = [i for i in issues if i["claimable"]][:40]
        (out / "issues.json").write_text(json.dumps(claimable, indent=2))
        prs = repo_open_prs(gh, full_name, max_prs=300)
        (out / "open_prs.json").write_text(json.dumps([{"number": p["number"], "title": p["title"], "author": (p.get("author") or {}).get("login"),
                                                        "updated": p["updatedAt"], "draft": p["isDraft"]} for p in prs], indent=2))
        log.info(STAGE, f"{len(issues)} open issues fetched, {len(claimable)} claimable; {len(prs)} open PRs indexed", repo=full_name)
        log.info(STAGE, "collecting static signals (ruff/bandit/pip-audit/todo/churn)", repo=full_name)
        sig = collect_signals(path, repo.get("language"))
        (out / "signals.json").write_text(json.dumps(sig, indent=2))
        contrib = (repo.get("contributing_excerpt") or "(no CONTRIBUTING file found)")
        contrib += "\n\n---- PULL REQUEST TEMPLATE ----\n" + ((repo.get("raw") or {}).get("pr_template") or "(none)")
        # agent/AI policy files live in the clone; surface them to the scout and to the hand-off checklist
        from .scanner import _ai_policy, ai_prohibited, ai_prose_must_be_human
        policy_text, agents_md = "", ""
        policy_files = ["AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md",
                        ".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md"]
        for pat in ("*ontributing*.md", "*/*ontributing*.md", "*/*/*ontributing*.md", "*/AGENTS.md", "*/*/AGENTS.md"):
            for q in sorted(path.glob(pat)):
                rel = str(q.relative_to(path))
                if "node_modules" not in q.parts and "target" not in q.parts and rel not in policy_files and len(policy_files) < 20:
                    policy_files.append(rel)
        for fn in policy_files:
            fp = path / fn
            if fp.exists():
                try:
                    txt = fp.read_text(errors="ignore")
                except Exception:
                    continue
                policy_text += f"\n\n[{fn}]\n" + txt[:20000]
                if fn in ("AGENTS.md", "CLAUDE.md") and not agents_md:
                    agents_md = txt[:6000]
        ai_policy = _ai_policy(policy_text)
        if agents_md:
            contrib += "\n\n---- AGENTS.md (repo guidance for AI-assisted work) ----\n" + agents_md
        (out / "contributing.md").write_text(contrib)
        raw = dict(repo.get("raw") or {})
        raw["ai_policy"] = ai_policy
        raw["has_agents_md"] = bool(agents_md)
        prohibited = ai_prohibited(policy_text)
        # garak-style "pure code-agent PRs are not allowed" still permits human-reviewed AI-assisted work; only a
        # blanket ban / "not written by an LLM" attestation blocks us
        if prohibited and re.search(r"pure code-agent", prohibited, re.I):
            prohibited = ""
        raw["ai_prohibited"] = prohibited
        raw["ai_prose_human"] = ai_prose_must_be_human(policy_text)
        # repos that reject AI co-author trailers via a CI check (e.g. ComfyUI's check-ai-co-authors.yml)
        no_trailer = ""
        for wf in sorted((path / ".github" / "workflows").glob("*.y*ml")) if (path / ".github" / "workflows").exists() else []:
            try:
                txt = wf.read_text(errors="ignore")
            except Exception:
                continue
            if re.search(r"co-?author", txt, re.I) and re.search(r"anthropic|claude|copilot|cursor|openai|\bai\b|agent", txt, re.I) and re.search(r"fail|exit 1|error", txt, re.I):
                no_trailer = f"{wf.name}: CI rejects AI co-author trailers"
                break
        raw["no_ai_trailer"] = no_trailer
        if no_trailer:
            log.info(STAGE, f"repo rejects AI co-author trailers ({no_trailer}); builds will omit the trailer", repo=full_name)
        db.update("repos", "full_name", full_name, {"raw": raw})
        repo["raw"] = raw
        if ai_policy:
            log.info(STAGE, f"AI-contribution policy found: {ai_policy[:200]}", repo=full_name)
        if prohibited:
            log.warn(STAGE, f"repo forbids AI-written PRs; marking skipped: {prohibited[:200]}", repo=full_name)
            db.update("repos", "full_name", full_name, {"status": "skipped", "status_note": "AI-written PRs prohibited by repo policy: " + prohibited, "analyzed_at": time.time()})
            (out / "analysis_summary.json").write_text(json.dumps({"skipped": "ai_prohibited", "policy": prohibited}, indent=2))
            return {"skipped": "ai_prohibited", "policy": prohibited}
        log.info(STAGE, f"signals: ruff={sig.get('ruff_count', 'n/a')} bandit={len(sig.get('bandit', []))} "
                        f"todo={sig.get('todo_count')} pip-audit={len(sig.get('pip_audit', []))}", repo=full_name)
        result = {"issues": len(issues), "claimable": len(claimable), "signals": {k: v for k, v in sig.items() if k.endswith("count")}}
        if not skip_scout:
            log.info(STAGE, "launching scout agent", repo=full_name)
            data = run_scout(full_name, repo, path, out, model, focus)
            ids = store_opportunities(full_name, data)
            result["opportunities"] = ids
            result["repo_notes"] = data.get("repo_notes")
            result["cost_usd"] = data.get("_cost_usd")
            log.ok(STAGE, f"{len(ids)} opportunities proposed (scout ${data.get('_cost_usd', 0):.2f})", repo=full_name,
                   data={"titles": [o["title"] for o in data["opportunities"]]})
            db.update("repos", "full_name", full_name, {"status_note": (data.get("repo_notes") or "")[:2000]})
        db.update("repos", "full_name", full_name, {"status": "analyzed", "analyzed_at": time.time()})
        (out / "analysis_summary.json").write_text(json.dumps({**result, "elapsed_s": round(time.time() - t0)}, indent=2))
        return result
    except Exception as e:
        db.update("repos", "full_name", full_name, {"status": "error", "status_note": str(e)[:1000]})
        log.error(STAGE, f"analysis failed: {e}", repo=full_name)
        raise
