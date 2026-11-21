"""Keep N local agents busy on the crew repo's queued issues, one Archon workflow run per issue.

Every finished run is appended to data/runs.jsonl:
  issue, title, started, ended, seconds, exit, verdict, pr, input_tokens, output_tokens, log

Tokens come from Archon's own per-run usage line ("Input tokens: N" / "Output tokens: N");
llama-server's /metrics (sampled by telemetry.py) is the independent cross-check.

Usage: python dispatcher.py --agents 4 [--once]
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORK = "dynamous-business/local-agent-crew-archon"
FORK_DIR = Path(os.environ.get("CREW_FORK_DIR", r"C:\Users\ASUS\proart-crew\archon-local-crew"))
# The CLI runs from a separate clone pinned at a tested commit, so the hourly upstream sync of
# FORK_DIR (the code the agents work on) never swaps the harness out from under a running crew.
HARNESS_DIR = Path(os.environ.get("CREW_HARNESS_DIR", r"C:\Users\ASUS\proart-crew\archon-harness"))
BUN = os.environ.get("CREW_BUN", "bun")
SERVER = os.environ.get("CREW_SERVER", "http://127.0.0.1:9931")
RUNS = ROOT / "data" / "runs.jsonl"
LOGS = ROOT / "data" / "run-logs"
STOP = ROOT / "data" / "STOP"   # touch this file to stop launching new runs
# Archon has no wall-clock limit for an AI node, only an idle timeout, and a model that keeps working
# never goes idle. A run past this is stopped and handed to cloud planning. 75 min (11/19) cut off
# two fixes that were already in review; fixes on Archon take ~60 min here, so the cap is 150.
MAX_RUN_SECS = int(os.environ.get("CREW_MAX_RUN_SECS", 150 * 60))


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def queued():
    out = gh("issue", "list", "--repo", FORK, "--label", "crew:queued", "--state", "open",
             "--limit", "100", "--json", "number,title")
    return sorted(json.loads(out), key=lambda i: i["number"])


def server_ok():
    try:
        return b"ok" in urllib.request.urlopen(SERVER + "/health", timeout=5).read()
    except Exception:
        return False


def model_label():
    try:
        data = json.loads(urllib.request.urlopen(SERVER + "/v1/models", timeout=5).read())
        return {"qwen3.8-flash-next": "Qwen3.8-Flash-Next", "qwen3-coder-next": "Qwen3-Coder-Next",
                "qwen3.5-9b": "Qwen3.5-9B"}.get(data["data"][0]["id"], data["data"][0]["id"])
    except Exception:
        return "a local model"


def parse(log_text):
    grab = lambda pat: (m.group(1) if (m := re.search(pat, log_text)) else None)
    to_int = lambda s: int(s.replace(",", "")) if s else None
    return {
        "verdict": grab(r"VERDICT=(\S+)"),
        "pr": grab(r"PR=(https://\S+)"),
        "input_tokens": to_int(grab(r"Input tokens:\s*([\d,]+)")),
        "output_tokens": to_int(grab(r"Output tokens:\s*([\d,]+)")),
    }


def launch(issue, label):
    LOGS.mkdir(parents=True, exist_ok=True)
    n = issue["number"]
    log = LOGS / f"issue-{n}-{int(time.time())}.log"
    env = dict(os.environ, CREW_MODEL_LABEL=label)
    # mark it taken right away so a restarted dispatcher never double-launches it
    subprocess.run(["gh", "issue", "edit", str(n), "--repo", FORK, "--add-label", "crew:working",
                    "--remove-label", "crew:queued"], capture_output=True)
    fh = log.open("w", encoding="utf-8")
    p = subprocess.Popen([BUN, "run", "cli", "workflow", "run", "local-crew-issue", "--cwd", str(FORK_DIR),
                          "--branch", f"crew/issue-{n}", str(n)],
                         cwd=HARNESS_DIR, stdout=fh, stderr=subprocess.STDOUT, env=env)
    print(f"{datetime.now():%H:%M:%S} START issue {n}: {issue['title'][:70]}", flush=True)
    return {"issue": n, "title": issue["title"], "proc": p, "fh": fh, "log": log,
            "started": datetime.now().isoformat(timespec="seconds"), "t0": time.time()}


TERMINAL = ("Workflow completed", "Workflow failed", 'error: script "cli" exited')
ATTEMPTS = ROOT / "data" / "attempts.json"


def live_run_pids():
    """{issue: pid} for crew workflow runs alive right now (bun processes on a crew/issue-N branch)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='bun.exe'\" | Where-Object CommandLine -match "
          "'local-crew-issue' | ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    pids = {}
    for line in out.splitlines():
        m = re.search(r"^(\d+) .*crew/issue-(\d+)", line)
        if m:
            pids.setdefault(int(m.group(2)), int(m.group(1)))
    return pids


