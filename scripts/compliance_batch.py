"""Run compliance checks on all prepared changes, concurrently per repo (never two agents in one workspace)."""
import concurrent.futures as cf, json, sys, traceback
from collections import defaultdict
from osc import db
from osc.compliance import check_change
db.init()
rows = db.rows("SELECT id, repo FROM changes WHERE status IN ('prepared','ready') ORDER BY repo")
by_repo = defaultdict(list)
for r in rows:
    by_repo[r["repo"]].append(r["id"])
def run_repo(repo):
    out = {}
    for cid in by_repo[repo]:
        try:
            res = check_change(cid)
            out[cid] = {"fails": res["fails"], "merge": res.get("merge_likelihood"), "cost": res.get("cost_usd")}
        except Exception as e:
            out[cid] = {"error": str(e)[:300] + traceback.format_exc()[-300:]}
    return out
results = {}
with cf.ThreadPoolExecutor(3) as ex:
    for out in ex.map(run_repo, list(by_repo)):
        results.update(out)
print(json.dumps(results, indent=1))
