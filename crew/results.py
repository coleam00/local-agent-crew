"""Compute every number the post and README quote, straight from the crew's own logs.

  python results.py                 # summary since power.json run_start
  python results.py --md            # the README results table (markdown)
  python results.py --json          # machine-readable, for the script/overlays
  python results.py --first-day --md   # the first 24 hours: the numbers the video and post quote

Definitions (also printed in the README):
  input tokens   = prompt tokens processed + prompt tokens served from llama-server's cache
  cloud cost     = uncached input x cloud input price + cached input x cloud cached price
                   + output x cloud output price, for the SAME model on a cloud API (data/cloud_prices.json)
  electricity    = integral of measured power over the run x the electricity rate (data/power.json)
  per day        = total / hours of the run x 24
  issues worked  = distinct upstream issues, each counted once at its latest attempt; with a window end
                   (--first-day) only issues whose latest attempt finished inside the window count
"""
import argparse, csv, json, statistics
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
CLOUD_MODEL = "qwen/qwen3.8-flash"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def excluded(ts, windows):
    return any(a <= ts <= b for a, b in windows)


def deltas(rows, key, windows=()):
    """Sum counter increases, skipping any step that ends inside an excluded window (e.g. the synthetic
    load used for the 11/20 power calibration, which is not crew work)."""
    total, prev = 0.0, None
    for r in rows:
        v = num(r[key])
        if v is None:
            continue
        if prev is not None and not excluded(r["ts"], windows):
            total += v - prev if v >= prev else v
        prev = v
    return total


