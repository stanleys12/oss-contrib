#!/usr/bin/env python3
"""osc — Open Source Contribution engine CLI.

  osc scan [--domain D ...] [--no-search] [--limit N]     discover + score repos
  osc scan --repo owner/name                              score one specific repo
  osc rank [--domain D] [--n 30]                          show ranked candidates
  osc analyze owner/name [--model sonnet] [--no-scout]    deep analysis → opportunities
  osc analyze-top [--n 12] [--domain D]                   analyze the best un-analyzed repos
  osc opps [--repo owner/name]                            list opportunities
  osc build <opportunity-id> [--model opus] [--rounds 2]  implement + review a change
  osc changes                                             list changes
  osc prepare <change-id>                                 fork + push branch, print `gh pr create` (PR not opened)
  osc serve [--port 8791]                                 UI + background worker
  osc auto [--n-repos 3] [--per-repo 1] [--domain D]      analyze + build end-to-end
"""
from __future__ import annotations

import argparse
import json
import sys

from osc import config, db


def main(argv=None):
    ap = argparse.ArgumentParser(prog="osc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--domain", action="append"); s.add_argument("--no-search", action="store_true")
    s.add_argument("--limit", type=int); s.add_argument("--repo")
    s = sub.add_parser("rank"); s.add_argument("--domain"); s.add_argument("--n", type=int, default=30)
    s = sub.add_parser("analyze"); s.add_argument("repo"); s.add_argument("--model"); s.add_argument("--no-scout", action="store_true")
    s = sub.add_parser("analyze-top"); s.add_argument("--n", type=int); s.add_argument("--domain"); s.add_argument("--model")
    s = sub.add_parser("opps"); s.add_argument("--repo")
    s = sub.add_parser("build"); s.add_argument("opp"); s.add_argument("--model"); s.add_argument("--reviewer-model"); s.add_argument("--rounds", type=int)
    sub.add_parser("changes")
    s = sub.add_parser("prepare"); s.add_argument("change")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8791); s.add_argument("--host", default="127.0.0.1")
    s = sub.add_parser("auto"); s.add_argument("--n-repos", type=int, default=3); s.add_argument("--per-repo", type=int, default=1); s.add_argument("--domain")
    a = ap.parse_args(argv)
    db.init()

    if a.cmd == "scan":
        from osc import scanner
        if a.repo:
            from osc.gh import GitHub
            n = scanner.enrich_and_store(GitHub(), {a.repo.lower(): {"full_name": a.repo, "domains": set(a.domain or ["unknown"]), "source": "manual"}})
            print(json.dumps(db.row("SELECT full_name, score, score_parts, stars, open_prs FROM repos WHERE lower(full_name)=?", (a.repo.lower(),)), indent=2))
        else:
            print(json.dumps(scanner.run_scan(a.domain, not a.no_search, a.limit), indent=2))
    elif a.cmd == "rank":
        from osc.scanner import rank
        print(f"{'score':>6} {'stars':>7} {'oPRs':>5} {'ext%':>5} {'gfi+hw':>6} {'lang':10} {'domain':14} {'status':10} repo")
        for r in rank(a.n, a.domain):
            print(f"{r['score']:>6.3f} {r['stars']:>7} {r['open_prs']:>5} {r['ext_merge_ratio']*100:>4.0f}% {r['gfi_issues']+r['hw_issues']:>6} "
                  f"{(r['language'] or '')[:10]:10} {r['domain'][:14]:14} {r['status']:10} {r['full_name']}")
    elif a.cmd == "analyze":
        from osc.analyzer import analyze_repo
        print(json.dumps(analyze_repo(a.repo, a.model, a.no_scout), indent=2, default=str))
    elif a.cmd == "analyze-top":
        from osc.pipeline import run_job
        print(json.dumps(run_job({"kind": "analyze_top", "target": None, "params": json.dumps({"n": a.n, "domain": a.domain, "model": a.model})}), indent=2, default=str))
    elif a.cmd == "opps":
        sql = "SELECT id, repo, kind, priority, confidence, accept_likelihood, status, title FROM opportunities"
        params = ()
        if a.repo:
            sql += " WHERE repo=?"; params = (a.repo,)
        for o in db.rows(sql + " ORDER BY priority DESC", params):
            print(f"{o['priority']:.2f} c={o['confidence']:.2f} a={o['accept_likelihood']:.2f} {o['status']:10} {o['kind']:12} {o['id']}  {o['repo']}: {o['title']}")
    elif a.cmd == "build":
        from osc.contributor import build_opportunity
        ch = build_opportunity(a.opp, a.model, a.reviewer_model, a.rounds)
        print(json.dumps({k: v for k, v in ch.items() if k not in ("diff", "review")}, indent=2, default=str))
    elif a.cmd == "changes":
        for c in db.rows("SELECT id, repo, branch, status, review_score, review_verdict, diff_stats, cost_usd, pr_title FROM changes ORDER BY created_at DESC"):
            print(f"{c['id']} {c['status']:10} score={c['review_score']} {c['review_verdict']} ${c['cost_usd']} {c['repo']} [{c['branch']}] {c['pr_title']}")
    elif a.cmd == "prepare":
        from osc.contributor import prepare_submission
        print(json.dumps(prepare_submission(a.change), indent=2))
    elif a.cmd == "serve":
        import uvicorn
        from osc.server import app
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "auto":
        from osc.pipeline import run_job
        print(json.dumps(run_job({"kind": "auto", "target": None, "params": json.dumps({"n_repos": a.n_repos, "per_repo": a.per_repo, "domain": a.domain})}), indent=2, default=str))


if __name__ == "__main__":
    main()
