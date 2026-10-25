#!/usr/bin/env python3
"""Keep the list of every London bus route id in config/busRoutes.json as {"routes": [...], "fetched": "YYYY-MM-DD"}.

TfL's bus status rollups only name routes that have a notice, so the dashboard's favourites picker needs this
full list (GET /Line/Mode/bus, one call, ~675 routes). The app key is optional (read from the environment
variable named by --key-env, sent as app_key); never printed.
Append-only: a route that disappears from the feed is kept. Rewritten only when the content changes.
A feed with fewer than MIN_ROUTES routes is refused. Exit codes: 0 ok, 1 failure.
"""
import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = "https://api.tfl.gov.uk/Line/Mode/bus"
MIN_ROUTES = 300  # the real list has ~675


def sort_ids(ids):
    return sorted(ids, key=lambda s: (0, int(s), s) if s.isdigit() else (1, 0, s))


def route_ids(data):
    if not isinstance(data, list):
        raise ValueError("bus route list is not a JSON array")
    ids = set()
    for r in data:
        if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"].strip():
            ids.add(r["id"].strip())
    return sort_ids(ids)


def update(path, data, today):
    """Merge the feed into the file at `path`. Returns True if the file was written."""
    new = route_ids(data)
    if len(new) < MIN_ROUTES:
        raise ValueError(f"only {len(new)} routes in the feed; refusing to write")
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        old_routes = old.get("routes") if isinstance(old, dict) else None
        if not isinstance(old_routes, list):
            old_routes = []
    except (OSError, ValueError):
        old_routes = []
    merged = sort_ids(set(old_routes) | set(new))
    if old_routes and merged == sort_ids(set(old_routes)):
        return False
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"routes": merged, "fetched": today}, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    return True


def fetch(key):
    url = URL + ("?" + urllib.parse.urlencode({"app_key": key}) if key else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "london-walkabout-busroutes/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-env", default="TFL_APP_KEY")
    ap.add_argument("--out", default="config/busRoutes.json")
    a = ap.parse_args(argv)
    try:
        data = fetch(os.environ.get(a.key_env, ""))
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        changed = update(a.out, data, today)
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("updated" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
