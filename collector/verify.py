#!/usr/bin/env python3
"""Check the last N hours for missing hourly slots, per dataset (stdlib only).

Every hour is expected for every dataset (no closure-window exemptions).
Exit 0 always, except exit 3 if any NDJSON line is corrupt.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATASETS = ["liftDisruptions", "lineStatus", "stopDisruptions", "crowdingReadings", "arrivalsReadings", "arrivalsDetail",
            "busLineStatus", "networkDisruptions", "crowdingNetwork", "nationalRailDepartures",
            "nationalRailIncidents", "nationalRailStations", "stationAccess"]
DAILY_AT_UTC = {"nationalRailStations": 3, "stationAccess": 3}  # once per UTC day: one record per day expected, due from this hour
GRACE = timedelta(minutes=20)  # a slot is "due" 20 min after its hour starts


def floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_slots(data, dataset, start, end):
    """Return (set of slot datetimes, corrupt-line list) for day-files covering [start, end]."""
    slots, corrupt, live = set(), [], set()
    day = floor_hour(start).replace(hour=0)
    while day <= end:
        p = Path(data) / dataset / (day.strftime("%Y-%m-%d") + ".ndjson")
        if not p.exists() and Path(str(p) + ".gz").exists():
            p = Path(str(p) + ".gz")
        if p.exists():
            with (gzip.open(p, "rt", encoding="utf-8") if str(p).endswith(".gz") else open(p, encoding="utf-8")) as fh:
                for n, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        s = rec.get("slot")
                        ts = parse_ts(s) if s else floor_hour(parse_ts(rec["polledAt"]))
                        slots.add(ts)
                        if "legacyDocId" not in rec:
                            live.add(ts)
                    except Exception:
                        corrupt.append(f"{p}:{n}")
        day += timedelta(days=1)
    return slots, corrupt, live


def merge_ranges(missing):
    out, run = [], []
    for m in sorted(missing):
        if run and m - run[-1] == timedelta(hours=1):
            run.append(m)
        else:
            if run:
                out.append((run[0], run[-1]))
            run = [m]
    if run:
        out.append((run[0], run[-1]))
    return out


def check(data, hours, now):
    end = floor_hour(now - GRACE)  # last slot that should exist
    start = floor_hour(now) - timedelta(hours=hours)
    found, corrupt, first_live = {}, [], {}
    for d in DATASETS:
        s, c, live = read_slots(data, d, start, end)
        found[d] = s
        corrupt += c
        first_live[d] = min(live) if live else None
    # Gaps are judged from each dataset's first collector-written record: history imported from the
    # old database (legacyDocId) has known gaps and must not trip the watchdog. A dataset that stops
    # writing altogether is caught by the collect job itself (exit code 2 -> red run -> email).
    result = {}
    for d in DATASETS:
        if first_live[d] is None:
            result[d] = {"expected": 0, "missing": []}
            continue
        b = max(start, first_live[d])
        exp = []
        if d in DAILY_AT_UTC:
            days = {h.replace(hour=0) for h in found[d]}
            day = b.replace(hour=0)
            while day.replace(hour=DAILY_AT_UTC[d]) <= end:
                exp.append(day.replace(hour=DAILY_AT_UTC[d]))
                day += timedelta(days=1)
            result[d] = {"expected": len(exp), "missing": [h for h in exp if h.replace(hour=0) not in days]}
            continue
        h = b
        while h <= end:
            exp.append(h)
            h += timedelta(hours=1)
        result[d] = {"expected": len(exp), "missing": [h for h in exp if h not in found[d]]}
    return result, corrupt


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--issue-file")
    ap.add_argument("--now", help="ISO UTC override for testing")
    a = ap.parse_args(argv)
    now = parse_ts(a.now) if a.now else datetime.now(timezone.utc)
    result, corrupt = check(a.data, a.hours, now)
    print(f"verify: window={a.hours}h now={now:%Y-%m-%dT%H:%MZ}")
    lines, worst = [], 0
    for d, r in result.items():
        miss = r["missing"]
        worst = max(worst, len(miss))
        ranges = merge_ranges(miss)
        txt = ", ".join(f"{s:%d %b %H:00}" + (f"-{e:%H:00}" if e != s else "") for s, e in ranges) or "none"
        print(f"  {d:18s} {r['expected'] - len(miss)}/{r['expected']} present; missing: {txt}")
        lines.append(f"| `{d}` | {r['expected'] - len(miss)}/{r['expected']} | {len(miss)} | {txt} |")
    if corrupt:
        print(f"CORRUPT lines: {len(corrupt)} e.g. {corrupt[:3]}")
        return 3
    if a.issue_file and worst > 2:
        body = (f"## Collector gap detected\n\nChecked the last {a.hours} h (as of {now:%Y-%m-%d %H:%M} UTC). "
                "More than 2 hourly slots are missing for at least one dataset.\n\n"
                "| Dataset | Present | Missing | Missing slots (UTC) |\n|---|---|---|---|\n" + "\n".join(lines) +
                "\n\nCheck the *collect* workflow runs (Actions tab) and `data/_health/`. "
                "TfL has no backfill, so missing hours cannot be recovered.\n")
        Path(a.issue_file).write_text(body, encoding="utf-8")
        print(f"issue body written to {a.issue_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
