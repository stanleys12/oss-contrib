"""Pre-submission compliance check for a prepared change: deterministic checks + an independent compliance agent."""
from __future__ import annotations

import json
import os
import shutil
import re
import subprocess
import time
from pathlib import Path

from . import config, db, log
from .analyzer import live_path, workspace_path, assert_workspace_free, _default_branch, osc_dir, _run
from .claude_runner import run_claude, extract_json
from .contributor import _git
from .quality import lint_change
from .schemas import COMPLIANCE_SCHEMA

STAGE = "comply"


def _gh(args: list[str], cwd: Path | None = None) -> str:
    env = {**os.environ, "GH_TOKEN": config.github_token()}
    return subprocess.run(["gh", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, env=env, timeout=120).stdout


def deterministic_checks(c: dict, repo: dict, path: Path) -> list[dict]:
    items: list[dict] = []
    raw = repo.get("raw") or {}

    def add(rule, ok, evidence, fix=""):
        items.append({"rule": rule, "status": "pass" if ok is True else "fail" if ok is False else "unclear", "evidence": evidence[:400], "fix": fix, "source": "deterministic"})

    owner, name = c["repo"].split("/")
    me = (c.get("fork_url") or "").rstrip("/").split("/")[-2] if c.get("fork_url") else None
    head = _git(path, "rev-parse", c["branch"]).strip()
    # 1. fork branch == local head
    if me:
        sha = _gh(["api", f"repos/{me}/{name}/branches/{c['branch']}", "-q", ".commit.sha"]).strip()
        add("fork branch matches local head", sha == head, f"fork={sha[:10]} local={head[:10]}", "click Re-push to fork")
    else:
        add("branch pushed to fork", False, "no fork_url", "click Prepare")
    # 2. merges cleanly onto current upstream default branch
    base = _default_branch(path)
    _run(["git", "fetch", "-q", "--depth", "50", "origin", base], cwd=path, timeout=600)
    behind = _run(["git", "rev-list", "--count", f"{c['base_sha']}..FETCH_HEAD"], cwd=path).stdout.strip()
    mt = _run(["git", "merge-tree", "--write-tree", "FETCH_HEAD", c["branch"]], cwd=path)
    add(f"merges cleanly into current upstream {base}", mt.returncode == 0, f"upstream moved {behind} commits since base; merge-tree rc={mt.returncode}",
        f"rebase branch onto origin/{base} and resolve conflicts" if mt.returncode else "")
    # 3. duplicates: open PRs referencing the linked issues or touching the same files
    issues = c.get("related_issues") or []
    dup_hits = []
    for n in issues:
        out = _gh(["pr", "list", "--repo", c["repo"], "--state", "open", "--search", f"#{n} in:body", "--json", "number,title,author", "--limit", "10"])
        try:
            for pr in json.loads(out or "[]"):
                if (pr.get("author") or {}).get("login") != me:
                    dup_hits.append(f"#{pr['number']} {pr['title'][:60]} (refs #{n})")
        except Exception:
            pass
    files = (c.get("diff_stats") or {}).get("files") or []
    for f in [f for f in files if not re.search(r"test|changelog|docs/", f)][:3]:
        out = _gh(["pr", "list", "--repo", c["repo"], "--state", "open", "--search", f"{os.path.basename(f)} in:title", "--json", "number,title,author", "--limit", "5"])
        try:
            for pr in json.loads(out or "[]"):
                if (pr.get("author") or {}).get("login") != me:
                    dup_hits.append(f"#{pr['number']} {pr['title'][:60]} (file {os.path.basename(f)})")
        except Exception:
            pass
    add("no open PR by someone else for the same issue/files", None if dup_hits else True, "; ".join(dup_hits[:6]) or "none found", "read the listed PRs; if one covers this fix, do not submit")
    # 4. linked issue state: assignee / closed / new claims
    for n in issues:
        out = _gh(["issue", "view", str(n), "--repo", c["repo"], "--json", "state,assignees,labels,comments"])
        try:
            d = json.loads(out)
            assignees = [a["login"] for a in d.get("assignees", [])]
            labels = [l["name"] for l in d.get("labels", [])]
            recent = [cm for cm in d.get("comments", []) if cm.get("author", {}).get("login") != me and re.search(r"\b(i('| a)m|i will|i'll|working on|assign(ed)? to me|take this|taking this)\b", cm.get("body", ""), re.I)]
            add(f"issue #{n} still open and unclaimed", d.get("state") == "OPEN" and not assignees and not recent,
                f"state={d.get('state')} assignees={assignees} labels={labels} claim-comments={len(recent)}",
                "if someone else claimed it, coordinate on the issue before opening the PR")
        except Exception as e:
            add(f"issue #{n} readable", None, str(e)[:100])
    # 5. PR body vs template headings
    tpl = ""
    for t in (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md"):
        if (path / t).exists():
            tpl = (path / t).read_text(errors="ignore"); break
    body = c.get("pr_body") or ""
    if tpl:
        heads = [h.strip() for h in re.findall(r"^#{1,4}\s+(.+)$", tpl, re.M)]
        missing = [h for h in heads if h.lower() not in body.lower()]
        add("PR body contains every PR-template section", not missing, f"template sections={heads}; missing={missing}", f"add sections: {missing}")
        boxes = re.findall(r"^\s*-\s*\[[ x]\]\s*(.+)$", tpl, re.M)
        unfilled = [b for b in boxes if b.strip()[:40].lower() in body.lower() and re.search(r"-\s*\[ \]\s*" + re.escape(b.strip()[:25]), body)]
        if boxes:
            add("template checkboxes addressed", not unfilled, f"unticked boxes: {unfilled[:5]}", "tick each box that is true; delete boxes that don't apply if the template allows")
    else:
        add("PR template", True, "repo has no PR template")
    # 6. disclosure
    pol = raw.get("ai_policy") or ""
    if pol:
        ok = bool(re.search(r"\b(ai|llm|claude|copilot|assist)", body, re.I))
        add("AI-assistance disclosure present (repo policy)", ok, pol[:200], "add an explicit AI-assistance statement to the PR body")
    if raw.get("ai_prose_human"):
        add("PR text must be written by the human", None, raw["ai_prose_human"][:200], "rewrite title/body/commit message yourself before submitting")
    # 7. commit hygiene
    log_ = _git(path, "log", "--format=%H%x00%s%x00%b%x00%an <%ae>%x01", f"{c['base_sha']}..{c['branch']}")
    commits = [rec.strip().split("\x00") for rec in log_.split("\x01") if rec.strip()]
    commits = [cm for cm in commits if len(cm) >= 3]
    add("single focused commit (or a clean series)", len(commits) <= 3, f"{len(commits)} commits", "squash with git rebase -i")
    if repo.get("dco_required"):
        missing = [cm[1][:40] for cm in commits if "Signed-off-by:" not in cm[2]]
        add("DCO sign-off on every commit", not missing, f"unsigned: {missing}" if missing else "all signed", "git commit --amend -s")
    subj = [cm[1] for cm in commits]
    add("no issue numbers in commit subjects", not any(re.search(r"#\d+", s) for s in subj), "; ".join(subj)[:200], "reword commit to drop #N (keep it in the PR body)")
    recent = _git(path, "log", "--format=%s", "-40", f"origin/{base}").splitlines()
    conv = sum(1 for s in recent if re.match(r"^(\[[^\]]+\]|\w+(\([^)]+\))?:)", s)) / max(1, len(recent))
    mine = all(re.match(r"^(\[[^\]]+\]|\w+(\([^)]+\))?:)", s) for s in subj)
    add("commit subject follows the repo's prefix convention", (mine if conv >= 0.6 else True), f"{conv:.0%} of recent upstream commits use a prefix; ours={'prefixed' if mine else 'plain'}", "reword subject to match git log style")
    if repo.get("cla_required"):
        add("CLA", None, "repo requires a CLA; bot will ask on the PR", "sign when the bot comments")
    # 8. tests re-run: run the repo's test runner directly on the touched test files (recorded commands often carry prose)
    test_files = [f for f in files if re.search(r"(^|/)tests?/.*\.py$|_test\.py$|^test_", f)]
    venv_py = path / ".venv" / "bin" / "python"
    if test_files and venv_py.exists():
        runner = ["-m", "pytest", "-q", "-x", *test_files] if (path / "pyproject.toml").exists() or (path / "pytest.ini").exists() or (path / "setup.cfg").exists() else ["-m", "unittest", *[f[:-3].replace("/", ".") for f in test_files]]
        if (path / "python").is_dir() and not (path / "pyproject.toml").exists():
            runner = ["-m", "pytest", "-q", "-x", *test_files]
        env = {**os.environ, "PYTHONPATH": ":".join(x for x in (str(path / "python") if (path / "python").is_dir() else "", str(path), os.environ.get("PYTHONPATH", "")) if x)}
        _git(path, "checkout", "-q", c["branch"])
        try:
            r = subprocess.run([str(venv_py), *runner], cwd=str(path), capture_output=True, text=True, timeout=1200, env=env)
            tail = (r.stdout + r.stderr)[-700:]
            add("tests pass on the branch (re-run now)", r.returncode == 0, f"$ {venv_py.name} {' '.join(runner)[:150]}\n{tail}", "fix failing tests")
        except Exception as e:
            add("tests pass on the branch (re-run now)", None, f"could not run: {e}"[:200])
        finally:
            _git(path, "checkout", "-q", base, check=False)
    else:
        add("tests re-run", None, "no touched test files or no workspace venv", "run the repo's tests for the touched files manually")
    # 9. formatter on touched python files
    py = [f for f in files if f.endswith(".py")]
    if py:
        venv_bin = path / ".venv" / "bin"
        _git(path, "checkout", "-q", c["branch"])
        try:
            results = []
            for tool, args in (("ruff", ["format", "--check"]), ("ruff", ["check"]), ("black", ["--check"])):
                exe = venv_bin / tool
                cfg = (path / "pyproject.toml").read_text(errors="ignore") if (path / "pyproject.toml").exists() else ""
                if exe.exists() and (tool in cfg or (path / f".{tool}.toml").exists() or (path / f"{tool}.toml").exists()):
                    r = subprocess.run([str(exe), *args, *py], cwd=str(path), capture_output=True, text=True, timeout=300)
                    results.append((tool + " " + args[0], r.returncode == 0, (r.stdout + r.stderr)[-200:]))
            if not results:
                our_ruff = config.VENV_BIN / "ruff"
                pc = (path / ".pre-commit-config.yaml").read_text(errors="ignore") if (path / ".pre-commit-config.yaml").exists() else ""
                uses_black = "id: black" in pc or "[tool.black]" in ((path / "pyproject.toml").read_text(errors="ignore") if (path / "pyproject.toml").exists() else "") and "ruff-format" not in pc
                if uses_black:
                    m = re.search(r"psf/black[^\n]*\n\s*rev:\s*['\"]?v?([\d.]+)", pc)
                    black_cmd = ["uvx", "--quiet", f"black@{m.group(1)}"] if m and shutil.which("uvx") else [str(config.VENV_BIN / "black")]
                    r = subprocess.run([*black_cmd, "--check", *py], cwd=str(path), capture_output=True, text=True, timeout=600)
                    results.append((f"black --check ({'pinned ' + m.group(1) if m else 'osc black'})", r.returncode == 0, (r.stdout + r.stderr)[-200:]))
                else:
                    r = subprocess.run([str(our_ruff), "format", "--check", *py], cwd=str(path), capture_output=True, text=True, timeout=300)
                    results.append(("ruff format (osc ruff, repo config)", r.returncode == 0, (r.stdout + r.stderr)[-200:]))
                # lint: only NEW findings count (pre-existing noise in large files is not ours to fix)
                def _count(sha):
                    r2 = subprocess.run([str(our_ruff), "check", "--output-format", "concise", *[f for f in py if _run(["git", "cat-file", "-e", f"{sha}:{f}"], cwd=path).returncode == 0]],
                                        cwd=str(path), capture_output=True, text=True, timeout=300)
                    return len([l for l in r2.stdout.splitlines() if re.match(r"^\S+:\d+:\d+:", l)])
                _git(path, "checkout", "-q", c["base_sha"], check=False); base_n = _count(c["base_sha"])
                _git(path, "checkout", "-q", c["branch"], check=False); head_n = _count(head)
                results.append((f"ruff check new findings (base {base_n} → branch {head_n})", head_n <= base_n, f"{head_n - base_n:+d} findings"))
            if results:
                bad = [x for x in results if not x[1]]
                add("repo formatter/linter clean on touched files", not bad, "; ".join(f"{t}: {'ok' if ok else out}" for t, ok, out in results)[:400], "run the formatter and amend")
            else:
                add("repo formatter/linter", None, "no configured tool found in the workspace venv", "run pre-commit manually")
        finally:
            _git(path, "checkout", "-q", base, check=False)
    # 10. our AI-pattern lint
    q = lint_change(path, c["base_sha"], head, c.get("pr_title") or "", body, ai_policy=pol)
    add("AI-pattern lint ≥ gate", q["score"] >= config.setting("MIN_QUALITY_SCORE"), f"score {q['score']}: {[f['rule'] for f in q['findings']]}", "fix findings")
    return items


def run_agent(c: dict, repo: dict, path: Path, out: Path) -> tuple[dict, float]:
    base = _default_branch(path)
    _git(path, "checkout", "-q", c["branch"])
    try:
        diff = _git(path, "diff", f"{c['base_sha']}...HEAD")
        commits = _git(path, "log", "--format=%h %s%n%b", f"{c['base_sha']}..HEAD")
        tests = "\n".join(f"- `{t.get('command')}` → {t.get('result')}" for t in (c.get("tests_run") or [])) or "(none)"
        p = (config.PROMPTS_DIR / "compliance.md").read_text()
        p = (p.replace("__REPO__", c["repo"]).replace("__BASE__", base).replace("__BRANCH__", c["branch"]).replace("__PR_TITLE__", c.get("pr_title") or "")
             .replace("__PR_BODY__", (c.get("pr_body") or "")[:9000]).replace("__COMMITS__", commits[:3000]).replace("__DIFF__", diff[:60000]).replace("__TESTS__", tests))
        res = run_claude(p, path, model=config.setting("REVIEWER_MODEL"), max_turns=60, stage=STAGE, repo=c["repo"], json_schema=COMPLIANCE_SCHEMA,
                         allowed_tools=["Read", "Grep", "Glob", "LS", "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git grep:*)", "Bash(gh pr:*)",
                                        "Bash(gh issue:*)", "Bash(gh api:*)", "Bash(gh search:*)", "Bash(cat:*)", "Bash(ls:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(head:*)", "Bash(sed -n:*)"],
                         max_budget_usd=float(config.setting("COMPLIANCE_BUDGET_USD")), transcript_path=out / "compliance.jsonl", timeout_s=2400)
        data = res.structured if isinstance(res.structured, dict) else extract_json(res.text)
        if not isinstance(data, dict) or "items" not in data:
            data = {"items": [], "blockers": [f"compliance agent failed: {res.error[:200]}"], "merge_likelihood": 0, "live_changes": "", "summary": "agent failed"}
        return data, res.cost_usd
    finally:
        _git(path, "checkout", "-q", base, check=False)


def check_change(cid: str, agent: bool = True) -> dict:
    c = db.parse_json_fields(db.row("SELECT * FROM changes WHERE id=?", (cid,)), ["tests_run", "diff_stats", "handoff"])
    opp = db.parse_json_fields(db.row("SELECT related_issues FROM opportunities WHERE id=?", (c["opportunity_id"],)), ["related_issues"])
    c["related_issues"] = opp.get("related_issues") or []
    repo = db.parse_json_fields(db.row("SELECT * FROM repos WHERE full_name=?", (c["repo"],)), ["raw"])
    path = live_path(c["repo"]); out = osc_dir(c["repo"]) / cid
    assert_workspace_free(path)
    log.info(STAGE, f"compliance check for {cid}", repo=c["repo"])
    det = deterministic_checks(c, repo, path)
    result = {"deterministic": det, "checked_at": time.time()}
    if agent:
        data, cost = run_agent(c, repo, path, out)
        result["agent"] = data
        result["cost_usd"] = cost
    fails = [i for i in det if i["status"] == "fail"] + [i for i in (result.get("agent") or {}).get("items", []) if i["status"] == "fail"]
    result["fails"] = len(fails)
    result["merge_likelihood"] = (result.get("agent") or {}).get("merge_likelihood")
    db.update("changes", "id", cid, {"compliance": result, "updated_at": time.time()})
    (out / "compliance.json").write_text(json.dumps(result, indent=2))
    lvl = log.ok if not fails else log.warn
    lvl(STAGE, f"{cid}: {len(fails)} failing checks, merge likelihood {result['merge_likelihood']}", repo=c["repo"], data={"fails": [f["rule"] for f in fails]})
    return result