def compute(until=None):
    """until: ISO local time to stop at (e.g. run_start + 24 h for the first-day numbers the video quotes)."""
    power = json.loads((DATA / "power.json").read_text(encoding="utf-8"))
    since = power["run_start"]
    until = until or "9999"
    with (DATA / "telemetry.csv").open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if since <= r["ts"] <= until]
    runs = [json.loads(l) for l in (DATA / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (DATA / "runs.jsonl").exists() else []
    runs = [r for r in runs if r["started"] >= since]
    # runs that started inside an excluded window (calibration load, B-roll session) are not crew results
    runs = [r for r in runs if not excluded(r["started"], [tuple(w) for w in power.get("exclude_windows", [])])]
    attempts_infra = sum(1 for r in runs if r.get("infra_failure"))
    # one row per issue: its last real attempt (infrastructure failures were re-queued, not verdicts)
    last = {}
    for r in runs:
        if not r.get("infra_failure") and not r.get("excluded"):
            # one row per UPSTREAM issue: a state-file race on 11/20 mirrored 90 upstream issues twice and
            # 17 were worked under both copies; the tokens were really spent, but the issue counts once
            last[r.get("upstream") or f"crew-{r.get('new_issue', r['issue'])}"] = r
    runs = [r for r in last.values() if r["ended"] <= until]   # window: issues whose latest attempt finished in it
    t0, t1 = datetime.fromisoformat(rows[0]["ts"]), datetime.fromisoformat(rows[-1]["ts"])
    hours = (t1 - t0).total_seconds() / 3600
    day = 24 / hours if hours else 0

    windows = [tuple(w) for w in power.get("exclude_windows", [])]
    live = DATA / "broll_session.json"  # a B-roll session still running is excluded from its start onward
    if live.exists():
        windows.append((json.loads(live.read_text())["start"], "9999"))
    prompt, cached, gen = (deltas(rows, k, windows) for k in ("prompt_tok", "cached_tok", "gen_tok"))
    pr = json.loads((DATA / "cloud_prices.json").read_text(encoding="utf-8"))
    p = pr["models"][CLOUD_MODEL]["pricing_usd_per_token"]
    p_in, p_out = float(p["prompt"]), float(p["completion"])
    p_cache = float(p.get("input_cache_read", p["prompt"]))
    cloud = prompt * p_in + cached * p_cache + gen * p_out

    k, base, rate = power.get("k", 1.0), power.get("base_w", 0.0), power["usd_per_kwh"]
    wh, prev_t, watts = 0.0, None, []
    for r in rows:
        t, w = datetime.fromisoformat(r["ts"]).timestamp(), num(r["gpu_w"])
        if w is not None:
            watts.append(w * k + base)
        if prev_t is not None and w is not None and t - prev_t < 120:
            wh += (w * k + base) * (t - prev_t) / 3600
        prev_t = t
    busy = [num(r["busy"]) for r in rows if num(r["busy"]) is not None]
    mem = [num(r["gpu_mem_mib"]) for r in rows if num(r["gpu_mem_mib"]) is not None]
    gen_secs = [r for r in rows if (num(r["busy"]) or 0) > 0]

    verdicts = {}
    for r in runs:
        verdicts[r.get("verdict") or "failed"] = verdicts.get(r.get("verdict") or "failed", 0) + 1
    per_issue_in = [r["input_tokens"] for r in runs if r.get("input_tokens")]
    per_issue_out = [r["output_tokens"] for r in runs if r.get("output_tokens")]
    return {
        "run_start": rows[0]["ts"], "run_end": rows[-1]["ts"], "hours": round(hours, 2),
        "model": power.get("model_label"),
        "issues_worked": len(runs), "issues_per_day": round(len(runs) * day, 1),
        "verdicts": verdicts, "prs_opened": sum(1 for r in runs if r.get("pr")),
        "runs_failed": sum(1 for r in runs if r.get("exit") not in (0, None)),
        "runs_timed_out": sum(1 for r in runs if r.get("timed_out")),
        "infra_retries": attempts_infra,
        "tokens_in": int(prompt + cached), "tokens_in_cached": int(cached), "tokens_out": int(gen),
        "tokens_in_per_day": int((prompt + cached) * day), "tokens_out_per_day": int(gen * day),
        "median_tokens_in_per_issue": int(statistics.median(per_issue_in)) if per_issue_in else None,
        "median_tokens_out_per_issue": int(statistics.median(per_issue_out)) if per_issue_out else None,
        "median_minutes_per_issue": round(statistics.median([r["seconds"] for r in runs]) / 60, 1) if runs else None,
        "avg_gen_tok_per_s_while_busy": round(gen / (len(gen_secs) * 10), 1) if gen_secs else None,
        "avg_agents_busy": round(statistics.mean(busy), 2) if busy else None,
        "peak_gpu_mem_gb": round(max(mem) / 1024, 1) if mem else None,
        "avg_power_w": round(statistics.mean(watts), 1) if watts else None,
        "power_calibrated": bool(power.get("calibrated")),
        "kwh": round(wh / 1000, 3), "kwh_per_day": round(wh / 1000 * day, 3),
        "cloud_usd": round(cloud, 2), "cloud_usd_per_day": round(cloud * day, 2),
        "electricity_usd": round(wh / 1000 * rate, 2), "electricity_usd_per_day": round(wh / 1000 * rate * day, 2),
        "cloud_price_checked": pr["checked_utc"], "usd_per_kwh": rate,
    }


def md(r):
    day = abs(r["hours"] - 24) > .1        # a window that is not exactly one day also gets per-day rates
    per = lambda v, f: f" ({f(v)} per day)" if day else ""
    usd = lambda v: f"${v:,.2f}"
    return "\n".join([
        "| Measure | Result |", "|---|---|",
        f"| Window | {r['run_start'][:16].replace('T', ' ')} to {r['run_end'][:16].replace('T', ' ')} ({r['hours']} h) |",
        f"| Model | {r['model']}, 4 agents at a time |",
        f"| Issues worked | {r['issues_worked']}{per(r['issues_per_day'], str)} |",
        "| Outcome | " + ", ".join(f"{k.replace('-', ' ')}: {v}" for k, v in sorted(r['verdicts'].items(), key=lambda kv: -kv[1])) + " |",
        f"| PRs opened | {r['prs_opened']} |",
        f"| Tokens read / written | {r['tokens_in'] / 1e6:,.1f}M / {r['tokens_out'] / 1e6:,.2f}M ({r['tokens_in_cached'] / 1e6:,.1f}M of the reads from cache) |",
        f"| Peak GPU memory in use | {r['peak_gpu_mem_gb']} GB of 128 GB unified |",
        f"| Average power, whole laptop | {r['avg_power_w']} W |",
        f"| Same model through a cloud API | {usd(r['cloud_usd'])}{per(r['cloud_usd_per_day'], usd)} (list price checked {r['cloud_price_checked'][:10]}) |",
        f"| Electricity on the laptop | {usd(r['electricity_usd'])}{per(r['electricity_usd_per_day'], usd)} ({r['kwh']} kWh at {r['usd_per_kwh'] * 100:.1f} cents/kWh) |",
    ])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--first-day", action="store_true", help="only the first 24 hours after run_start")
    a = ap.parse_args()
    until = None
    if a.first_day:
        from datetime import timedelta
        start = json.loads((DATA / "power.json").read_text(encoding="utf-8"))["run_start"]
        until = (datetime.fromisoformat(start) + timedelta(hours=24)).isoformat(timespec="seconds")
    r = compute(until)
    print(md(r) if a.md else json.dumps(r, indent=1))