class Adopted:
    """A run started by an earlier dispatcher that is still going (or finished unrecorded)."""
    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    def poll(self):
        if self.returncode is None and self.pid is not None:
            alive = subprocess.run(["tasklist", "/FI", f"PID eq {self.pid}", "/NH"], capture_output=True,
                                   text=True).stdout
            if str(self.pid) not in alive:
                self.pid = None
        if self.returncode is None and self.pid is None:
            self.returncode = -2  # real exit decided from the log text in finish()
        return self.returncode


def adopt():
    """Pick up runs whose log has no record in runs.jsonl, so a dispatcher restart or crash loses nothing."""
    recorded = set()
    if RUNS.exists():
        recorded = {json.loads(l)["log"] for l in RUNS.read_text(encoding="utf-8").splitlines() if l.strip()}
    live = live_run_pids()
    runs = []
    for log in sorted(LOGS.glob("issue-*.log")) if LOGS.exists() else []:
        if log.name in recorded:
            continue
        n, ts = (int(x) for x in re.match(r"issue-(\d+)-(\d+)\.log", log.name).groups())
        text = log.read_text(encoding="utf-8", errors="replace")
        if not any(t in text for t in TERMINAL) and n not in live:
            continue  # neither finished nor running: not ours to judge (e.g. killed with no output)
        title = ""
        try:
            title = json.loads(gh("issue", "view", str(n), "--repo", FORK, "--json", "title"))["title"]
        except Exception:
            pass
        runs.append({"issue": n, "title": title, "proc": Adopted(live.get(n)), "fh": None, "log": log,
                     "started": datetime.fromtimestamp(ts).isoformat(timespec="seconds"), "t0": ts,
                     "adopted": True})
        print(f"{datetime.now():%H:%M:%S} ADOPT issue {n} ({'running' if n in live else 'finished'})", flush=True)
    return runs


