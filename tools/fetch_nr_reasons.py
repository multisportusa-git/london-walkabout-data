#!/usr/bin/env python3
"""Fetch Darwin's delay/cancellation reason texts from the Rail Data Marketplace "Reference Data" product
(GetReasonCodeList) and keep them in config/nrReasonCodes.json as
    {"canc": {code: text}, "late": {code: text}, "fetched": "YYYY-MM-DD"}.

- The key is read from the environment variable named by --key-env (never printed, sent only in x-apikey).
- Append-only: new texts replace old ones, but a code that disappears from the feed is kept (nothing is pruned).
- The file is rewritten (and `fetched` bumped) only when the content changes.
- A feed with fewer than MIN_CODES reasons is refused and nothing is written.
Exit codes: 0 ok (changed or not), 1 fetch/validation failure.
"""
import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

URL = "https://api1.raildata.org.uk/1010-reference-data1_0/LDBSVWS/api/ref/20211101/GetReasonCodeList"
MIN_CODES = 100  # the real list has ~416 codes


def normalise(rows):
    """[{code, lateReason, cancReason}, ...] -> {"canc": {"501": text}, "late": {"501": text}}."""
    if not isinstance(rows, list):
        raise ValueError("reason list is not a JSON array")
    canc, late = {}, {}
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("code"), (int, str)) or isinstance(r.get("code"), bool):
            continue
        code = str(r["code"]).strip()
        if not code:
            continue
        for field, dest in (("cancReason", canc), ("lateReason", late)):
            t = r.get(field)
            if isinstance(t, str) and t.strip():
                dest[code] = t.strip()
    return {"canc": canc, "late": late}


def merge(old, new):
    out = {}
    for k in ("canc", "late"):
        d = dict(old.get(k) or {})
        d.update(new.get(k) or {})
        out[k] = d
    return out


def update(path, rows, today):
    """Merge `rows` into the file at `path`. Returns True if the file was written."""
    new = normalise(rows)
    if max(len(new["canc"]), len(new["late"])) < MIN_CODES:
        raise ValueError(f"only {len(new['canc'])}/{len(new['late'])} reasons in the feed; refusing to write")
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
    merged = merge(old, new)
    if old and merged["canc"] == old.get("canc") and merged["late"] == old.get("late"):
        return False
    merged["fetched"] = today
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fh.write("\n")
    return True


def fetch(key):
    req = urllib.request.Request(URL, headers={"x-apikey": key, "Accept": "application/json",
                                               "User-Agent": "london-walkabout-reasons/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-env", default="NR_REF_KEY")
    ap.add_argument("--out", default="config/nrReasonCodes.json")
    a = ap.parse_args(argv)
    key = os.environ.get(a.key_env, "")
    if not key:
        print(f"no key in ${a.key_env}", file=sys.stderr)
        return 1
    try:
        rows = fetch(key)
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        changed = update(a.out, rows, today)
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("updated" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
