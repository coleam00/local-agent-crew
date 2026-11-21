"""Live crew dashboard: python dashboard.py [--port 8765], then open http://127.0.0.1:8765

Everything on it is read from files the crew writes; nothing is estimated in the page itself.
  data/telemetry.csv     power, GPU memory, llama-server token counters (telemetry.py)
  data/runs.jsonl        finished runs (dispatcher.py)
  data/running.json      runs in flight (dispatcher.py)
  data/cloud_prices.json same model's cloud API price, with the date it was checked
  data/power.json        electricity rate + the whole-system power calibration (see README)
"""
import argparse, csv, json, re, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CLOUD_MODEL = "qwen/qwen3.8-flash"


def jload(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def telemetry(since):
    rows = []
    try:
        with (DATA / "telemetry.csv").open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["ts"] >= since]
    except FileNotFoundError:
        pass
    return rows


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sum_deltas(rows, key):
    total, prev = 0.0, None
    for r in rows:
        v = num(r[key])
        if v is None:
            continue
        if prev is not None:
            total += v - prev if v >= prev else v  # a counter reset means the server restarted
        prev = v
    return total


def state():
    power = jload(DATA / "power.json", {})
    since = power.get("run_start", "2000-01-01")
    rows = telemetry(since)
    runs = [json.loads(l) for l in (DATA / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()] if (DATA / "runs.jsonl").exists() else []
    runs = [r for r in runs if r["started"] >= since]
    running = jload(DATA / "running.json", {"running": [], "agents": 0})

    prompt, cached, gen = (sum_deltas(rows, k) for k in ("prompt_tok", "cached_tok", "gen_tok"))
    price = jload(DATA / "cloud_prices.json", {}).get("models", {}).get(CLOUD_MODEL, {}).get("pricing_usd_per_token", {})
    p_in, p_out = float(price.get("prompt", 0)), float(price.get("completion", 0))
    p_cache = float(price.get("input_cache_read", price.get("prompt", 0)))
    cloud = prompt * p_in + cached * p_cache + gen * p_out

    # electricity: integrate whole-system watts = gpu_w * k + base (calibrated on battery, see README)
    k, base = power.get("k", 1.0), power.get("base_w", 0.0)
    rate = power.get("usd_per_kwh", 0.0)
    wh, prev_t = 0.0, None
    for r in rows:
        t, w = datetime.fromisoformat(r["ts"]).timestamp(), num(r["gpu_w"])
        if prev_t is not None and w is not None and t - prev_t < 120:
            wh += (w * k + base) * (t - prev_t) / 3600
        prev_t = t
    last = rows[-1] if rows else {}
    tps = None
    valid = [r for r in rows[-12:] if num(r["gen_tok"]) is not None]  # a blank sample is not zero
    if len(valid) >= 2:
        a, b = valid[0], valid[-1]
        dt = datetime.fromisoformat(b["ts"]).timestamp() - datetime.fromisoformat(a["ts"]).timestamp()
        d = num(b["gen_tok"]) - num(a["gen_tok"])
        tps = round(d / dt, 1) if dt > 0 and d >= 0 else None  # d < 0: the server restarted
    hours = 0.0
    if rows:
        hours = (datetime.fromisoformat(rows[-1]["ts"]) - datetime.fromisoformat(rows[0]["ts"])).total_seconds() / 3600

    agents = []
    for r in running.get("running", []):
        node = None
        try:
            txt = (DATA / "run-logs" / r["log"]).read_text(encoding="utf-8", errors="replace")
            started = re.findall(r"^\[([\w.-]+)\] Started", txt, re.M)
            node = started[-1] if started else "starting"
        except Exception:
            pass
        agents.append({"issue": r["issue"], "title": r["title"], "node": node,
                       "secs": int(time.time() - datetime.fromisoformat(r["started"]).timestamp())})
    out = {
        "now": datetime.now().isoformat(timespec="seconds"), "hours": round(hours, 2),
        "model": power.get("model_label", ""), "agents_max": running.get("agents", 0), "agents": agents,
        "queued": running.get("queued", 0),
        "issues": len(runs), "prs": sum(1 for r in runs if r.get("pr")),
        "root_causes": sum(1 for r in runs if r.get("verdict")),
        "tok_in": int(prompt + cached), "tok_out": int(gen), "tps": tps,
        "gpu_w": num(last.get("gpu_w")), "gpu_mem_gb": round((num(last.get("gpu_mem_mib")) or 0) / 1024, 1),
        "cloud_usd": round(cloud, 4), "power_usd": round(wh / 1000 * rate, 4), "kwh": round(wh / 1000, 3),
        "calibrated": bool(power.get("calibrated")), "price_checked": jload(DATA / "cloud_prices.json", {}).get("checked_utc"),
    }
    # The totals on screen must be the exact numbers the post quotes: take them from results.py (one row per
    # upstream issue, infra retries and the calibration window excluded, calibrated whole-laptop power).
    try:
        import results
        r = results.compute()
        out.update({"issues": r["issues_worked"], "prs": r["prs_opened"], "tok_in": r["tokens_in"],
                    "tok_out": r["tokens_out"], "cloud_usd": r["cloud_usd"], "power_usd": r["electricity_usd"],
                    "kwh": r["kwh"], "hours": r["hours"]})
    except Exception as e:  # never blank the screen over a results error; the live fallback above stays
        out["results_error"] = str(e)[:200]
    return out


STEP = {"fetch": "Reading the issue", "triage": "Finding the root cause", "report": "Posting what it found",
        "build": "Writing the fix", "implement": "Writing the fix", "check": "Running the tests",
        "review": "Reviewing its own diff", "open-pr": "Opening a PR"}


def agents_feed(n=14):
    """Last n actions per agent slot, read straight from each run's Archon transcript."""
    running = jload(DATA / "running.json", {"running": []}).get("running", [])
    out = []
    for r in sorted(running, key=lambda r: r.get("slot") or 0):
        lines, node = [], None
        try:
            txt = (DATA / "run-logs" / r["log"]).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"Transcript: (\S+\.jsonl)", txt)
            started = re.findall(r"^\[([\w.-]+)\] Started", txt, re.M)
            node = started[-1].split(".")[-1] if started else None
            if m and Path(m.group(1)).exists():
                p = Path(m.group(1))
                with p.open(encoding="utf-8", errors="replace") as f:
                    f.seek(max(0, p.stat().st_size - 60000)); f.readline()
                    for line in f:
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        ev = e.get("event") or {}
                        if ev.get("type") == "tool_call":
                            a = ev.get("rawInput") or {}
                            arg = a.get("path") or a.get("pattern") or a.get("command") or ""
                            if arg:
                                lines.append({"k": "tool", "tool": ev.get("name", ""), "t": str(arg)[:140]})
                        elif ev.get("type") == "agent_thought_chunk":
                            t = " ".join((ev.get("text") or "").split())
                            if len(t) > 40:
                                lines.append({"k": "think", "t": t[:220]})
        except Exception:
            pass
        out.append({"slot": r.get("slot"), "issue": r["issue"], "title": r["title"],
                    "step": STEP.get(node, "Working"), "node": node or "", "lines": lines[-n:]})
    return {"agents": out}


class H(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive: one connection per open page instead of one per 2-second poll. On 11/20 the
    # machine ran out of ephemeral ports (16,384) to loopback TIME_WAIT entries that were not expiring.
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/agents"):
            body, ctype = json.dumps(agents_feed()).encode(), "application/json"
        elif self.path.startswith("/agents"):
            body, ctype = (Path(__file__).parent / "agents.html").read_bytes(), "text/html; charset=utf-8"
        elif self.path.startswith("/api/state"):
            body, ctype = json.dumps(state()).encode(), "application/json"
        else:
            body, ctype = (Path(__file__).parent / "dashboard.html").read_bytes(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
