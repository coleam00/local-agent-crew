"""Sample power, GPU and token counters every N seconds into data/telemetry.csv.

Columns
  ts                      ISO time (local)
  gpu_w                   NVIDIA RTX Spark GPU power from nvidia-smi (GPU rail only, not the whole laptop)
  gpu_util, gpu_mem_mib   utilisation % and memory in use
  sys_w                   whole-laptop power from the battery's discharge rate. Only valid while
                          UNPLUGGED (it is 0 on AC). Used to calibrate gpu_w -> whole-system watts.
  on_ac                   1 when plugged in
  prompt_tok              llama-server prompt tokens processed (cumulative, uncached input)
  cached_tok              prompt tokens served from the KV cache (cumulative, cached input)
  gen_tok                 tokens generated (cumulative, output)
  busy                    requests being processed right now (agents mid-turn)
  deferred                requests waiting for a free slot

Usage: python telemetry.py [--every 10] [--server http://127.0.0.1:9931]
"""
import argparse, csv, json, re, subprocess, time, urllib.request
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "telemetry.csv"
FIELDS = ["ts", "gpu_w", "gpu_util", "gpu_mem_mib", "sys_w", "on_ac",
          "prompt_tok", "cached_tok", "gen_tok", "busy", "deferred"]
PS_BATTERY = ("$b=Get-CimInstance -Namespace root\\wmi -ClassName BatteryStatus | Select-Object -First 1;"
              "\"$($b.PowerOnline) $($b.DischargeRate)\"")


def nvsmi():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=power.draw,utilization.gpu,memory.used",
                              "--format=csv,noheader,nounits", "-i", "0"],
                             capture_output=True, text=True, timeout=10).stdout.strip().split(",")
        return [float(x) if x.strip() not in ("[N/A]", "") else "" for x in out]
    except Exception:
        return ["", "", ""]


def battery():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_BATTERY],
                             capture_output=True, text=True, timeout=15).stdout.split()
        on_ac = 1 if out[0] == "True" else 0
        return (int(out[1]) / 1000.0 if not on_ac else 0.0), on_ac
    except Exception:
        return "", ""


_conn = None


def _get(server, path):
    """One keep-alive connection, reused: a new connection every 10 s left ~8,000 loopback TIME_WAIT
    entries over a day, and on 11/20 Windows was not expiring them, exhausting the 16,384 ports."""
    global _conn
    import http.client
    from urllib.parse import urlparse
    u = urlparse(server)
    for attempt in (1, 2):
        try:
            if _conn is None:
                _conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
            _conn.request("GET", path, headers={"Connection": "keep-alive"})
            return _conn.getresponse().read().decode()
        except Exception:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            if attempt == 2:
                raise


def metrics(server):
    try:
        text = _get(server, "/metrics")
        get = lambda k: float(re.search(rf"^llamacpp:{k} (\S+)$", text, re.M).group(1))
        return [int(get("prompt_tokens_total")), int(get("prompt_tokens_cached_total")),
                int(get("tokens_predicted_total")), int(get("requests_processing")),
                int(get("requests_deferred"))]
    except Exception:
        return ["", "", "", "", ""]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=float, default=10)
    ap.add_argument("--server", default="http://127.0.0.1:9931")
    a = ap.parse_args()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    new = not OUT.exists()
    n = 0
    with OUT.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        sys_w, on_ac = battery()
        while True:
            t0 = time.time()
            if n % 3 == 0:  # battery every 3rd sample: PowerShell start-up is the slow part
                sys_w, on_ac = battery()
            w.writerow([datetime.now().isoformat(timespec="seconds"), *nvsmi(), sys_w, on_ac, *metrics(a.server)])
            f.flush()
            n += 1
            time.sleep(max(0, a.every - (time.time() - t0)))


if __name__ == "__main__":
    main()
