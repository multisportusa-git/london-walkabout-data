#!/usr/bin/env python3
"""Daily rollups of the hourly record (stdlib only, zero tokens).

Usage: python3 collector/rollup.py --data data

Writes data/_rollup/day/<YYYY-MM-DD>.json: one small file per UTC day holding, per hour, every line's
status category and the messages behind it (message -> hours), plus network metrics (buses, National Rail boards and
incidents, lifts, station notices, crowding, dataset coverage). The dashboard sums whichever days a
chosen time range covers, in the browser, so any range costs nothing to compute.

Derived files only: raw data is never touched. A day is (re)built only when its source files changed
size (today grows every hour; an append-only top-up changes an older day). No rollup is ever removed.
"""
import argparse, datetime, glob, gzip, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aggregate import line_statuses, line_worst, line_mode, tidy_reason  # noqa: E402
from collect import DATASETS as ALL_DATASETS, DAILY_AT_UTC  # noqa: E402

VERSION = 2  # bump to rebuild every day file when the format changes
READ = ["lineStatus", "busLineStatus", "liftDisruptions", "stopDisruptions", "crowdingReadings",
        "nationalRailDepartures", "nationalRailIncidents", "arrivalsReadings"]
DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.ndjson(\.gz)?$")


def day_files(data, ds, day):
    return [p for p in (os.path.join(data, ds, day + ".ndjson"), os.path.join(data, ds, day + ".ndjson.gz")) if os.path.exists(p)]


def read_day(data, ds, day):
    """{slot: record} for one dataset-day, last record per slot wins; corrupt lines skipped."""
    out = {}
    for p in day_files(data, ds, day):
        try:
            fh = gzip.open(p, "rt", encoding="utf-8", errors="replace") if p.endswith(".gz") else open(p, encoding="utf-8", errors="replace")
            with fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    s = r.get("slot") if isinstance(r, dict) else None
                    if isinstance(s, str) and s.startswith(day):
                        out[s] = r
        except (OSError, EOFError):
            continue
    return out


def health_day(data, day):
    """[(slot, {dataset: status})] from the run health lines (several runs per slot are possible)."""
    out = []
    p = os.path.join(data, "_health", day + ".ndjson")
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and str(r.get("slot", "")).startswith(day):
                    out.append((r["slot"], {k: (v or {}).get("status") for k, v in (r.get("datasets") or {}).items()}))
    return out


def src_sizes(data, day):
    s = {}
    for ds in READ:
        n = sum(os.path.getsize(p) for p in day_files(data, ds, day))
        if n: s[ds] = n
    p = os.path.join(data, "_health", day + ".ndjson")
    if os.path.exists(p): s["_health"] = os.path.getsize(p)
    return s


def all_days(data):
    days = set()
    for ds in READ + ["_health"]:
        for f in os.listdir(os.path.join(data, ds)) if os.path.isdir(os.path.join(data, ds)) else []:
            m = DAY_RE.match(f)
            if m: days.add(m.group(1))
    return sorted(days)


def H(slot):
    return int(slot[11:13])


def clip(t, n=480):
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def reason_key(st, name):
    inc = st.get("nrIncident")
    if isinstance(inc, dict) and inc.get("summary"):
        return clip(inc["summary"])
    r = (st.get("reason") or "").strip()
    if re.match(r"https?://", r):
        return "National Rail notice: " + r.rstrip("/").rsplit("/", 1)[-1][:120]
    return (clip(tidy_reason(r, name)) if r else None) or st.get("description") or "No reason given"


def nr_board(rec):
    F = rec.get("fields") or []
    if not isinstance(rec.get("stations"), dict) or not all(f in F for f in ("std", "etd", "isCancelled")):
        return None
    i_s, i_e, i_c = F.index("std"), F.index("etd"), F.index("isCancelled")
    tot = canc = late = 0
    for b in rec["stations"].values():
        for r in (b or {}).get("services") or []:
            tot += 1
            if r[i_c]:
                canc += 1; continue
            try:
                late += (datetime.datetime.fromisoformat(r[i_e]) - datetime.datetime.fromisoformat(r[i_s])).total_seconds() >= 300
            except Exception:
                pass
    return tot, canc, late


