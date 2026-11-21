"""Run a B-roll session: real agents on real, already-triaged issues, so the screen shows genuine work,
with NOTHING posted (CREW_BROLL=1 makes the workflow skip every GitHub write and push) and the session's
tokens excluded from the results (its time window is added to power.json exclude_windows).

Stop the dispatcher first (touch data/STOP and wait for it to exit), then:
  python broll_session.py 6 41 77 102          # four issues, one per agent slot
  python broll_session.py --end                # (if interrupted) close the window and clear running.json
"""
import json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPO = "dynamous-business/local-agent-crew-archon"
FORK_DIR = Path(r"C:\Users\ASUS\proart-crew\archon-local-crew")
HARNESS = Path(r"C:\Users\ASUS\proart-crew\archon-harness")
BUN = os.environ.get("CREW_BUN", "bun")
STATE = DATA / "broll_session.json"


def close_window():
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    p = json.loads((DATA / "power.json").read_text(encoding="utf-8"))
    win = [st.get("start", datetime.now().isoformat(timespec="seconds")), datetime.now().isoformat(timespec="seconds")]
    p.setdefault("exclude_windows", []).append(win)
    notes = p.setdefault("exclude_windows_notes", [])
    notes.append(f"{win[0]}..{win[1]}: B-roll session (agents re-worked issues {st.get('issues')} for the screen "
                 f"recording, nothing posted); not crew results.")
    (DATA / "power.json").write_text(json.dumps(p, indent=1), encoding="utf-8")
    (DATA / "running.json").write_text(json.dumps({"agents": 4, "queued": 0, "running": []}), encoding="utf-8")
    STATE.unlink(missing_ok=True)
    print("excluded window", win)


def main():
    if "--end" in sys.argv:
        return close_window()
    issues = [int(a) for a in sys.argv[1:5]]
    STATE.write_text(json.dumps({"start": datetime.now().isoformat(timespec="seconds"), "issues": issues}))
    (DATA / "broll-logs").mkdir(parents=True, exist_ok=True)
    runs = []
    for slot, n in enumerate(issues):
        title = json.loads(subprocess.run(["gh", "issue", "view", str(n), "--repo", REPO, "--json", "title"],
                                          capture_output=True, text=True, encoding="utf-8").stdout)["title"]
        ts = int(time.time())
        log = DATA / "run-logs" / f"broll-{n}-{ts}.log"
        fh = log.open("w", encoding="utf-8")
        env = dict(os.environ, CREW_BROLL="1", CREW_MODEL_LABEL="Qwen3.8-Flash-Next")
        p = subprocess.Popen([BUN, "run", "cli", "workflow", "run", "local-crew-issue", "--cwd", str(FORK_DIR),
                              "--branch", f"crew/broll-{n}-{ts}", str(n)], cwd=HARNESS, stdout=fh,
                             stderr=subprocess.STDOUT, env=env)
        runs.append({"issue": n, "title": title, "started": datetime.now().isoformat(timespec="seconds"),
                     "log": log.name, "slot": slot, "proc": p, "fh": fh})
        print(f"b-roll agent {slot + 1}: #{n} {title[:60]}", flush=True)
        (DATA / "running.json").write_text(json.dumps({"agents": 4, "queued": 0, "running": [
            {k: r[k] for k in ("issue", "title", "started", "log", "slot")} for r in runs]}), encoding="utf-8")
        time.sleep(15)
    try:
        while any(r["proc"].poll() is None for r in runs):
            time.sleep(5)
    finally:
        for r in runs:
            if r["proc"].poll() is None:
                subprocess.run(["taskkill", "/PID", str(r["proc"].pid), "/T", "/F"], capture_output=True)
            r["fh"].close()
        close_window()


if __name__ == "__main__":
    main()
