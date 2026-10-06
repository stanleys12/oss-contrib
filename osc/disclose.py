"""Responsible disclosure (user goal 2026-10-06, Security advisory credit badge + correct security practice).

When the pipeline builds a security fix, this decides whether the flaw is already public (normal public PR)
or a genuine not-yet-public vulnerability. For the latter it does NOT open a public PR (which would hand
attackers the exploit); it drafts a private report, checks the repo accepts private vulnerability reports,
and queues it for the user. A merged/published advisory credits the reporter.

Nothing is sent unless DISCLOSE_AUTO_SUBMIT is true or submit(..., force=True); the draft always goes to the
user first. Security is sensitive: this never includes a working exploit, and never auto-sends.

  python -m osc.disclose --classify <change_id>
  python -m osc.disclose --submit <change_id>     # gated: opens the private report
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, db, log
from .claude_runner import extract_json, run_claude
from .daily import PROJECT, send_digest

STAGE = "disclose"
MARKER = "SECURITY-DISCLOSURE"
STATE_F = PROJECT / "data" / "disclose_state.json"

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["public_pr", "private", "unsure"]},
        "reason": {"type": "string"},
        "already_public": {"type": "string", "description": "URL of the public disclosure, if any."},
        "report_summary": {"type": "string"},
        "report_details": {"type": "string"},
        "affected_versions": {"type": "string"},
        "cwe": {"type": "string"},
        "severity": {"type": "string"},
        "fix_note": {"type": "string"},
    },
    "required": ["decision", "reason"],
}


def _gh(*args: str, timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GH_TOKEN": config.github_token(), "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def _state() -> dict:
    try:
        st = json.loads(STATE_F.read_text())
    except Exception:
        st = {}
    st.setdefault("reports", {})
    return st


def _save(st: dict) -> None:
    STATE_F.write_text(json.dumps(st, indent=1))


def pvr_enabled(repo: str) -> bool:
    rc, out = _gh("api", f"repos/{repo}/private-vulnerability-reporting", "-q", ".enabled")
    return rc == 0 and out.strip() == "true"


def classify(cid: str) -> dict:
    c = db.parse_json_fields(db.row("SELECT * FROM changes WHERE id=?", (cid,)), ["diff_stats"])
    if not c:
        raise RuntimeError("unknown change")
    opp = db.parse_json_fields(db.row("SELECT * FROM opportunities WHERE id=?", (c["opportunity_id"],)), ["related_issues"]) or {}
    if (opp.get("kind") or "") != "security":
        return {"decision": "public_pr", "reason": "not a security-kind opportunity"}
    diff = (c.get("diff") or "")[:12000]
    p = (config.PROMPTS_DIR / "disclose.md").read_text()
    for k, v in {"__REPO__": c["repo"], "__TITLE__": c.get("pr_title") or "", "__SUMMARY__": (c.get("summary") or "")[:1500],
                 "__WHY__": (c.get("why") or "")[:1500], "__ISSUES__": ", ".join(f"#{n}" for n in (opp.get("related_issues") or [])) or "none",
                 "__DIFF__": diff}.items():
        p = p.replace(k, v)
    res = run_claude(p, config.ROOT, model=config.setting("DISCLOSE_MODEL"), max_turns=30, stage=STAGE, repo=c["repo"],
                     json_schema=SCHEMA, max_budget_usd=float(config.setting("DISCLOSE_BUDGET_USD")), timeout_s=1200,
                     allowed_tools=["Bash(gh api:*)", "Bash(gh search:*)", "Bash(gh issue:*)", "WebFetch", "WebSearch"])
    d = res.structured if isinstance(res.structured, dict) else extract_json(res.text) or {"decision": "unsure", "reason": "no answer"}
    d["cost"] = res.cost_usd
    return d


def handle_security(cid: str) -> dict:
    """Called for an approved security change before it would be opened. Returns the routing decision; marks
    the change and queues a private draft when disclosure should be private. Never sends anything."""
    st = _state()
    c = db.row("SELECT repo, pr_title FROM changes WHERE id=?", (cid,))
    try:
        d = classify(cid)
    except Exception as e:
        log.warn(STAGE, f"{cid}: classify failed: {e}")
        return {"decision": "public_pr", "reason": f"classify failed: {e}"}
    if d["decision"] == "public_pr":
        return d
    # private or unsure: keep it OUT of the public-PR path
    note = (db.row("SELECT status_note FROM changes WHERE id=?", (cid,)) or {}).get("status_note") or ""
    pvr = pvr_enabled(c["repo"]) if d["decision"] == "private" else None
    tag = f"{MARKER} ({d['decision']}): {d.get('reason', '')[:200]}"
    db.update("changes", "id", cid, {"status_note": (note + "\n" + tag).strip(), "updated_at": time.time()})
    st["reports"][cid] = {"ts": time.time(), "repo": c["repo"], "title": c["pr_title"], "pvr": pvr, "submitted": "", **d}
    _save(st)
    where = "private vulnerability report" if d["decision"] == "private" else "your review (unsure if already public)"
    log.warn(STAGE, f"{cid}: {c['repo']} routed to {where}; PVR={'on' if pvr else 'off' if pvr is not None else '?'}", repo=c["repo"])
    send_digest(f"[oss-contrib] security finding needs you: {c['repo']}",
                f"A security fix the pipeline built looks like it should be disclosed privately, not opened as a public PR.\n\n"
                f"Repo: {c['repo']}\nChange: {cid}\nDecision: {d['decision']} ({d.get('reason', '')})\n"
                f"Private reporting enabled on the repo: {'yes' if pvr else 'no' if pvr is not None else 'unknown'}\n\n"
                f"Draft summary: {d.get('report_summary', '')}\nSeverity: {d.get('severity', '')}\n\n"
                f"Nothing was sent. Review the draft in data/disclose_state.json, then say \"submit {cid}\" to send the private report.\n")
    return d


def submit(cid: str, force: bool = False) -> dict:
    st = _state()
    r = st["reports"].get(cid)
    if not r:
        raise RuntimeError(f"no disclosure draft for {cid}; run classify first")
    if r["decision"] != "private":
        raise RuntimeError(f"draft is '{r['decision']}', not a private report")
    if r.get("submitted"):
        return {"cid": cid, "submitted": True, "url": r["submitted"], "note": "already sent"}
    if not (config.setting("DISCLOSE_AUTO_SUBMIT") or force):
        return {"cid": cid, "submitted": False, "why": "DISCLOSE_AUTO_SUBMIT is off; pass force to send"}
    if not pvr_enabled(r["repo"]):
        return {"cid": cid, "submitted": False, "why": "repo does not accept private vulnerability reports; contact the maintainers another way"}
    body = (r.get("report_details", "") +
            f"\n\nAffected: {r.get('affected_versions', 'unknown')}" +
            (f"\nCWE: {r['cwe']}" if r.get("cwe") else "") +
            f"\n\n{r.get('fix_note', '')}").strip()
    home = str(Path.home())
    if config.github_token() in body or home in body:
        raise RuntimeError("draft contains a secret or local path; not sending")
    payload = json.dumps({"summary": r.get("report_summary", r["title"])[:1024], "description": body,
                          "severity": (r.get("severity", "") or "").split()[0].lower() if r.get("severity") else None})
    p = subprocess.run(["gh", "api", "-X", "POST", f"repos/{r['repo']}/security-advisories/reports", "--input", "-", "-q", ".html_url"],
                       input=payload, capture_output=True, text=True, env={**os.environ, "GH_TOKEN": config.github_token()}, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"private report failed: {(p.stderr or p.stdout)[:200]}")
    url = p.stdout.strip()
    r["submitted"] = url
    _save(st)
    log.ok(STAGE, f"{cid}: private report sent to {r['repo']}: {url}", repo=r["repo"])
    return {"cid": cid, "submitted": True, "url": url}


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--classify" in a:
        print(json.dumps(classify(a[a.index("--classify") + 1]), indent=1, default=str))
    elif "--submit" in a:
        print(json.dumps(submit(a[a.index("--submit") + 1], force="--force" in a), indent=1, default=str))
