"""Build several opportunities sequentially in one process. Usage: build_many.py <opp_id> ..."""
import json, sys
from osc.contributor import build_opportunity
out = {}
for oid in sys.argv[1:]:
    try:
        ch = build_opportunity(oid); out[oid] = {k: ch.get(k) for k in ("id", "status", "review_score", "review_verdict")}
    except Exception as e:
        out[oid] = {"error": str(e)[:300]}
print(json.dumps(out, indent=1))
