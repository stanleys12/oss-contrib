"""Flagship polish pass: builder with prompts/polish.md as feedback, then lint + reviewer. Usage: polish.py <change_id>"""
import json, subprocess, sys, time
from pathlib import Path
from osc import db, config, log, quality
from osc.analyzer import workspace_path, assert_workspace_free, _default_branch
from osc.contributor import _load_change, run_builder, run_reviewer, osc_dir, _git, _diff_stats, _run

cid = sys.argv[1]
c, opp, repo = _load_change(cid)
path = workspace_path(c["repo"]); out = osc_dir(c["repo"]) / cid
assert_workspace_free(path)
base_branch = _default_branch(path)
polish = (config.PROMPTS_DIR / "polish.md").read_text().replace("__REPO__", c["repo"])
prev = c.get("review") or {}
feedback = polish
extra = out / "polish_extra.md"
if extra.exists():
    feedback += "\n\n### Extra context from the operator\n" + extra.read_text()
if prev.get("required_fixes") or prev.get("slop_findings"):
    feedback += "\n\n### Outstanding reviewer notes from the last round\n" + "\n".join(f"- {f}" for f in (prev.get("required_fixes") or []) + (prev.get("slop_findings") or []))
_git(path, "checkout", "-q", c["branch"])
rnd = int(c.get("review_round") or 0) + 1
log.info("build", f"POLISH pass on {cid} (round {rnd}, opus, up to 400 turns)", repo=c["repo"])
db.update("changes", "id", cid, {"status": "building", "updated_at": time.time()})
old_turns = config.setting("BUILDER_MAX_TURNS")
try:
    import os
    os.environ["OSC_BUILDER_MAX_TURNS"] = "400"
    build, bcost = run_builder(opp, repo, path, out, c["branch"], feedback, "opus")
    if _git(path, "status", "--porcelain").strip():
        _git(path, "add", "-A", "--", ".", ":!.venv", ":!venv", ":!node_modules")
        _run(["git", "commit", "-q", "-m", build.get("pr_title") or c["pr_title"] or opp["title"]], cwd=path)
    head = _git(path, "rev-parse", "HEAD").strip(); diff = _git(path, "diff", f"{c['base_sha']}...{head}")
    pr_title = build.get("pr_title") or c["pr_title"]; pr_body = build.get("pr_body") or c["pr_body"]
    q = quality.lint_change(path, c["base_sha"], head, pr_title or "", pr_body or "", ai_policy=str((repo.get("raw") or {}).get("ai_policy") or ""))
    build2 = {"pr_title": pr_title, "pr_body": pr_body, "summary": build.get("summary") or c["summary"], "why": build.get("why") or c["why"],
              "tests_run": build.get("tests_run") or c["tests_run"], "limitations": build.get("limitations") or c["limitations"]}
    review, rcost = run_reviewer(opp, repo, path, out, c["branch"], c["base_sha"], build2, diff, rnd, config.setting("REVIEWER_MODEL"), q)
    ok = review.get("verdict") == "approve" and q["score"] >= config.setting("MIN_QUALITY_SCORE")
    status = "ready" if ok else ("rejected" if review.get("verdict") == "reject" else "needs_work")
    handoff = c.get("handoff") or {}
    for k in ("pre_pr_steps", "issue_comment", "post_pr_notes"):
        if build.get(k): handoff[k] = build[k]
    st = _diff_stats(diff)
    db.update("changes", "id", cid, {"head_sha": head, "diff": diff, "diff_stats": st, "files_changed": st["files"], "pr_title": pr_title, "pr_body": pr_body,
        "summary": build2["summary"], "why": build2["why"], "tests_run": build2["tests_run"], "limitations": build2["limitations"], "handoff": handoff,
        "quality": q, "review": review, "review_score": review.get("score"), "review_verdict": review.get("verdict"), "review_round": rnd,
        "cost_usd": round((c["cost_usd"] or 0) + bcost + rcost, 3), "status": status,
        "status_note": f"polish pass (round {rnd}): {review.get('verdict')} {review.get('score')}/10, reads-human {review.get('reads_human')}, AI-lint {q['score']}", "updated_at": time.time()})
    (out / "change.diff").write_text(diff); (out / "polish_summary.md").write_text(build2["summary"] or "")
    print(json.dumps({"change": cid, "status": status, "verdict": review.get("verdict"), "score": review.get("score"), "reads_human": review.get("reads_human"),
                      "lint": q["score"], "diff_lines": st["total"], "fixes": review.get("required_fixes"), "cost": round(bcost + rcost, 2)}, indent=1))
finally:
    _git(path, "checkout", "-q", base_branch, check=False)
