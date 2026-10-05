"""Regenerate data/reports/PLAYBOOK.md from the current DB state."""
import json, time
from urllib.parse import quote
from osc import db
rows=[db.parse_json_fields(r,["handoff","diff_stats","review","compliance","quality"]) for r in db.rows("SELECT * FROM changes WHERE status IN ('prepared','submitted','ready') ORDER BY CASE status WHEN 'submitted' THEN 1 ELSE 0 END, json_extract(compliance,'$.merge_likelihood') DESC, review_score DESC")]
out=["# Contribution Playbook (generated %s)\n"%time.strftime("%Y-%m-%d %H:%M"),
     "Ordered: not-yet-opened first (by merge likelihood), then already-open PRs. Use ONE GitHub account (stanleys12) for everything.\n"]
for i,c in enumerate(rows,1):
    repo=db.parse_json_fields(db.row("SELECT stars, cla_required, dco_required, raw FROM repos WHERE full_name=?",(c["repo"],)),["raw"]); raw=repo["raw"] or {}
    opp=db.parse_json_fields(db.row("SELECT title, kind, related_issues FROM opportunities WHERE id=?",(c["opportunity_id"],)),["related_issues"]) or {}
    h=c.get("handoff") or {}; cp=c.get("compliance") or {}; ag=cp.get("agent") or {}; me="stanleys12"; owner,name=c["repo"].split("/"); base=raw.get("default_branch") or "main"
    st=c.get("diff_stats") or {}
    out.append(f"\n---\n\n## {i}. {c['repo']} ({repo['stars']//1000}k★) — {opp.get('kind')}: {c['pr_title']}  [{c['status'].upper()}]\n")
    if c.get("pr_url"): out.append(f"- PR: {c['pr_url']}")
    out.append(f"- Compliance merge-likelihood **{cp.get('merge_likelihood','-')}/10** · review {c['review_score']}/10 · AI-lint {(c.get('quality') or {}).get('score','-')} · {st.get('total')} lines in {len(st.get('files',[]))} files")
    if ag.get("summary"): out.append(f"- Agent summary: _{ag['summary'][:400]}_")
    if raw.get("ai_prose_human"): out.append(f"- ⚠️ Repo forbids AI-written PR text: write the title/description/commit message yourself.")
    if raw.get("no_ai_trailer"): out.append(f"- Repo CI rejects AI co-author trailers ({raw['no_ai_trailer']}); commit has none.")
    if repo["cla_required"]: out.append("- CLA required (bot comments after opening; sign once).")
    if repo["dco_required"]: out.append("- DCO required: commit is signed off.")
    if c["status"]=="submitted":
        out.append("\n**Status**: open. Answer reviewers in your own words; for requested code changes, use Revise on the dashboard."); 
        for s in h.get("post_pr_notes") or []: out.append(f"- {s}")
        continue
    pre=f"https://github.com/{owner}/{name}/compare/{base}...{me}:{name}:{quote(c['branch'])}?expand=1&title={quote(c['pr_title'] or '')}&body={quote(c['pr_body'] or '')}"
    open(f"data/reports/{owner}__{name}/{c['id']}/one_click_pr_url.txt","w").write(pre); open(f"data/reports/{owner}__{name}/{c['id']}/pr_body.md","w").write(c["pr_body"] or "")
    out.append("\n**Steps**\n"); n=0
    def step(t):
        global n; n+=1; out.append(f"{n}. {t}")
    for s in h.get("pre_pr_steps") or []: step(s)
    step(f"Read the diff: http://127.0.0.1:8791/#/changes/{c['id']}/diff")
    if raw.get("ai_prose_human"):
        step(f"Rewrite the commit message yourself, Re-push to fork, then open https://github.com/{owner}/{name}/compare/{base}...{me}:{name}:{c['branch']}?expand=1 and write the title/description yourself.")
    else:
        step(f"Open the pre-filled PR form (`data/reports/{owner}__{name}/{c['id']}/one_click_pr_url.txt` or the dashboard PR tab) → check base `{base}`, head `{me}:{c['branch']}` → Create pull request.")
    iss=opp.get("related_issues") or []
    if h.get("issue_comment") and iss:
        step("Right after opening, post on " + ", ".join(f"https://github.com/{c['repo']}/issues/{x}" for x in iss) + " (replace <PR number>):\n\n```\n" + h["issue_comment"].strip() + "\n```")
    step(f"Paste the PR URL into Mark as submitted on http://127.0.0.1:8791/#/changes/{c['id']}/pr")
    for s in h.get("post_pr_notes") or []: step("After opening: " + s)
parked=db.rows("SELECT repo, status_note FROM changes WHERE status IN ('needs_work','rejected') ORDER BY repo")
out.append("\n---\n\n## Not submitted (with reasons)\n"); out += [f"- **{p['repo']}**: {p['status_note']}" for p in parked]
open("data/reports/PLAYBOOK.md","w").write("\n".join(out)); print("PLAYBOOK.md regenerated:", len(rows), "entries")
