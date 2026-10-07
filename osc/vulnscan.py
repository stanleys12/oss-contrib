"""Vulnerability research + coordinated disclosure (user goal 2026-10-07).

Picks an open-source project that accepts private vulnerability reports, has an agent do defensive security
analysis of the LOCAL checkout only, write an offline proof-of-concept, and confirm the flaw is not already
public. A second agent adversarially reproduces it. A confirmed, non-public, reproducible finding is drafted
as a private report, emailed to the user, and only sent after the user confirms.

Safety, by construction:
- All analysis and verification run against the local clone. The prompts forbid touching any live/external
  system, and forbid weaponized exploits. The point is to help maintainers fix flaws before they are exploited.
- Nothing is reported without VULNSCAN_AUTO_SUBMIT (default False) or an explicit submit; false positives are
  filtered by the adversarial verifier, which has to reproduce the flaw before it is confirmed.

  python -m osc.vulnscan --repo <owner/name>    # research one repo (no report sent)
  python -m osc.vulnscan --scan [N]             # research N PVR-enabled candidates
  python -m osc.vulnscan --list                 # show findings awaiting your review
  python -m osc.vulnscan --report <id>          # send the private report (gated)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, db, log
from .analyzer import ensure_clone, osc_dir
from .claude_runner import extract_json, run_claude
from .daily import DENY, PROJECT, needs_gpu, send_digest
from .disclose import pvr_enabled

STAGE = "vulnscan"
STATE_F = PROJECT / "data" / "vulnscan_state.json"

VULN_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "title": {"type": "string"}, "vuln_class": {"type": "string"}, "where": {"type": "string"},
        "impact": {"type": "string"}, "reachable": {"type": "string"}, "local_verification": {"type": "string"},
        "affected_versions": {"type": "string"}, "cwe": {"type": "string"}, "severity": {"type": "string"},
        "not_public": {"type": "string"}, "report_body": {"type": "string"}, "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["found"],
}
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["confirmed", "fix", "reject"]},
                   "problems": {"type": "array", "items": {"type": "string"}},
                   "report_body": {"type": "string"}, "severity": {"type": "string"}},
    "required": ["verdict"],
}

RESEARCH_TOOLS = ["Read", "Grep", "Glob", "LS", "Bash(git log:*)", "Bash(git show:*)", "Bash(git grep:*)", "Bash(git blame:*)",
                  "Bash(gh api:*)", "Bash(gh issue:*)", "Bash(gh pr:*)", "Bash(gh search:*)", "WebFetch", "WebSearch",
                  "Bash(python:*)", "Bash(python3:*)", "Bash(uv:*)", "Bash(.venv/bin/*)", "Bash(node:*)", "Bash(npm:*)",
                  "Bash(go:*)", "Bash(cargo:*)", "Bash(cat:*)", "Bash(ls:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(sed:*)",
                  "Bash(mkdir:*)", "Write", "Edit"]


def _gh(*args: str, timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GH_TOKEN": config.github_token()})
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def _state() -> dict:
    try:
        st = json.loads(STATE_F.read_text())
    except Exception:
        st = {}
    st.setdefault("findings", {})
    st.setdefault("scanned", {})
    return st


def _save(st: dict) -> None:
    tmp = STATE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE_F)


def candidates(n: int) -> list[str]:
    """PVR-enabled, active, non-GPU repos in our security-adjacent domains, least-recently-scanned first."""
    from .scanner import rank
    st = _state()
    doms = ["security", "security-vendors", "agents-mcp", "llm-inference", "crypto-pq"]
    seen, out = set(), []
    for dom in doms:
        for r in rank(limit=120, domain=dom):
            full = r["full_name"]
            if full in seen or full in DENY or r.get("archived") or needs_gpu(r):
                continue
            seen.add(full)
            out.append(full)
    out.sort(key=lambda f: st["scanned"].get(f, 0))
    picked = []
    for full in out:
        if len(picked) >= n:
            break
        if pvr_enabled(full):          # only projects that invite private reports
            picked.append(full)
        else:
            st["scanned"][full] = time.time()      # no PVR: skip and don't recheck soon
    _save(st)
    return picked


def scan(repo: str) -> dict:
    db.init()
    st = _state()
    st["scanned"][repo] = time.time()
    _save(st)
    path = ensure_clone(repo)
    out = osc_dir(repo) / "vulnscan"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = (config.PROMPTS_DIR / "vulnscan.md").read_text().replace("__REPO__", repo)
    res = run_claude(p, path, model=config.setting("VULNSCAN_MODEL"), max_turns=int(config.setting("VULNSCAN_MAX_TURNS")),
                     stage=STAGE, repo=repo, skip_permissions=True, json_schema=VULN_SCHEMA,
                     max_budget_usd=float(config.setting("VULNSCAN_BUDGET_USD")), transcript_path=out / f"scan_{stamp}.jsonl", timeout_s=5400,
                     allowed_tools=RESEARCH_TOOLS, disallowed_tools=["Bash(git push:*)", "Bash(gh pr create:*)", "Bash(gh repo fork:*)"],
                     append_system_prompt="Defensive security research. Analyze ONLY this local checkout. Never connect to or attack any live or "
                                          "external system. No weaponized exploits. Never push or open anything. Reset any throwaway test files when done.")
    a = res.structured if isinstance(res.structured, dict) else extract_json(res.text) or {"found": False, "reason": "no answer"}
    a["cost"] = res.cost_usd
    # discard any throwaway test files the agent left
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=str(path), capture_output=True)
    subprocess.run(["git", "clean", "-fdq", "-e", ".venv", "-e", "node_modules"], cwd=str(path), capture_output=True)
    if not a.get("found"):
        log.info(STAGE, f"{repo}: no confirmable vulnerability. {a.get('reason', '')[:200]}", repo=repo)
        return {"repo": repo, "found": False, "cost": a["cost"]}

    vp = (config.PROMPTS_DIR / "vulnscan_verify.md").read_text()
    for k, v in {"__REPO__": repo, "__TITLE__": str(a.get("title")), "__VULN_CLASS__": str(a.get("vuln_class")),
                 "__SEVERITY__": str(a.get("severity")), "__WHERE__": str(a.get("where"))[:1500],
                 "__REACHABLE__": str(a.get("reachable"))[:1500], "__IMPACT__": str(a.get("impact"))[:1500],
                 "__LOCAL_VERIFICATION__": str(a.get("local_verification"))[:2000], "__NOT_PUBLIC__": str(a.get("not_public"))[:1000],
                 "__REPORT_BODY__": str(a.get("report_body"))[:6000]}.items():
        vp = vp.replace(k, v)
    vr = run_claude(vp, path, model=config.setting("VULNSCAN_MODEL"), max_turns=int(config.setting("VULNSCAN_MAX_TURNS")),
                    stage="vulnscan-check", repo=repo, skip_permissions=True, json_schema=VERIFY_SCHEMA,
                    max_budget_usd=float(config.setting("VULNSCAN_VERIFY_BUDGET_USD")), transcript_path=out / f"verify_{stamp}.jsonl", timeout_s=3600,
                    allowed_tools=RESEARCH_TOOLS, disallowed_tools=["Bash(git push:*)", "Bash(gh pr create:*)", "Bash(gh repo fork:*)"],
                    append_system_prompt="Adversarial verification, local checkout only, never touch a live system, no weaponized exploits.")
    a["cost"] += vr.cost_usd
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=str(path), capture_output=True)
    subprocess.run(["git", "clean", "-fdq", "-e", ".venv", "-e", "node_modules"], cwd=str(path), capture_output=True)
    v = vr.structured if isinstance(vr.structured, dict) else extract_json(vr.text) or {"verdict": "reject", "problems": ["no verdict"]}
    if v["verdict"] == "reject":
        log.info(STAGE, f"{repo}: finding rejected by verifier: {v.get('problems')}", repo=repo)
        return {"repo": repo, "found": False, "rejected": v.get("problems"), "cost": a["cost"]}
    if v["verdict"] == "fix":
        if v.get("report_body"):
            a["report_body"] = v["report_body"]
        if v.get("severity"):
            a["severity"] = v["severity"]
    fid = f"vuln-{uuid.uuid4().hex[:8]}"
    a.update(id=fid, repo=repo, ts=time.time(), verdict=v["verdict"], pvr=pvr_enabled(repo), submitted="")
    st["findings"][fid] = a
    _save(st)
    log.ok(STAGE, f"{repo}: CONFIRMED {a.get('severity')} {a.get('vuln_class')} - {a.get('title')} ({fid})", repo=repo)
    send_digest(f"[oss-contrib] possible vulnerability found in {repo} (needs your review)",
                f"The vulnerability-research lane confirmed a finding. NOTHING has been sent to the maintainers.\n\n"
                f"Repo: {repo}\nFinding: {a.get('title')}\nClass: {a.get('vuln_class')} · severity {a.get('severity')}\n"
                f"Where: {a.get('where')}\nImpact: {a.get('impact')}\nVerified: {str(a.get('local_verification'))[:500]}\n"
                f"Private reporting enabled on the repo: {'yes' if a['pvr'] else 'no'}\n\n"
                f"Review the full finding and draft report in data/vulnscan_state.json ({fid}).\n"
                f"To send the private report once you're satisfied: python -m osc.vulnscan --report {fid}\n")
    return {"repo": repo, "found": True, "id": fid, "severity": a.get("severity"), "cost": a["cost"]}


def report(fid: str, force: bool = False) -> dict:
    st = _state()
    a = st["findings"].get(fid)
    if not a:
        raise RuntimeError(f"no finding {fid}")
    if a.get("submitted"):
        return {"id": fid, "submitted": True, "url": a["submitted"], "note": "already sent"}
    if not (config.setting("VULNSCAN_AUTO_SUBMIT") or force):
        return {"id": fid, "submitted": False, "why": "not sending without your go; run with --report (confirms) or set VULNSCAN_AUTO_SUBMIT"}
    repo = a["repo"]
    if not pvr_enabled(repo):
        return {"id": fid, "submitted": False, "why": "repo does not accept private vulnerability reports"}
    body = (a.get("report_body", "") + f"\n\nAffected: {a.get('affected_versions', 'unknown')}"
            + (f"\nCWE: {a['cwe']}" if a.get("cwe") else "")
            + "\n\nThis was found through good-faith security research and verified locally. A fix can be shared on request. "
              "Found with AI-assisted analysis.").strip()
    if config.github_token() in body or str(Path.home()) in body:
        raise RuntimeError("report contains a secret or local path; not sending")
    payload = json.dumps({"summary": a.get("title", "")[:1024], "description": body,
                          "severity": (a.get("severity", "") or "").split()[0].lower() or None})
    p = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/security-advisories/reports", "--input", "-", "-q", ".html_url"],
                       input=payload, capture_output=True, text=True, env={**os.environ, "GH_TOKEN": config.github_token()}, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"private report failed: {(p.stderr or p.stdout)[:200]}")
    a["submitted"] = p.stdout.strip()
    _save(st)
    log.ok(STAGE, f"{fid}: private report sent to {repo}: {a['submitted']}", repo=repo)
    send_digest(f"[oss-contrib] private vulnerability report sent to {repo}", f"{a['title']}\n{a['submitted']}\n")
    return {"id": fid, "submitted": True, "url": a["submitted"]}


def run_scan(n: int) -> dict:
    db.init()
    found = []
    for repo in candidates(n * 3):
        if len(found) >= n:
            break
        try:
            r = scan(repo)
        except Exception as e:
            log.warn(STAGE, f"{repo}: scan failed: {e}")
            continue
        if r.get("found"):
            found.append(r)
    return {"findings": found}


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--repo" in a:
        print(json.dumps(scan(a[a.index("--repo") + 1]), indent=1, default=str))
    elif "--report" in a:
        print(json.dumps(report(a[a.index("--report") + 1], force=True), indent=1, default=str))
    elif "--list" in a:
        for fid, f in _state()["findings"].items():
            print(f"  {fid}  {f['repo']}  [{f.get('severity')}] {f.get('vuln_class')}  {f.get('title')}  {'SENT' if f.get('submitted') else 'awaiting your review'}")
    else:
        print(json.dumps(run_scan(int(a[a.index("--scan") + 1]) if "--scan" in a and a[-1].isdigit() else 1), indent=1, default=str))
