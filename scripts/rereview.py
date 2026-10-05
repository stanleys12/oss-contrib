"""Usage: rereview.py <change_id>"""
import json, sys
from osc.contributor import rereview_change
print(json.dumps(rereview_change(sys.argv[1]), indent=1))
