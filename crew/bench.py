"""Quick throughput check against the local server: N concurrent requests with a ~6K-token prompt
(a real Archon source file), each generating up to 512 tokens. Prints llama-server's own timings.

  python bench.py --concurrency 1
  python bench.py --concurrency 4
"""
import argparse, json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SRC = Path(r"C:\Users\ASUS\proart-crew\archon-local-crew\packages\workflows\src")


def prompt(i):
    files = sorted(SRC.rglob("*.ts"), key=lambda p: -p.stat().st_size)
    code = files[i % 8].read_text(encoding="utf-8")[:24000]
    return f"Review this file and list the three riskiest functions with one sentence each.\n\n```ts\n{code}\n```"


def call(i, model):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt(i)}], "max_tokens": 512,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request("http://127.0.0.1:9931/v1/chat/completions", body, {"Content-Type": "application/json"})
    t0 = time.time()
    r = json.loads(urllib.request.urlopen(req, timeout=900).read())
    t = r["timings"]
    return {"wall_s": round(time.time() - t0, 1), "prompt_n": t["prompt_n"], "prompt_tps": round(t["prompt_per_second"], 1),
            "gen_n": t["predicted_n"], "gen_tps": round(t["predicted_per_second"], 1),
            "draft_accept": round(t.get("draft_n_accepted", 0) / t["draft_n"], 2) if t.get("draft_n") else None}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--model", default="qwen3.8-flash-next")
    a = ap.parse_args()
    t0 = time.time()
    with ThreadPoolExecutor(a.concurrency) as ex:
        res = list(ex.map(lambda i: call(i, a.model), range(a.concurrency)))
    wall = time.time() - t0
    for r in res:
        print(r)
    total_gen = sum(r["gen_n"] for r in res)
    print(f"concurrency={a.concurrency} wall={wall:.1f}s aggregate_gen_tps={total_gen / wall:.1f}")