def roll_day(data, day):
    blank = lambda: [None] * 24
    m = {k: blank() for k in ("busGood", "busTotal", "nrDeps", "nrCanc", "nrLate", "nrInc", "nrUnpl",
                              "lifts", "liftStations", "notices", "noticeStations")}
    lines, crowd = {}, {}
    cov = {ds: (0 if ds in DAILY_AT_UTC else [0] * 24) for ds in ALL_DATASETS}
    seen = {}

    for s, r in read_day(data, "lineStatus", day).items():
        h = H(s)
        for x in r.get("lines") or []:
            if not isinstance(x, dict) or not x.get("id"):
                continue
            e = lines.setdefault(x["id"], {"n": x.get("name") or x["id"], "m": line_mode(x), "c": blank(), "r": {}})
            cat = line_worst(line_statuses(x))[0]
            e["c"][h] = cat
            if cat not in (0, 5, None):   # 5 = closed for the night: not a disruption
                for k in {reason_key(st, e["n"]) for st in x.get("statuses") or [] if isinstance(st, dict)
                          and st.get("severity") != 10 and st.get("description") != "Good Service"}:
                    e["r"].setdefault(k, []).append(h)   # hours (UTC) this message applied
        seen.setdefault("lineStatus", set()).add(h)
    for s, r in read_day(data, "busLineStatus", day).items():
        tot = r.get("totalLines")
        if isinstance(tot, int):
            good = r.get("goodServiceCount")
            m["busTotal"][H(s)] = tot
            m["busGood"][H(s)] = good if isinstance(good, int) else tot - len(r.get("disrupted") or [])
        seen.setdefault("busLineStatus", set()).add(H(s))
    for s, r in read_day(data, "liftDisruptions", day).items():
        D = [x for x in r.get("disruptions") or [] if isinstance(x, dict)]
        m["lifts"][H(s)] = r["count"] if isinstance(r.get("count"), int) else len(D)
        m["liftStations"][H(s)] = len({x.get("stationUniqueId") for x in D if x.get("stationUniqueId")})
        seen.setdefault("liftDisruptions", set()).add(H(s))
    for s, r in read_day(data, "stopDisruptions", day).items():
        D = [x for x in r.get("disruptions") or [] if isinstance(x, dict)]
        m["notices"][H(s)] = len(D)
        m["noticeStations"][H(s)] = len({x.get("atcoCode") or x.get("stationAtcoCode") or x.get("commonName") for x in D})
        seen.setdefault("stopDisruptions", set()).add(H(s))
    for s, r in read_day(data, "crowdingReadings", day).items():
        st = r.get("stations") if isinstance(r.get("stations"), dict) else (r.get("readings") if isinstance(r.get("readings"), dict) else {})
        for sid, v in st.items():
            p = v.get("percentageOfBaseline") if isinstance(v, dict) else None
            if isinstance(v, dict) and v.get("dataAvailable") is not False and isinstance(p, (int, float)):
                crowd.setdefault(sid, blank())[H(s)] = round(100 * p, 1)
        seen.setdefault("crowdingReadings", set()).add(H(s))
    for s, r in read_day(data, "nationalRailDepartures", day).items():
        b = nr_board(r)
        if b:
            m["nrDeps"][H(s)], m["nrCanc"][H(s)], m["nrLate"][H(s)] = b
        seen.setdefault("nationalRailDepartures", set()).add(H(s))
    for s, r in read_day(data, "nationalRailIncidents", day).items():
        if isinstance(r.get("count"), int):
            m["nrInc"][H(s)] = r["count"]
            m["nrUnpl"][H(s)] = r["count"] - (r.get("planned") or 0)
        seen.setdefault("nationalRailIncidents", set()).add(H(s))
    for s in read_day(data, "arrivalsReadings", day):
        seen.setdefault("arrivalsReadings", set()).add(H(s))
    for s, st in health_day(data, day):
        for ds, v in st.items():
            if v in ("ok", "partial"):
                seen.setdefault(ds, set()).add(H(s))
    for ds, hours in seen.items():
        if ds not in cov:
            continue
        if ds in DAILY_AT_UTC:
            cov[ds] = 1 if hours else 0
        else:
            for h in hours: cov[ds][h] = 1
    return {"day": day, "v": VERSION, "src": src_sizes(data, day), "lines": lines, "m": m, "crowd": crowd, "cov": cov}


def update(data):
    """Build missing or stale day rollups. Returns the days written."""
    out_dir = os.path.join(data, "_rollup", "day")
    os.makedirs(out_dir, exist_ok=True)
    built = []
    for day in all_days(data):
        p = os.path.join(out_dir, day + ".json")
        try:
            with open(p, encoding="utf-8") as fh:
                old = json.load(fh)
            if old.get("v") == VERSION and old.get("src") == src_sizes(data, day):
                continue
        except (OSError, ValueError):
            pass
        R = roll_day(data, day)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(R, fh, separators=(",", ":"))
        os.replace(tmp, p)
        built.append(day)
    return built


def index(data):
    d = os.path.join(data, "_rollup", "day")
    days = sorted(f[:10] for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
    first_slot, cov_first = None, {}
    for day in days:
        try:
            with open(os.path.join(d, day + ".json"), encoding="utf-8") as fh:
                R = json.load(fh)
        except (OSError, ValueError):
            continue
        hrs = [h for l in R.get("lines", {}).values() for h, c in enumerate(l["c"]) if c is not None]
        if hrs and not first_slot:
            first_slot = f"{day}T{min(hrs):02d}:00:00Z"
        for ds, v in (R.get("cov") or {}).items():   # first hour each dataset was recorded (gaps are measured from there)
            if ds in cov_first: continue
            h = (0 if v else None) if not isinstance(v, list) else next((i for i, x in enumerate(v) if x), None)
            if h is not None: cov_first[ds] = f"{day}T{h:02d}:00:00Z"
    return {"v": VERSION, "days": days, "first": days[0] if days else None, "last": days[-1] if days else None,
            "firstSlot": first_slot, "covFirst": cov_first}


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--data", default="data")
    a = ap.parse_args(argv)
    built = update(a.data)
    print(f"rollup: {len(built)} day(s) built" + (f" ({built[0]} .. {built[-1]})" if built else ""))


if __name__ == "__main__":
    main()