def finish(run):
    if run["fh"]:
        run["fh"].close()
    text = run["log"].read_text(encoding="utf-8", errors="replace")
    code = run["proc"].returncode
    if code == -2:  # adopted: no exit code available, read it from Archon's own last words
        code = 0 if "Workflow completed successfully" in text else 1
    rec = {"issue": run["issue"], "title": run["title"], "started": run["started"],
           "ended": datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - run["t0"]), "exit": code,
           **parse(text), "log": run["log"].name, "timed_out": bool(run.get("timed_out"))}
    # Died before triage finished and was not timed out: the machine failed (network, workspace),
    # not the model. That is not a verdict, so queue the issue again, up to 3 attempts.
    rec["infra_failure"] = code != 0 and "[triage] Completed" not in text and not rec["timed_out"]
    if rec["infra_failure"]:
        attempts = json.loads(ATTEMPTS.read_text()) if ATTEMPTS.exists() else {}
        attempts[str(rec["issue"])] = attempts.get(str(rec["issue"]), 0) + 1
        ATTEMPTS.write_text(json.dumps(attempts))
        label = "crew:queued" if attempts[str(rec["issue"])] < 3 else "crew:infra-failed"
        subprocess.run(["gh", "issue", "edit", str(rec["issue"]), "--repo", FORK, "--add-label", label,
                        "--remove-label", "crew:working"], capture_output=True)
        with RUNS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"{datetime.now():%H:%M:%S} RETRY issue {rec['issue']} (infrastructure failure, "
              f"attempt {attempts[str(rec['issue'])]}) -> {label}", flush=True)
        return
    # the source of truth is GitHub, not log text: the label the run left and any PR on its branch
    labels = set()
    try:
        labels = {l["name"] for l in json.loads(gh("issue", "view", str(rec["issue"]), "--repo", FORK,
                                                     "--json", "labels"))["labels"]}
        rec["verdict"] = ("simple-fix" if labels & {"crew:triaged", "crew:pr-open"} else
                          "needs-cloud-planning" if "needs-cloud-planning" in labels else
                          "not-actionable" if "crew:not-actionable" in labels else rec["verdict"])
        prs = json.loads(gh("pr", "list", "--repo", FORK, "--head", f"crew/issue-{rec['issue']}",
                            "--state", "all", "--json", "url"))
        rec["pr"] = prs[0]["url"] if prs else rec["pr"]
    except Exception as e:
        print(f"OUTCOME_LOOKUP_ERROR issue {rec['issue']}: {e}", file=sys.stderr, flush=True)
    if rec["exit"] != 0 and "crew:pr-open" not in labels:
        # the local attempt did not finish: hand it to cloud planning with whatever triage found
        subprocess.run(["gh", "issue", "edit", str(rec["issue"]), "--repo", FORK, "--add-label",
                        "needs-cloud-planning", "--remove-label", "crew:working"], capture_output=True)
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    with RUNS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"{datetime.now():%H:%M:%S} DONE  issue {rec['issue']} exit={rec['exit']} verdict={rec['verdict']} "
          f"in={rec['input_tokens']} out={rec['output_tokens']} {rec['seconds']}s {rec['pr'] or ''}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--once", action="store_true", help="exit when the queue is empty")
    a = ap.parse_args()
    running, last_poll, queue = adopt(), 0, []
    state_file = ROOT / "data" / "running.json"
    for i, r in enumerate(running):  # stable agent slot, so a viewer pane keeps following one agent
        r["slot"] = i
    while True:
        for r in running:
            if r["proc"].poll() is None and time.time() - r["t0"] > MAX_RUN_SECS and not r.get("timed_out"):
                r["timed_out"] = True
                if r["proc"].pid:
                    subprocess.run(["taskkill", "/PID", str(r["proc"].pid), "/T", "/F"], capture_output=True)
                print(f"{datetime.now():%H:%M:%S} TIMEOUT issue {r['issue']} after {MAX_RUN_SECS // 60} min", flush=True)
        for r in [r for r in running if r["proc"].poll() is not None]:
            finish(r)
            running.remove(r)
        state_file.write_text(json.dumps({"agents": a.agents, "queued": len(queue), "running": [
            {"issue": r["issue"], "title": r["title"], "started": r["started"], "log": r["log"].name,
             "slot": r.get("slot")} for r in running]}), encoding="utf-8")
        if STOP.exists():
            if not running:
                print("STOP file found and all runs finished; exiting", flush=True)
                return
        elif len(running) < a.agents and server_ok():
            if time.time() - last_poll > 60 or not queue:
                try:
                    taken = {r["issue"] for r in running}
                    queue = [i for i in queued() if i["number"] not in taken]
                    last_poll = time.time()
                except Exception as e:
                    print(f"QUEUE_ERROR {e}", file=sys.stderr, flush=True)
            if queue:
                run = launch(queue.pop(0), model_label())
                run["slot"] = min(set(range(a.agents)) - {r.get("slot") for r in running})
                running.append(run)
                time.sleep(20)  # stagger starts so prompt processing does not all land at once
                continue
            if a.once and not running:
                return
        time.sleep(5)


if __name__ == "__main__":
    main()
