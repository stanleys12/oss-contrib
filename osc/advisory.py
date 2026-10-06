"""Advisory-database corrections (user goal 2026-10-06, toward the Security advisory credit badge).

github/advisory-database takes public PRs that improve existing advisory records: a missing fix-commit
reference, a missing CWE, a missing public reference. A merged improvement credits the contributor.

This finds reviewed advisories with a GitHub source repo that are missing a commit reference or a CWE,
has an agent research ONE citable correction from public sources (the repo's own advisory, the fix PR,
NVD, release notes), a second agent verify every fact, and then prepares a one-advisory PR.

Nothing is submitted unless ADVISORY_AUTO_SUBMIT is true. `prepare` opens the PR; `draft` only researches.

  python -m osc.advisory --find            # list candidates, cheap
  python -m osc.advisory --draft [N]       # research+verify N candidates, save drafts, submit nothing
  python -m osc.advisory --submit <GHSA>   # open the PR for an already-verified draft (gated)
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config, db, log
from .claude_runner import extract_json, run_claude
from .daily import PROJECT

STAGE = "advisory"
STATE_F = PROJECT / "data" / "advisory_state.json"
DB = "github/advisory-database"


def _api(path: str) -> tuple[list | dict, str | None]:
    r = urllib.request.Request("https://api.github.com" + path,
                               headers={"Authorization": f"Bearer {config.github_token()}",
                                        "Accept": "application/vnd.github+json", "User-Agent": "osc-advisory"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        link = resp.headers.get("Link", "")
        data = json.load(resp)
    m = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return data, (m.group(1).replace("https://api.github.com", "") if m else None)


def _gh(*args: str, timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GH_TOKEN": config.github_token(), "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def _state() -> dict:
    try:
        st = json.loads(STATE_F.read_text())
    except Exception:
        st = {}
    st.setdefault("seen", {})
    st.setdefault("drafts", {})
    return st


def _save(st: dict) -> None:
    tmp = STATE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE_F)


# ---------------------------------------------------------------- find candidates
def candidates(pages: int = 8) -> list[dict]:
    st = _state()
    url = "/advisories?per_page=100&type=reviewed&sort=updated"
    seen: dict[str, dict] = {}
    for _ in range(pages):
        rows, nxt = _api(url)
        for a in rows:
            seen[a["ghsa_id"]] = a
        if not nxt:
            break
        url = nxt
    out = []
    for a in seen.values():
        g = a["ghsa_id"]
        loc = a.get("source_code_location") or ""
        if a.get("withdrawn_at") or not loc.startswith("https://github.com/") or g in st["seen"]:
            continue
        refs = a.get("references") or []
        gap = None
        if not any("/commit/" in u for u in refs):
            gap = "no fix-commit reference"
        elif not a.get("cwes") and a.get("cve_id"):
            gap = "no CWE"
        if not a.get("cwes") and a.get("cve_id") and gap != "no CWE":
            gap = gap + "; no CWE" if gap else "no CWE"
        if not gap:
            continue
        out.append({"ghsa": g, "cve": a.get("cve_id"), "repo": loc.replace("https://github.com/", ""),
                    "summary": (a.get("summary") or "")[:120], "severity": a.get("severity"),
                    "gap": gap, "nrefs": len(refs), "published": a.get("published_at", "")[:10]})
    # a linked fix PR makes the commit easy and verifiable: surface those first
    return sorted(out, key=lambda c: (c["gap"] != "no CWE", -c["nrefs"]))


# ---------------------------------------------------------------- locate the OSV file in the repo
def _osv_path(ghsa: str, published: str) -> tuple[str, dict, str] | None:
    """(repo path, parsed record, blob sha). Tries the publish-date dir, then code search."""
    y, m = (published[:4], published[5:7]) if len(published) >= 7 else ("", "")
    tries = [f"advisories/github-reviewed/{y}/{m}/{ghsa}/{ghsa}.json"] if y else []
    rc, out = _gh("api", f"search/code?q=repo:{DB}+filename:{ghsa}.json", "--jq", ".items[0].path")
    if rc == 0 and out:
        tries.append(out)
    for p in dict.fromkeys(tries):
        rc, out = _gh("api", f"repos/{DB}/contents/{p}", "--jq", "{sha:.sha,content:.content}")
        if rc == 0 and out:
            try:
                d = json.loads(out)
                raw = base64.b64decode(d["content"]).decode()
                return p, json.loads(raw), d["sha"], raw
            except Exception:
                continue
    return None


# ---------------------------------------------------------------- the two agents
ADVISORY_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["commit", "cwe", "reference", "none"]},
        "summary": {"type": "string"},
        "addition": {},
        "json_pointer": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "pr_body": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["found"],
}
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["ok", "fix", "reject"]},
                   "problems": {"type": "array", "items": {"type": "string"}},
                   "addition": {}, "pr_body": {"type": "string"}},
    "required": ["verdict"],
}


def draft(c: dict) -> dict:
    loc = _osv_path(c["ghsa"], c["published"])
    if not loc:
        return {"ghsa": c["ghsa"], "found": False, "reason": "could not locate the OSV file in the repo"}
    path, record, sha, raw = loc
    cwd = config.ROOT
    p = (config.PROMPTS_DIR / "advisory.md").read_text()
    for k, v in {"__GHSA__": c["ghsa"], "__CVE__": str(c["cve"]), "__REPO__": c["repo"], "__SUMMARY__": c["summary"],
                 "__GAP__": c["gap"], "__RECORD__": json.dumps(record, indent=2)[:12000]}.items():
        p = p.replace(k, v)
    res = run_claude(p, cwd, model=config.setting("ADVISORY_MODEL"), max_turns=40, stage=STAGE, repo=c["ghsa"],
                     json_schema=ADVISORY_SCHEMA, max_budget_usd=float(config.setting("ADVISORY_BUDGET_USD")), timeout_s=1800,
                     allowed_tools=["Bash(gh api:*)", "Bash(gh search:*)", "Bash(gh pr view:*)", "Bash(gh release:*)", "WebFetch", "WebSearch"])
    a = res.structured if isinstance(res.structured, dict) else extract_json(res.text) or {"found": False, "reason": "no answer"}
    a["cost"] = res.cost_usd
    if not a.get("found"):
        return {"ghsa": c["ghsa"], "found": False, "reason": a.get("reason", ""), "cost": res.cost_usd}
    vp = (config.PROMPTS_DIR / "advisory_verify.md").read_text()
    for k, v in {"__GHSA__": c["ghsa"], "__CVE__": str(c["cve"]), "__KIND__": str(a.get("kind")),
                 "__POINTER__": str(a.get("json_pointer")), "__ADDITION__": json.dumps(a.get("addition"), indent=2),
                 "__PR_BODY__": str(a.get("pr_body")), "__EVIDENCE__": "\n".join(f"- {e}" for e in a.get("evidence") or []),
                 "__RECORD__": json.dumps(record, indent=2)[:12000]}.items():
        vp = vp.replace(k, v)
    vr = run_claude(vp, cwd, model=config.setting("ADVISORY_MODEL"), max_turns=30, stage="advisory-check", repo=c["ghsa"],
                    json_schema=VERIFY_SCHEMA, max_budget_usd=float(config.setting("ADVISORY_BUDGET_USD")), timeout_s=1200,
                    allowed_tools=["Bash(gh api:*)", "Bash(gh search:*)", "WebFetch", "WebSearch"])
    v = vr.structured if isinstance(vr.structured, dict) else extract_json(vr.text) or {"verdict": "reject", "problems": ["no verdict"]}
    a["cost"] += vr.cost_usd
    if v["verdict"] == "fix":
        if v.get("addition") is not None:
            a["addition"] = v["addition"]
        if v.get("pr_body"):
            a["pr_body"] = v["pr_body"]
    a.update(ghsa=c["ghsa"], cve=c["cve"], repo=c["repo"], path=path, blob_sha=sha, record=record, raw=raw,
             verdict=v["verdict"], problems=v.get("problems", []), gap=c["gap"])
    return a


# ---------------------------------------------------------------- apply + PR (gated)
def _apply(record: dict, pointer: str, addition) -> dict:
    r = json.loads(json.dumps(record))
    if pointer == "/references/-":
        r.setdefault("references", []).append(addition)
    elif pointer == "/database_specific/cwe_ids/-":
        r.setdefault("database_specific", {}).setdefault("cwe_ids", []).append(addition)
    else:
        raise RuntimeError(f"unsupported pointer {pointer}")
    return r


def prepare(ghsa: str, force: bool = False) -> dict:
    st = _state()
    a = st["drafts"].get(ghsa)
    if not a:
        raise RuntimeError(f"no draft for {ghsa}; run --draft first")
    if a.get("verdict") != "ok" and not force:
        raise RuntimeError(f"draft is '{a.get('verdict')}', not ok: {a.get('problems')}")
    if not (config.setting("ADVISORY_AUTO_SUBMIT") or force):
        return {"ghsa": ghsa, "submitted": False, "why": "ADVISORY_AUTO_SUBMIT is off"}
    rc, me = _gh("api", "user", "-q", ".login"); me = me.strip()
    def ser(d):
        return json.dumps(d, indent=2, ensure_ascii=False)
    if a.get("raw") is not None and ser(a["record"]) != a["raw"]:
        raise RuntimeError("our serialization does not match the repo file byte-for-byte; not editing")
    new = _apply(a["record"], a["json_pointer"], a["addition"])
    body = ser(new)
    # verify we changed exactly what we meant and nothing else
    import difflib
    diff = "".join(difflib.unified_diff(ser(a["record"]).splitlines(True), body.splitlines(True), lineterm="\n"))
    added = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    if removed or not added:
        raise RuntimeError(f"edit is not a clean single addition (added {len(added)}, removed {len(removed)})")
    _gh("api", "-X", "POST", f"repos/{DB}/forks", timeout=120)
    time.sleep(3)
    base = _gh("api", f"repos/{DB}", "-q", ".default_branch")[1].strip()
    # sync fork
    _gh("api", "-X", "POST", f"repos/{me}/advisory-database/merge-upstream", "-f", f"branch={base}")
    sha = _gh("api", f"repos/{me}/advisory-database/git/ref/heads/{base}", "-q", ".object.sha")[1].strip()
    branch = f"{me}-{ghsa}"
    _gh("api", "-X", "POST", f"repos/{me}/advisory-database/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}")
    # the file path/sha on the fork tip
    fsha = _gh("api", f"repos/{me}/advisory-database/contents/{a['path']}?ref={branch}", "-q", ".sha")[1].strip() or a["blob_sha"]
    rc, out = _gh("api", "-X", "PUT", f"repos/{me}/advisory-database/contents/{a['path']}",
                  "-f", f"message={a['summary']} ({ghsa})", "-f", f"branch={branch}", "-f", f"sha={fsha}",
                  "-f", "content=" + base64.b64encode(body.encode()).decode())
    if rc != 0:
        raise RuntimeError(f"commit failed: {out[:200]}")
    title = f"[{ghsa}] {a['summary']}"
    rc, url = _gh("api", "-X", "POST", f"repos/{DB}/pulls", "-f", f"title={title}", "-f", f"head={me}:{branch}",
                  "-f", f"base={base}", "-f", f"body={a['pr_body']}", "-q", ".html_url")
    if rc != 0:
        raise RuntimeError(f"PR failed: {url[:200]}")
    a["pr_url"] = url.strip()
    st["seen"][ghsa] = time.time()
    _save(st)
    log.ok(STAGE, f"opened advisory PR {url.strip()}", repo=ghsa)
    return {"ghsa": ghsa, "submitted": True, "url": url.strip()}


def run_drafts(n: int) -> dict:
    db.init()
    st = _state()
    cands = candidates()
    done = []
    for c in cands:
        if len([d for d in done if d.get("verdict") == "ok"]) >= n:
            break
        if c["ghsa"] in st["drafts"] and st["drafts"][c["ghsa"]].get("verdict") == "ok":
            continue
        log.info(STAGE, f"researching {c['ghsa']} ({c['repo']}, {c['gap']})")
        try:
            a = draft(c)
        except Exception as e:
            log.warn(STAGE, f"{c['ghsa']}: {e}")
            continue
        if a.get("found") and a.get("verdict") in ("ok", "fix"):
            st["drafts"][c["ghsa"]] = a
            done.append(a)
            log.ok(STAGE, f"{c['ghsa']}: {a.get('verdict')} - {a.get('summary')}")
        else:
            st["seen"][c["ghsa"]] = time.time()      # nothing to add, or rejected: don't revisit
            if a.get("verdict") == "reject":
                log.info(STAGE, f"{c['ghsa']}: rejected by checker: {a.get('problems')}")
        _save(st)
    return {"drafted": [{"ghsa": d["ghsa"], "summary": d.get("summary"), "verdict": d.get("verdict")} for d in done]}


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--find" in a:
        for c in candidates()[:40]:
            print(f"  {c['ghsa']}  {c['cve'] or '-':16s} {c['severity'] or '-':8s} {c['repo']:32s} [{c['gap']}]  {c['summary'][:50]}")
    elif "--submit" in a:
        print(json.dumps(prepare(a[a.index("--submit") + 1], force="--force" in a), indent=1))
    else:
        n = int(a[a.index("--draft") + 1]) if "--draft" in a and len(a) > a.index("--draft") + 1 and a[a.index("--draft") + 1].isdigit() else 3
        print(json.dumps(run_drafts(n), indent=1, default=str))
