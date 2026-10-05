"""Stage 3 — build a change for an opportunity, then have an independent reviewer judge it.

Loop: builder → reviewer → (revise → builder with feedback) up to MAX_REVIEW_ROUNDS.
Never pushes. Output: a local branch in the workspace clone + a `changes` row holding the diff,
PR title/body, rationale, tests run, and the review verdict — everything the human needs to
decide whether to open the PR.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from . import config, db, log
from .analyzer import assert_workspace_free, ensure_clone, live_path, osc_dir, workspace_path, _default_branch, _run
from .claude_runner import extract_json, run_claude
from .quality import format_findings, lint_change
from .schemas import BUILD_SCHEMA, REVIEW_SCHEMA

STAGE = "build"


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n].rstrip("-") or "change"


def _git(path: Path, *args, check=True) -> str:
    r = _run(["git", *args], cwd=path)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[-400:]}")
    return r.stdout


_LEFTOVER_SKIP = re.compile(r"(^|/)(\.venv|venv|node_modules|build[^/]*|_build|dist|target|\.tox|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|cmake-build-[^/]*)(/|$)|\.(log|o|a|so|dylib|pyc)$")


def _stage_leftovers(path: Path) -> bool:
    """Stage source edits the builder left uncommitted, never build output, virtualenvs or submodule
    pointer moves. (A plain `git add -A -- . :!.venv` errors out when .venv is already gitignored.)
    Returns True if anything was staged."""
    subs = set(_git(path, "config", "--file", ".gitmodules", "--get-regexp", "path", check=False).split()[1::2])
    changed = [f for f in _git(path, "diff", "--name-only").splitlines() if f and f not in subs]
    new = [f for f in _git(path, "ls-files", "-o", "--exclude-standard").splitlines() if f and not _LEFTOVER_SKIP.search(f)]
    files = [f for f in changed + new if not _LEFTOVER_SKIP.search(f)]
    if files:
        _git(path, "add", "--", *files)
    return bool(files)


def _diff_stats(diff: str) -> dict:
    files, add, rem = set(), 0, 0
    for l in diff.splitlines():
        if l.startswith("+++ b/"):
            files.add(l[6:])
        elif l.startswith("+") and not l.startswith("+++"):
            add += 1
        elif l.startswith("-") and not l.startswith("---"):
            rem += 1
    return {"files": sorted(files), "additions": add, "deletions": rem, "total": add + rem}


def _contrib_text(repo: dict) -> str:
    t = repo.get("contributing_excerpt") or "(no CONTRIBUTING file found — follow the conventions you observe in the codebase and git log)"
    raw = repo.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if raw.get("pr_template"):
        t += "\n\n---- PULL REQUEST TEMPLATE ----\n" + raw["pr_template"]
    if raw.get("ai_policy"):
        t += "\n\nNOTE — this repo documents a policy on AI-assisted contributions (follow it exactly): " + raw["ai_policy"]
    if raw.get("has_agents_md"):
        t += "\n\nNOTE — this repo has an AGENTS.md; read it first and follow its rules for commits/tests/trailers."
    if repo.get("dco_required"):
        t += "\n\nNOTE: this repo requires DCO sign-off (`git commit -s`)."
    if repo.get("cla_required"):
        t += "\n\nNOTE: this repo requires a CLA; the human submitter will sign it."
    return t[:6000]


def _test_hint(path: Path) -> str:
    if (path / "pyproject.toml").exists() or (path / "setup.py").exists():
        return "python -m pytest <relevant test files> -q"
    if (path / "package.json").exists():
        return "npm test -- <pattern>"
    if (path / "Cargo.toml").exists():
        return "cargo test <name>"
    if (path / "go.mod").exists():
        return "go test ./..."
    return "run the project's test runner on the touched modules"


def run_builder(opp: dict, repo: dict, path: Path, out: Path, branch: str, feedback: str | None, model: str) -> tuple[dict, float]:
    p = (config.PROMPTS_DIR / "builder.md").read_text()
    fb = ""
    if feedback:
        fb = ("## Reviewer feedback from the previous round (address every required fix)\n" + feedback)
    p = (p.replace("__REPO__", opp["repo"]).replace("__BRANCH__", branch).replace("__KIND__", str(opp.get("kind")))
         .replace("__TITLE__", str(opp.get("title"))).replace("__SUMMARY__", str(opp.get("summary")))
         .replace("__RATIONALE__", str(opp.get("rationale")))
         .replace("__EVIDENCE__", "\n  - " + "\n  - ".join(opp.get("evidence") or []) if opp.get("evidence") else "(none)")
         .replace("__ISSUES__", ", ".join(f"#{n}" for n in (opp.get("related_issues") or [])) or "(none)")
         .replace("__APPROACH__", str(opp.get("approach"))).replace("__TESTS_PLAN__", str(opp.get("tests_plan") or "(design your own)"))
         .replace("__RISK__", str(opp.get("risk"))).replace("__REVIEW_FEEDBACK__", fb)
         .replace("__CONTRIBUTING__", _contrib_text(repo)).replace("__OSC_DIR__", str(out))
         .replace("__QUALITY_RULES__", (config.PROMPTS_DIR / "code_quality_rules.md").read_text()))
    from .opener import CLA_FAMILY
    # Google's cla/google check counts the AI co-author as an uncovered contributor and fails the PR (puppeteer #15499, #15532)
    google = CLA_FAMILY.get(opp["repo"].split("/")[0]) == "google" or opp["repo"].split("/")[0].lower().startswith("google")
    trailer = bool(config.setting("AI_COAUTHOR_TRAILER")) and not (repo.get("raw") or {}).get("no_ai_trailer") and not google
    p = p.replace("__TRAILER_RULE__", "Keep the default Co-authored-by trailer that your git commits add (this project discloses AI assistance)."
                  if trailer else "Do not add AI attribution lines or Co-Authored-By trailers; the human submitter owns the commit.")
    res = run_claude(
        p, path, model=model, max_turns=config.setting("BUILDER_MAX_TURNS"), stage="build", repo=opp["repo"],
        skip_permissions=True, settings_json={"includeCoAuthoredBy": trailer}, disallowed_tools=["Bash(git push:*)", "Bash(gh pr create:*)", "Bash(gh repo fork:*)"],
        json_schema=BUILD_SCHEMA, max_budget_usd=float(config.setting("REVISE_BUDGET_USD") if feedback else config.setting("BUILDER_BUDGET_USD")), transcript_path=out / "builder.jsonl", timeout_s=5400,
        append_system_prompt="You are working inside an isolated clone. Never push, never open PRs, never modify files outside the repo directory except the OSC output directory you were given.",
    )
    data = res.structured if isinstance(res.structured, dict) else None
    if not data:
        try:
            data = json.loads((out / "build_result.json").read_text())
        except Exception:
            data = extract_json(res.text) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("completed", False)
    if not res.ok and not data.get("completed"):
        data.setdefault("abandon_reason", f"builder error: {res.error[:300]}")
    return data, res.cost_usd


def run_reviewer(opp: dict, repo: dict, path: Path, out: Path, branch: str, base: str, build: dict, diff: str, rnd: int, model: str,
                 quality: dict | None = None) -> tuple[dict, float]:
    p = (config.PROMPTS_DIR / "reviewer.md").read_text()
    p = p.replace("__LINT_FINDINGS__", format_findings(quality) if quality else "(linter not run)")
    tests = "\n".join(f"- `{t.get('command')}` → {t.get('result')}" for t in (build.get("tests_run") or [])) or "(none reported)"
    trimmed = diff if len(diff) < 120_000 else diff[:120_000] + "\n... (diff truncated; read files for the rest)"
    p = (p.replace("__REPO__", opp["repo"]).replace("__BRANCH__", branch).replace("__BASE__", base)
         .replace("__PR_TITLE__", str(build.get("pr_title"))).replace("__SUMMARY__", str(build.get("summary")))
         .replace("__WHY__", str(build.get("why"))).replace("__TESTS_RUN__", tests)
         .replace("__LIMITATIONS__", str(build.get("limitations"))).replace("__PR_BODY__", str(build.get("pr_body") or "(none)")[:8000])
         .replace("__DIFF__", trimmed)
         .replace("__CONTRIBUTING__", _contrib_text(repo)).replace("__TEST_HINT__", _test_hint(path))
         .replace("__OSC_DIR__", str(out)).replace("__ROUND__", str(rnd)))
    res = run_claude(
        p, path, model=model, max_turns=config.setting("REVIEWER_MAX_TURNS"), stage="review", repo=opp["repo"],
        json_schema=REVIEW_SCHEMA, max_budget_usd=float(config.setting("REVIEWER_BUDGET_USD")), transcript_path=out / f"reviewer_{rnd}.jsonl", timeout_s=2400,
        allowed_tools=["Read", "Grep", "Glob", "LS", "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git grep:*)",
                       "Bash(git blame:*)", "Bash(gh pr:*)", "Bash(gh issue:*)", "Bash(gh search:*)", "Bash(python:*)", "Bash(python3:*)",
                       "Bash(pytest:*)", "Bash(.venv/bin/*)", "Bash(npm test:*)", "Bash(npx:*)", "Bash(cargo test:*)", "Bash(go test:*)",
                       "Bash(cat:*)", "Bash(ls:*)", "Bash(grep:*)", "Bash(rg:*)", f"Write({out}/*)"],
    )
    data = res.structured if isinstance(res.structured, dict) else None
    if not data:
        try:
            data = json.loads((out / f"review_round_{rnd}.json").read_text())
        except Exception:
            data = extract_json(res.text) or {}
    if not isinstance(data, dict) or "verdict" not in data:
        data = {"verdict": "revise", "score": 0, "required_fixes": [f"reviewer failed to produce a verdict: {res.error[:200]}"],
                "strengths": [], "suggestions": [], "maintainer_perspective": ""}
    return data, res.cost_usd



_TRANSIENT = ("Can't reach the API server", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT", "overloaded", "529", "rate_limit", "Connection error")


def _retry_transient(fn, what: str, repo: str, waits=(60, 180)):
    """Run fn(); on a transient API/network failure wait and retry, up to len(waits) times."""
    for i, wait in enumerate((*waits, None)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if wait is None or not any(t in msg for t in _TRANSIENT):
                raise
            log.warn(STAGE, f"{what}: transient failure ({msg[:80]}); retrying in {wait}s", repo=repo)
            time.sleep(wait)

def build_opportunity(opp_id: str, model: str | None = None, reviewer_model: str | None = None, max_rounds: int | None = None) -> dict:
    db.init()
    opp = db.row("SELECT * FROM opportunities WHERE id=?", (opp_id,))
    if not opp:
        raise RuntimeError(f"unknown opportunity {opp_id}")
    opp = db.parse_json_fields(opp, ["evidence", "related_issues", "related_prs", "raw"])
    repo = db.parse_json_fields(db.row("SELECT * FROM repos WHERE full_name=?", (opp["repo"],)), ["raw"])
    if (repo.get("raw") or {}).get("ai_prohibited"):
        raise RuntimeError(f"{opp['repo']} forbids AI-written PRs: {repo['raw']['ai_prohibited']}")
    model = model or config.setting("BUILDER_MODEL")
    reviewer_model = reviewer_model or config.setting("REVIEWER_MODEL")
    max_rounds = max_rounds or config.setting("MAX_REVIEW_ROUNDS")
    change_id = f"chg-{uuid.uuid4().hex[:10]}"
    out = osc_dir(opp["repo"]) / change_id
    out.mkdir(parents=True, exist_ok=True)
    branch = f"osc/{_slug(opp['title'])}-{change_id[-4:]}"
    db.update("opportunities", "id", opp_id, {"status": "building", "updated_at": time.time()})
    path = ensure_clone(opp["repo"], (repo.get("raw") or {}).get("default_branch"))
    base_branch = _default_branch(path)
    base_sha = _git(path, "rev-parse", "HEAD").strip()
    _git(path, "checkout", "-B", branch, base_sha)
    total_cost = 0.0
    change = {"id": change_id, "opportunity_id": opp_id, "repo": opp["repo"], "branch": branch, "base_sha": base_sha,
              "status": "building", "created_at": time.time(), "updated_at": time.time(), "model": model, "review_round": 0}
    db.upsert("changes", change, "id")
    log.info(STAGE, f"building '{opp['title']}' on {branch} (builder={model}, reviewer={reviewer_model})", repo=opp["repo"],
             data={"change_id": change_id, "opportunity_id": opp_id})
    feedback = None
    review: dict = {}
    build: dict = {}
    try:
        for rnd in range(1, max_rounds + 1):
            build, cost = _retry_transient(lambda: run_builder(opp, repo, path, out, branch, feedback, model if rnd == 1 else (config.setting("REVISE_MODEL") or model)), f"builder round {rnd}", opp["repo"])
            total_cost += cost
            # If the builder left uncommitted work, commit it so the diff is captured.
            if _stage_leftovers(path):
                _run(["git", "commit", "-q", "-m", build.get("pr_title") or opp["title"]], cwd=path)
            head_sha = _git(path, "rev-parse", "HEAD").strip()
            diff = _git(path, "diff", f"{base_sha}...{head_sha}") if head_sha != base_sha else ""
            stats = _diff_stats(diff)
            change.update({"head_sha": head_sha, "pr_title": build.get("pr_title"), "pr_body": build.get("pr_body"),
                           "summary": build.get("summary"), "why": build.get("why"), "files_changed": build.get("files_changed") or stats["files"],
                           "diff": diff, "diff_stats": stats, "tests_run": build.get("tests_run") or [], "limitations": build.get("limitations"),
                           "handoff": {"pre_pr_steps": build.get("pre_pr_steps") or [], "issue_comment": build.get("issue_comment") or "",
                                       "post_pr_notes": build.get("post_pr_notes") or []},
                           "cost_usd": round(total_cost, 3), "review_round": rnd, "updated_at": time.time()})
            if not build.get("completed") or not diff.strip():
                reason = build.get("abandon_reason") or "builder produced no diff"
                change.update({"status": "rejected", "status_note": f"abandoned by builder: {reason}"})
                db.upsert("changes", change, "id")
                db.update("opportunities", "id", opp_id, {"status": "rejected", "status_note": reason[:500], "updated_at": time.time()})
                log.warn(STAGE, f"abandoned: {reason[:300]}", repo=opp["repo"])
                return change
            log.ok(STAGE, f"round {rnd}: {stats['total']} diff lines across {len(stats['files'])} files; builder cost so far ${total_cost:.2f}",
                   repo=opp["repo"], data={"files": stats["files"]})
            change["status"] = "built"
            db.upsert("changes", change, "id")
            quality = lint_change(path, base_sha, head_sha, build.get("pr_title") or "", build.get("pr_body") or "",
                                  ai_policy=str((repo.get("raw") or {}).get("ai_policy") or ""))
            change["quality"] = quality
            (out / f"quality_round_{rnd}.json").write_text(json.dumps(quality, indent=2))
            log.info(STAGE, f"AI-pattern lint round {rnd}: score {quality['score']}/100 ({quality['verdict']}) {quality['counts']}", repo=opp["repo"],
                     data={"findings": quality["findings"][:20]})
            review, rcost = _retry_transient(lambda: run_reviewer(opp, repo, path, out, branch, base_sha, build, diff, rnd, reviewer_model, quality), f"reviewer round {rnd}", opp["repo"])
            total_cost += rcost
            change.update({"review": review, "review_score": review.get("score"), "review_verdict": review.get("verdict"),
                           "cost_usd": round(total_cost, 3), "status": "reviewed", "updated_at": time.time()})
            db.upsert("changes", change, "id")
            log.info(STAGE, f"review round {rnd}: {review.get('verdict')} score={review.get('score')} fixes={len(review.get('required_fixes') or [])}",
                     repo=opp["repo"], data={"required_fixes": review.get("required_fixes")})
            slop = [f for f in quality["findings"] if f["severity"] == "high"] + [f for f in (review.get("slop_findings") or [])]
            if review.get("verdict") == "approve" and quality["score"] < config.setting("MIN_QUALITY_SCORE"):
                review["verdict"] = "revise"
                review["required_fixes"] = list(review.get("required_fixes") or []) + [
                    f"AI-pattern lint score {quality['score']}/100 is below the {config.setting('MIN_QUALITY_SCORE')} gate; fix every finding listed below."]
                log.warn(STAGE, f"reviewer approved but lint gate failed ({quality['score']}); forcing revision", repo=opp["repo"])
            if review.get("verdict") == "approve":
                break
            if review.get("verdict") == "reject":
                break
            feedback = "\n".join(f"- {f}" for f in (review.get("required_fixes") or []))
            if review.get("slop_findings"):
                feedback += "\n\nMachine-generated-code patterns the reviewer flagged (each must be removed):\n" + "\n".join(f"- {f}" for f in review["slop_findings"])
            if quality["findings"]:
                feedback += "\n\nDeterministic lint findings:\n" + format_findings(quality)
            feedback += "\n\nReviewer's maintainer perspective: " + str(review.get("maintainer_perspective"))
        verdict = review.get("verdict")
        stats = change.get("diff_stats") or {}
        too_small = stats.get("total", 0) < config.setting("MIN_DIFF_LINES")
        too_big = stats.get("total", 0) > config.setting("MAX_DIFF_LINES")
        q_ok = (change.get("quality") or {}).get("score", 100) >= config.setting("MIN_QUALITY_SCORE")
        if verdict == "approve" and not too_small and q_ok:
            status, note = "ready", ("approved by reviewer" + (" (large diff — consider splitting)" if too_big else ""))
        elif verdict == "reject":
            status, note = "rejected", "rejected by reviewer: " + "; ".join(review.get("required_fixes") or [])[:400]
        else:
            status, note = "needs_work", ("diff too small to be meaningful" if too_small else
                                          "AI-pattern lint below gate" if not q_ok else "reviewer still requests fixes after max rounds")
        change.update({"status": status, "status_note": note, "updated_at": time.time()})
        db.upsert("changes", change, "id")
        db.update("opportunities", "id", opp_id, {"status": status if status != "rejected" else "rejected", "status_note": note, "updated_at": time.time()})
        (out / "change.json").write_text(json.dumps({k: v for k, v in change.items() if k != "diff"}, indent=2, default=str))
        (out / "change.diff").write_text(change.get("diff") or "")
        lvl = log.ok if status == "ready" else log.warn
        lvl(STAGE, f"change {change_id} → {status}: {note} (total ${total_cost:.2f})", repo=opp["repo"], data={"change_id": change_id})
        return change
    except Exception as e:
        change.update({"status": "failed", "status_note": str(e)[:800], "cost_usd": round(total_cost, 3), "updated_at": time.time()})
        db.upsert("changes", change, "id")
        db.update("opportunities", "id", opp_id, {"status": "proposed", "status_note": f"build failed: {e}"[:500], "updated_at": time.time()})
        log.error(STAGE, f"build failed: {e}", repo=opp["repo"])
        raise
    finally:
        # leave the workspace on the default branch so other jobs start clean; the branch persists
        try:
            _git(path, "checkout", "-q", base_branch, check=False)
        except Exception:
            pass


# ------------------------------------------------------------------------------------
# Human hand-off: fork + push (explicit action) and PR command generation. Never opens the PR.
# ------------------------------------------------------------------------------------
def prepare_submission(change_id: str) -> dict:
    ch = db.row("SELECT * FROM changes WHERE id=?", (change_id,))
    if not ch:
        raise RuntimeError("unknown change")
    if ch["status"] not in ("ready", "needs_work", "reviewed", "prepared", "submitted"):
        raise RuntimeError(f"change is {ch['status']}, not ready")
    path = live_path(ch["repo"])
    assert_workspace_free(path)
    me = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True,
                        env={**__import__('os').environ, "GH_TOKEN": config.github_token()}).stdout.strip()
    if not me:
        raise RuntimeError("gh could not resolve the current user (token issue)")
    env = {**__import__("os").environ, "GH_TOKEN": config.github_token(), "GIT_TERMINAL_PROMPT": "0"}
    name = ch["repo"].split("/")[1]
    fork = f"{me}/{name}"
    r = subprocess.run(["gh", "repo", "view", fork, "--json", "url", "-q", ".url"], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        log.info("prepare", f"forking {ch['repo']} → {fork}", repo=ch["repo"])
        r = subprocess.run(["gh", "repo", "fork", ch["repo"], "--clone=false", "--remote=false"], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"fork failed: {r.stderr[-300:]}")
        time.sleep(3)
    fork_url = f"https://github.com/{fork}"
    push_url = f"https://{me}:{config.github_token()}@github.com/{fork}.git"
    # a named remote gives --force-with-lease a tracking ref to compare against (re-pushes after amends stay safe)
    subprocess.run(["git", "remote", "remove", "fork"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "remote", "add", "fork", push_url], cwd=str(path), capture_output=True)
    subprocess.run(["git", "fetch", "-q", "fork", ch["branch"]], cwd=str(path), capture_output=True, env=env)
    r = subprocess.run(["git", "push", "--force-with-lease", "fork", f"{ch['branch']}:{ch['branch']}"], cwd=str(path),
                       capture_output=True, text=True, env=env)
    subprocess.run(["git", "remote", "set-url", "fork", f"https://github.com/{fork}.git"], cwd=str(path), capture_output=True)  # drop token from config
    if r.returncode != 0:
        raise RuntimeError(f"push to fork failed: {r.stderr[-300:].replace(config.github_token(), '***')}")
    body_path = osc_dir(ch["repo"]) / ch["id"] / "pr_body.md"
    body_path.write_text(ch.get("pr_body") or "")
    base = _default_branch(path)
    cmd = (f"gh pr create --repo {ch['repo']} --base {base} --head {me}:{ch['branch']} "
           f"--title {json.dumps(ch.get('pr_title') or '')} --body-file {json.dumps(str(body_path))}")
    if ch["status"] == "submitted":
        db.update("changes", "id", change_id, {"fork_url": fork_url, "status_note": "branch re-pushed; open PR updated", "updated_at": time.time()})
    else:
        db.update("changes", "id", change_id, {"status": "prepared", "fork_url": fork_url, "status_note": "branch pushed to fork; PR not opened",
                                               "updated_at": time.time()})
        db.update("opportunities", "id", ch["opportunity_id"], {"status": "prepared", "updated_at": time.time()})
    log.ok("prepare", f"branch {ch['branch']} pushed to {fork_url}; run the printed gh command to open the PR", repo=ch["repo"],
           data={"command": cmd})
    return {"fork_url": fork_url, "branch": ch["branch"], "pr_create_command": cmd, "body_file": str(body_path),
            "compare_url": f"https://github.com/{ch['repo']}/compare/{base}...{me}:{name}:{ch['branch']}?expand=1"}


# ------------------------------------------------------------------------------------
# Revision / re-review of an existing change (used by the worker `revise`/`rereview` jobs)
# ------------------------------------------------------------------------------------
def _load_change(cid: str):
    c = db.parse_json_fields(db.row("SELECT * FROM changes WHERE id=?", (cid,)), ["tests_run", "quality", "review", "handoff"])
    if not c:
        raise RuntimeError(f"unknown change {cid}")
    opp = db.parse_json_fields(db.row("SELECT * FROM opportunities WHERE id=?", (c["opportunity_id"],)), ["evidence", "related_issues", "related_prs"])
    repo = db.parse_json_fields(db.row("SELECT * FROM repos WHERE full_name=?", (c["repo"],)), ["raw"])
    return c, opp, repo


def rereview_change(cid: str) -> dict:
    from .quality import lint_change
    from .analyzer import assert_workspace_free
    c, opp, repo = _load_change(cid)
    path = live_path(c["repo"]); out = osc_dir(c["repo"]) / cid
    assert_workspace_free(path)
    base_branch = _default_branch(path)
    _git(path, "checkout", "-q", c["branch"])
    try:
        head = _git(path, "rev-parse", "HEAD").strip(); diff = _git(path, "diff", f"{c['base_sha']}...{head}")
        q = lint_change(path, c["base_sha"], head, c["pr_title"] or "", c["pr_body"] or "", ai_policy=str((repo.get("raw") or {}).get("ai_policy") or ""))
        rnd = int(c.get("review_round") or 0) + 1
        build = {k: c.get(k) for k in ("pr_title", "pr_body", "summary", "why", "tests_run", "limitations")}
        review, cost = run_reviewer(opp, repo, path, out, c["branch"], c["base_sha"], build, diff, rnd, config.setting("REVIEWER_MODEL"), q)
        ok = review.get("verdict") == "approve" and q["score"] >= config.setting("MIN_QUALITY_SCORE")
        status = "ready" if ok else ("rejected" if review.get("verdict") == "reject" else "needs_work")
        if c["status"] in ("prepared", "submitted") and ok:
            status = c["status"]
        db.update("changes", "id", cid, {"head_sha": head, "diff": diff, "diff_stats": _diff_stats(diff), "quality": q, "review": review,
            "review_score": review.get("score"), "review_verdict": review.get("verdict"), "review_round": rnd,
            "cost_usd": round((c["cost_usd"] or 0) + cost, 3), "status": status,
            "status_note": f"re-reviewed (round {rnd}): {review.get('verdict')} {review.get('score')}/10, reads-human {review.get('reads_human')}, AI-lint {q['score']}", "updated_at": time.time()})
        return {"change": cid, "status": status, "verdict": review.get("verdict"), "score": review.get("score"), "lint": q["score"], "cost": round(cost, 2)}
    finally:
        _git(path, "checkout", "-q", base_branch, check=False)


def revise_change(cid: str, model: str | None = None) -> dict:
    from .quality import lint_change, format_findings
    from .analyzer import assert_workspace_free
    c, opp, repo = _load_change(cid)
    path = live_path(c["repo"]); out = osc_dir(c["repo"]) / cid
    assert_workspace_free(path)
    base_branch = _default_branch(path)
    review, q = c.get("review") or {}, c.get("quality") or {}
    feedback = "\n".join(f"- {f}" for f in (review.get("required_fixes") or []))
    if review.get("slop_findings"):
        feedback += "\n\nMachine-generated-code patterns the reviewer flagged (each must be removed):\n" + "\n".join(f"- {f}" for f in review["slop_findings"])
    if q.get("findings"):
        feedback += "\n\nDeterministic lint findings (fix every one; the change cannot ship below lint 85):\n" + format_findings(q, 60)
    feedback += "\n\nReviewer's maintainer perspective: " + str(review.get("maintainer_perspective") or "")
    feedback += ("\n\nYou are revising an existing branch: keep the commits, amend or add as the repo's style suggests, and keep the diff focused. "
                 "Prefer deleting docstrings/comments/assert messages over rewording them.")
    _git(path, "checkout", "-q", c["branch"])
    model = model or config.setting("BUILDER_MODEL")
    rnd = int(c.get("review_round") or 0) + 1
    log.info("build", f"revision round {rnd} on {cid} ({model})", repo=c["repo"])
    db.update("changes", "id", cid, {"status": "building", "updated_at": time.time()})
    try:
        build, bcost = run_builder(opp, repo, path, out, c["branch"], feedback, model)
        if _stage_leftovers(path):
            _run(["git", "commit", "-q", "-m", build.get("pr_title") or c["pr_title"] or opp["title"]], cwd=path)
        head = _git(path, "rev-parse", "HEAD").strip(); diff = _git(path, "diff", f"{c['base_sha']}...{head}")
        pr_title = build.get("pr_title") or c["pr_title"]; pr_body = build.get("pr_body") or c["pr_body"]
        q2 = lint_change(path, c["base_sha"], head, pr_title or "", pr_body or "", ai_policy=str((repo.get("raw") or {}).get("ai_policy") or ""))
        build2 = {"pr_title": pr_title, "pr_body": pr_body, "summary": build.get("summary") or c["summary"], "why": build.get("why") or c["why"],
                  "tests_run": build.get("tests_run") or c["tests_run"], "limitations": build.get("limitations") or c["limitations"]}
        review2, rcost = run_reviewer(opp, repo, path, out, c["branch"], c["base_sha"], build2, diff, rnd, config.setting("REVIEWER_MODEL"), q2)
        ok = review2.get("verdict") == "approve" and q2["score"] >= config.setting("MIN_QUALITY_SCORE")
        status = "ready" if ok else ("rejected" if review2.get("verdict") == "reject" else "needs_work")
        handoff = c.get("handoff") or {}
        for k in ("pre_pr_steps", "issue_comment", "post_pr_notes"):
            if build.get(k):
                handoff[k] = build[k]
        st = _diff_stats(diff)
        db.update("changes", "id", cid, {"head_sha": head, "diff": diff, "diff_stats": st, "files_changed": st["files"], "pr_title": pr_title, "pr_body": pr_body,
            "summary": build2["summary"], "why": build2["why"], "tests_run": build2["tests_run"], "limitations": build2["limitations"], "handoff": handoff,
            "quality": q2, "review": review2, "review_score": review2.get("score"), "review_verdict": review2.get("verdict"), "review_round": rnd,
            "cost_usd": round((c["cost_usd"] or 0) + bcost + rcost, 3), "status": status,
            "status_note": f"revision round {rnd}: {review2.get('verdict')} {review2.get('score')}/10, reads-human {review2.get('reads_human')}, AI-lint {q2['score']}", "updated_at": time.time()})
        (out / "change.diff").write_text(diff)
        lvl = log.ok if ok else log.warn
        lvl("build", f"revision round {rnd} → {status} (review {review2.get('score')}, lint {q2['score']}, ${bcost + rcost:.2f})", repo=c["repo"])
        return {"change": cid, "status": status, "verdict": review2.get("verdict"), "score": review2.get("score"), "lint": q2["score"], "diff_lines": st["total"], "cost": round(bcost + rcost, 2)}
    except Exception as e:
        db.update("changes", "id", cid, {"status": "needs_work", "status_note": f"revision failed: {e}"[:400], "updated_at": time.time()})
        raise
    finally:
        _git(path, "checkout", "-q", base_branch, check=False)
