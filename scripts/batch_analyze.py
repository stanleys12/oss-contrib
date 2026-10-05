"""Analyze several repos concurrently (each in its own clone/workspace). Usage: batch_analyze.py [-j 3] owner/name ..."""
import argparse, concurrent.futures as cf, json, sys, time
from osc import db
from osc.analyzer import analyze_repo
from osc import log

ap = argparse.ArgumentParser(); ap.add_argument("repos", nargs="+"); ap.add_argument("-j", type=int, default=3); ap.add_argument("--model"); ap.add_argument("--focus-file")
a = ap.parse_args()
db.init()
results = {}
def one(r):
    t0 = time.time()
    try:
        res = analyze_repo(r, a.model, focus=(open(a.focus_file).read() if a.focus_file else None))
        return r, {"ok": True, "opps": len(res.get("opportunities", [])), "cost": res.get("cost_usd"), "s": round(time.time() - t0)}
    except Exception as e:
        return r, {"ok": False, "error": str(e)[:300], "s": round(time.time() - t0)}
with cf.ThreadPoolExecutor(a.j) as ex:
    for r, res in ex.map(one, a.repos):
        results[r] = res
        log.info("batch", f"{r}: {res}")
print(json.dumps(results, indent=1))
