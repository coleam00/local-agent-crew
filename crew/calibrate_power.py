"""Calibrate whole-laptop power against the GPU power reading, from a stretch on battery.

On battery, Windows reports the battery's discharge rate: the whole laptop's draw (CPU, GPU, memory,
screen, SSD, Wi-Fi). On AC it reports nothing, so the crew's long run only has the GPU reading. This fits
    laptop_w = k * gpu_w + base_w
over the on-battery samples, and writes k and base_w to data/power.json so electricity covers the laptop.

  python calibrate_power.py            # report only
  python calibrate_power.py --write    # also update data/power.json
"""
import argparse, csv, json, statistics
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--since", default=None, help="only samples at/after this ISO time")
    a = ap.parse_args()
    with (DATA / "telemetry.csv").open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["on_ac"] == "0" and (not a.since or r["ts"] >= a.since)]
    # The battery reading is refreshed every ~30 s and lags; average the GPU reading over the same 30 s.
    pairs, gpu_window = [], []
    for r in rows:
        g = num(r["gpu_w"])
        if g is not None:
            gpu_window.append(g)
        s = num(r["sys_w"])
        if s and s > 0 and gpu_window:
            pairs.append((statistics.mean(gpu_window[-3:]), s))
    if len(pairs) < 10:
        print(f"only {len(pairs)} on-battery samples; need at least 10")
        return
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    k = sum((x - mx) * (y - my) for x, y in pairs) / sum((x - mx) ** 2 for x in xs)
    base = my - k * mx
    idle = [y for x, y in pairs if x < 3]
    load = [(x, y) for x, y in pairs if x >= 10]
    print(f"samples on battery: {len(pairs)}  ({rows[0]['ts']} -> {rows[-1]['ts']})")
    print(f"fit: laptop_w = {k:.3f} * gpu_w + {base:.2f}")
    if idle:
        print(f"idle (GPU < 3 W):  laptop {statistics.mean(idle):.1f} W over {len(idle)} samples")
    if load:
        print(f"load (GPU >= 10 W): GPU {statistics.mean(x for x, _ in load):.1f} W, "
              f"laptop {statistics.mean(y for _, y in load):.1f} W over {len(load)} samples")
    if a.write:
        p = json.loads((DATA / "power.json").read_text(encoding="utf-8"))
        p.update({"calibrated": True, "k": round(k, 4), "base_w": round(base, 2),
                  "calibration_note": f"Fitted {datetime.now():%Y-%m-%d %H:%M} from {len(pairs)} on-battery samples: "
                                      f"laptop_w = {k:.3f} * gpu_w + {base:.2f} (battery discharge rate vs nvidia-smi)."})
        (DATA / "power.json").write_text(json.dumps(p, indent=1), encoding="utf-8")
        print("power.json updated")


if __name__ == "__main__":
    main()
