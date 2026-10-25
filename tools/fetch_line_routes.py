#!/usr/bin/env python3
"""Keep every TfL rail line's route (ordered stops with coordinates, per branch) in config/lineRoutes.json:
    {"lines": {id: {"n": name, "m": mode, "b": [[stopId, ...], ...]}}, "stops": {id: [name, lat, lon]}, "fetched": day}
The dashboard uses it to draw a line's route and highlight the stretch named in a disruption ("between X and Y").
Source: TfL GET /Line/{id}/Route/Sequence/{outbound|inbound} for tube, DLR, Overground, Elizabeth line and tram
(about 2 calls per line). The app key is optional (environment variable named by --key-env, sent as app_key,
never printed). Append-only: lines, branches and stops that disappear from the feed are kept. Rewritten only when
the content changes. A feed with fewer than MIN_LINES lines is refused. Exit codes: 0 ok, 1 failure.
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.tfl.gov.uk"
MODES = "tube,dlr,overground,elizabeth-line,tram"
MIN_LINES = 10  # the real set has ~20


def _num(x):
    return round(float(x), 5)


def build(responses):
    """{line id: [Route/Sequence responses]} -> {"lines": ..., "stops": ...}."""
    lines, stops = {}, {}
    for lid, rs in responses.items():
        seen, branches, name, mode = set(), [], None, None
        for r in rs or []:
            if not isinstance(r, dict):
                continue
            name = name or r.get("lineName")
            mode = mode or r.get("mode")
            for seq in r.get("stopPointSequences") or []:
                ids = []
                for p in seq.get("stopPoint") or []:
                    try:
                        sid, lat, lon = p["id"], _num(p["lat"]), _num(p["lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not sid or ids[-1:] == [sid]:
                        continue
                    stops[sid] = [str(p.get("name") or sid), lat, lon]
                    ids.append(sid)
                key = tuple(ids) if ids <= ids[::-1] else tuple(ids[::-1])
                if len(ids) >= 2 and key not in seen:
                    seen.add(key)
                    branches.append(ids)
        if branches:
            lines[lid] = {"n": name or lid, "m": mode or "", "b": branches}
    return {"lines": lines, "stops": stops}


def _canon(b):
    return tuple(b) if b <= b[::-1] else tuple(b[::-1])


def merge(old, new):
    lines = {k: dict(v) for k, v in (old.get("lines") or {}).items()}
    for lid, v in (new.get("lines") or {}).items():
        cur = lines.get(lid)
        if not cur:
            lines[lid] = v
            continue
        have = {_canon(b) for b in cur["b"]}
        add = [b for b in v["b"] if _canon(b) not in have]
        lines[lid] = {"n": v.get("n") or cur["n"], "m": v.get("m") or cur["m"], "b": cur["b"] + add}
    stops = dict(old.get("stops") or {})
    stops.update(new.get("stops") or {})
    return {"lines": lines, "stops": stops}


def update(path, responses, today):
    """Merge a fresh feed into the file at `path`. Returns True if the file was written."""
    new = build(responses)
    if len(new["lines"]) < MIN_LINES:
        raise ValueError(f"only {len(new['lines'])} lines in the feed; refusing to write")
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
    merged = merge(old, new)
    if old and merged["lines"] == old.get("lines") and merged["stops"] == old.get("stops"):
        return False
    merged["fetched"] = today
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fh.write("\n")
    return True


def _get(url, key):
    if key:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"app_key": key})
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "london-walkabout-lineroutes/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_all(key):
    ids = [x["id"] for x in _get(f"{API}/Line/Mode/{MODES}", key)]
    out = {}
    for lid in ids:
        rs = []
        for d in ("outbound", "inbound"):
            try:
                rs.append(_get(f"{API}/Line/{lid}/Route/Sequence/{d}", key))
            except (urllib.error.URLError, ValueError):
                pass
            time.sleep(0.3)
        out[lid] = rs
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-env", default="TFL_APP_KEY")
    ap.add_argument("--out", default="config/lineRoutes.json")
    a = ap.parse_args(argv)
    try:
        data = fetch_all(os.environ.get(a.key_env, ""))
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        changed = update(a.out, data, today)
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("updated" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
