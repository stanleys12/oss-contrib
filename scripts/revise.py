"""Usage: revise.py <change_id> [--model opus]"""
import argparse, json
from osc.contributor import revise_change
ap = argparse.ArgumentParser(); ap.add_argument("cid"); ap.add_argument("--model"); a = ap.parse_args()
print(json.dumps(revise_change(a.cid, a.model), indent=1))
