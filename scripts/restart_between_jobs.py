"""Restart the dashboard/worker in the first gap where no job is running (sqlite with busy timeout, 0.25s poll)."""
import sqlite3, subprocess, time, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
con = sqlite3.connect(os.path.join(ROOT, "data", "osc.db"), timeout=30)
while True:
    n = con.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
    if n == 0:
        subprocess.run(["pkill", "-fi", "osc.py serve"]); time.sleep(1)
        subprocess.Popen([os.path.join(ROOT, ".venv/bin/python"), "osc.py", "serve", "--port", "8791"], cwd=ROOT,
                         stdout=open(os.path.join(ROOT, "logs/server.log"), "a"), stderr=subprocess.STDOUT, start_new_session=True)
        print("restarted", time.strftime("%H:%M:%S")); sys.exit(0)
    time.sleep(0.25)
