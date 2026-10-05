"""Build one opportunity outside the worker. Usage: build_one.py <opportunity_id>"""
import json, sys
from osc.contributor import build_opportunity
ch = build_opportunity(sys.argv[1])
print(json.dumps({k: ch.get(k) for k in ("id", "status", "review_score", "review_verdict", "status_note")}, indent=1))
