"""Mirror new upstream Archon issues into the crew repo so the local agents can work them
without touching the real repository.

Every mirrored body is defused before it is posted:
  - @mentions get a zero-width space after the @, so nobody is notified
  - #123 and owner/repo#123 references are broken the same way, so they neither link to the
    wrong fork issue nor create a backlink on the upstream issue
  - github.com links to upstream issues/PRs are broken so GitHub records no cross-reference

Usage:
  python mirror_issues.py --seed 40      # mirror the 40 most recent open upstream issues
  python mirror_issues.py --watch 300    # then poll every 5 minutes for new ones
"""
import argparse, json, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

UPSTREAM = "coleam00/Archon"
FORK = "dynamous-business/local-agent-crew-archon"
STATE = Path(__file__).resolve().parent.parent / "data" / "mirror_state.json"
ZW = "​"
LABELS = {
    "crew:queued": "c5def5", "crew:working": "fbca04", "crew:triaged": "0e8a16",
    "crew:pr-open": "1d76db", "needs-cloud-planning": "d93f0b", "crew:not-actionable": "cccccc",
}


def gh(*args, input=None, tries=4):
    for attempt in range(tries):
        r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", input=input)
        if r.returncode == 0:
            return r.stdout
        transient = any(s in r.stderr for s in ("timeout", "TLS", "connection", "502", "503", "EOF"))
        if not transient or args[:2] == ("issue", "create"):  # never retry a create: it may have landed
            break
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()}")


def defuse(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"https?://github\.com/", f"https://github{ZW}.com/", text)
    text = re.sub(r"(?<![\w`])@(?=[A-Za-z0-9])", "@" + ZW, text)
    text = re.sub(r"(?<![\w&/])#(?=\d)", "#" + ZW, text)
    text = re.sub(r"([\w.-]+/[\w.-]+)#(?=\d)", r"\1#" + ZW, text)
    return text


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"mirrored": {}, "since": None}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    merged = load_state()  # never drop entries another process saved in the meantime
    merged["mirrored"].update(s["mirrored"])
    merged["since"] = s.get("since") or merged.get("since")
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=1), encoding="utf-8")
    tmp.replace(STATE)  # atomic: a reader never sees half a file


def ensure_labels():
    have = {l["name"] for l in json.loads(gh("label", "list", "--repo", FORK, "--limit", "200", "--json", "name"))}
    for name, color in LABELS.items():
        if name not in have:
            gh("label", "create", name, "--repo", FORK, "--color", color)


def mirror_one(issue, state):
    num = str(issue["number"])
    # Re-read the file first: on 11/20 a seed run and the watcher each held their own copy of the state
    # and overwrote each other's saves, which mirrored 90 upstream issues twice.
    state["mirrored"].update(load_state()["mirrored"])
    if num in state["mirrored"]:
        return False
    full = json.loads(gh("api", f"repos/{UPSTREAM}/issues/{num}"))
    comments = json.loads(gh("api", f"repos/{UPSTREAM}/issues/{num}/comments?per_page=10"))
    parts = [
        f"> Mirrored from upstream Archon issue {num} (opened {full['created_at'][:10]}) "
        f"for the local agent crew experiment. Upstream labels: "
        f"{', '.join(l['name'] for l in full.get('labels', [])) or 'none'}.",
        "",
        defuse(full.get("body") or "(no description)"),
    ]
    for c in comments:
        parts += ["", "---", f"**Upstream comment ({c['created_at'][:10]}):**", "", defuse(c.get("body") or "")]
    body = "\n".join(parts)[:60000]
    url = gh("issue", "create", "--repo", FORK, "--title", defuse(full["title"]),
             "--label", "crew:queued", "--body-file", "-", input=body).strip()
    state["mirrored"][num] = {"fork_issue": int(url.rsplit("/", 1)[-1]), "title": full["title"],
                              "upstream_created": full["created_at"],
                              "mirrored_at": datetime.now(timezone.utc).isoformat()}
    save_state(state)
    print(f"MIRRORED upstream {num} -> {url}", flush=True)
    return True


def recent_open(limit):
    return json.loads(gh("issue", "list", "--repo", UPSTREAM, "--state", "open", "--limit", str(limit),
                         "--json", "number,createdAt", "--search", "sort:created-desc"))


def new_since(since_iso):
    q = f"repo:{UPSTREAM} is:issue created:>={since_iso[:19]}Z"
    out = json.loads(gh("api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=50",
                        "-f", "sort=created", "-f", "order=asc"))
    return [i for i in out["items"] if "pull_request" not in i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--watch", type=int, default=0, help="poll interval in seconds")
    a = ap.parse_args()
    state = load_state()
    ensure_labels()
    if a.seed:
        for issue in sorted(recent_open(a.seed), key=lambda i: i["number"]):
            mirror_one(issue, state)
    if not state["since"]:
        state["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        save_state(state)
    while a.watch:
        try:
            for issue in new_since(state["since"]):
                mirror_one(issue, state)
        except Exception as e:  # keep the watcher alive through API blips
            print(f"MIRROR_ERROR {e}", file=sys.stderr, flush=True)
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
