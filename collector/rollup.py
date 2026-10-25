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

VERSION = 7  # bump to rebuild every day file when the format changes
GO_LIVE = "2026-10-24T21:00:00Z"   # new collector took over (old Claude task before this)
INCIDENT = ["2026-09-22T15:00:00Z", "2026-09-25T15:00:00Z"]
READ = ["lineStatus", "busLineStatus", "arrivalsNetwork", "crowdingNetwork", "liftDisruptions", "stopDisruptions", "crowdingReadings",
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
    """[(slot, {dataset: status}, line)] from the run health lines (several runs per slot are possible)."""
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
                    out.append((r["slot"], {k: (v or {}).get("status") for k, v in (r.get("datasets") or {}).items()}, r))
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


INFO_RE = re.compile(r"^\W*(cleared|lines? (have )?re-?opened|normal service|services? (has |have )?resumed)", re.I)
KIND_RANK = {"u": 3, "p": 2, "i": 1}


def incident_of(st, look):
    """The National Rail incident behind a link-only status: stored with it (from 25 Oct) or looked up in the KB archive."""
    inc = st.get("nrIncident")
    if isinstance(inc, dict) and inc.get("summary"):
        return inc
    r = (st.get("reason") or "").strip()
    if look and re.match(r"https?://", r):
        x = look(r)
        if x:
            return {"summary": x.get("summary"), "planned": x.get("planned")}
    return None


def status_kind(st, inc):
    """u = unplanned disruption, p = planned works, i = information only (e.g. a cleared incident)."""
    if inc is not None or re.match(r"https?://", (st.get("reason") or "").strip()):
        if not inc: return "u"
        if INFO_RE.search(inc.get("summary") or ""): return "i"
        return "p" if inc.get("planned") else "u"
    cat = (st.get("disruption") or {}).get("category")
    if cat == "PlannedWork": return "p"
    if cat == "Information": return "i"
    if cat == "RealTime": return "u"
    return "p" if st.get("description") in ("Planned Closure", "Part Closure") else "u"


def reason_key(st, name, inc=None):
    inc = inc or (st.get("nrIncident") if isinstance(st.get("nrIncident"), dict) else None)
    if inc and inc.get("summary"):
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


def nr_detail(rec, h, nrs, nro, blank):
    """Per station (CRS) and per operator: departures, cancelled, 5+ min late; per station mean loading %.
    Also Darwin reason codes, sparse as {code: {hour: n}}: rc = cancelReason on cancelled services,
    rl = delayReason on running ones (any delay). Boards recorded without those fields give empty maps."""
    F = rec.get("fields") or []
    ix = {f: F.index(f) for f in ("std", "etd", "isCancelled", "operatorCode", "loadingPct") if f in F}
    if len(ix) < 4: return
    i_cr = F.index("cancelReason") if "cancelReason" in F else None
    i_dr = F.index("delayReason") if "delayReason" in F else None
    for crs, b in (rec.get("stations") or {}).items():
        svs = (b or {}).get("services")
        if svs is None:   # board unavailable this hour
            continue
        S = nrs.setdefault(crs, {"d": blank(), "c": blank(), "l": blank(), "ld": blank(), "rc": {}, "rl": {}})
        if S["d"][h] is None: S["d"][h], S["c"][h], S["l"][h] = 0, 0, 0
        ld = []
        for sv in svs:
            op = sv[ix["operatorCode"]] or "?"
            O = nro.setdefault(op, {"d": blank(), "c": blank(), "l": blank(), "rc": {}, "rl": {}})
            late = 0
            canc = 1 if sv[ix["isCancelled"]] else 0
            if not canc:
                try:
                    late = 1 if (datetime.datetime.fromisoformat(sv[ix["etd"]]) - datetime.datetime.fromisoformat(sv[ix["std"]])).total_seconds() >= 300 else 0
                except Exception:
                    late = 0
            i_r = i_cr if canc else i_dr
            code = sv[i_r] if i_r is not None and i_r < len(sv) else None
            for X in (S, O):
                X["d"][h] = (X["d"][h] or 0) + 1; X["c"][h] = (X["c"][h] or 0) + canc; X["l"][h] = (X["l"][h] or 0) + late
                if code is not None and code != "":
                    per_hour = X["rc" if canc else "rl"].setdefault(str(code), {})
                    per_hour[str(h)] = per_hour.get(str(h), 0) + 1
            if "loadingPct" in ix and isinstance(sv[ix["loadingPct"]], (int, float)): ld.append(sv[ix["loadingPct"]])
        if ld: S["ld"][h] = round(sum(ld) / len(ld), 1)


def roll_bus_day(data, day):
    """Bus routes for one day: totals per hour and, for each disrupted route, the kind of disruption per hour and TfL's messages."""
    blank = lambda: [None] * 24
    out = {"day": day, "v": VERSION, "t": blank(), "g": blank(), "routes": {}}
    for s, r in read_day(data, "busLineStatus", day).items():
        h = H(s); tot = r.get("totalLines")
        if not isinstance(tot, int): continue
        D = [x for x in r.get("disrupted") or [] if isinstance(x, dict) and x.get("id")]
        good = r.get("goodServiceCount")
        out["t"][h], out["g"][h] = tot, good if isinstance(good, int) else tot - len(D)
        for x in D:
            e = out["routes"].setdefault(x["id"], {"k": blank(), "r": {}, "rk": {}})
            hk = None
            for st in x.get("statuses") or []:
                if not isinstance(st, dict) or st.get("severity") == 10: continue
                kind = status_kind(st, None)
                k = clip(re.sub(r"\s+", " ", (st.get("reason") or st.get("description") or "No reason given")).strip())
                hours = e["r"].setdefault(k, [])
                if not hours or hours[-1] != h: hours.append(h)
                if KIND_RANK[kind] > KIND_RANK.get(e["rk"].get(k), 0): e["rk"][k] = kind
                if KIND_RANK[kind] > KIND_RANK.get(hk, 0): hk = kind
            e["k"][h] = hk or "u"
    return out


def roll_day(data, day, look=None):
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
                hk, done = None, set()
                for st in x.get("statuses") or []:
                    if not isinstance(st, dict) or st.get("severity") == 10 or st.get("description") == "Good Service":
                        continue
                    inc = incident_of(st, look)
                    k, kind = reason_key(st, e["n"], inc), status_kind(st, inc)
                    if k not in done:
                        e["r"].setdefault(k, []).append(h); done.add(k)   # hours (UTC) this message applied
                    rk = e.setdefault("rk", {})
                    if KIND_RANK[kind] > KIND_RANK.get(rk.get(k), 0): rk[k] = kind
                    if KIND_RANK[kind] > KIND_RANK.get(hk, 0): hk = kind
                e.setdefault("k", blank())[h] = hk or "u"
        seen.setdefault("lineStatus", set()).add(h)
    for s, r in read_day(data, "busLineStatus", day).items():
        tot = r.get("totalLines")
        if isinstance(tot, int):
            good = r.get("goodServiceCount")
            m["busTotal"][H(s)] = tot
            m["busGood"][H(s)] = good if isinstance(good, int) else tot - len(r.get("disrupted") or [])
        seen.setdefault("busLineStatus", set()).add(H(s))
    lf, sd, ar, am, al = {}, {}, {}, {}, {}
    for s, r in read_day(data, "liftDisruptions", day).items():
        D = [x for x in r.get("disruptions") or [] if isinstance(x, dict)]
        for x in D:
            sid = x.get("stationUniqueId")
            if not sid: continue
            msg = clip(re.sub(r"\s+", " ", x.get("message") or "Lift out of service").strip())
            e = lf.setdefault(sid, {"n": (msg.split(":")[0].strip() if ":" in msg[:60] else sid), "o": blank(), "r": {}})
            e["o"][H(s)] = (e["o"][H(s)] or 0) + max(1, len(x.get("disruptedLiftUniqueIds") or []))
            hrs = e["r"].setdefault(msg, [])
            if not hrs or hrs[-1] != H(s): hrs.append(H(s))
        m["lifts"][H(s)] = r["count"] if isinstance(r.get("count"), int) else len(D)
        m["liftStations"][H(s)] = len({x.get("stationUniqueId") for x in D if x.get("stationUniqueId")})
        seen.setdefault("liftDisruptions", set()).add(H(s))
    for s, r in read_day(data, "stopDisruptions", day).items():
        D = [x for x in r.get("disruptions") or [] if isinstance(x, dict)]
        m["notices"][H(s)] = len(D)
        for x in D:
            k = x.get("stationAtcoCode") or x.get("atcoCode") or x.get("commonName")
            if not k: continue
            e = sd.setdefault(k, {"n": x.get("commonName") or k, "c": blank(), "r": {}})
            e["c"][H(s)] = (e["c"][H(s)] or 0) + 1
            msg = clip(re.sub(r"\s+", " ", x.get("description") or "Notice").strip())
            hrs = e["r"].setdefault(msg, [])
            if not hrs or hrs[-1] != H(s): hrs.append(H(s))
        m["noticeStations"][H(s)] = len({x.get("atcoCode") or x.get("stationAtcoCode") or x.get("commonName") for x in D})
        seen.setdefault("stopDisruptions", set()).add(H(s))
    for s, r in read_day(data, "crowdingReadings", day).items():
        st = r.get("stations") if isinstance(r.get("stations"), dict) else (r.get("readings") if isinstance(r.get("readings"), dict) else {})
        for sid, v in st.items():
            p = v.get("percentageOfBaseline") if isinstance(v, dict) else None
            if isinstance(v, dict) and v.get("dataAvailable") is not False and isinstance(p, (int, float)):
                crowd.setdefault(sid, blank())[H(s)] = round(100 * p, 1)
        seen.setdefault("crowdingReadings", set()).add(H(s))
    nrs, nro = {}, {}
    for s, r in read_day(data, "nationalRailDepartures", day).items():
        b = nr_board(r)
        if b:
            m["nrDeps"][H(s)], m["nrCanc"][H(s)], m["nrLate"][H(s)] = b
            nr_detail(r, H(s), nrs, nro, blank)
        seen.setdefault("nationalRailDepartures", set()).add(H(s))
    for s, r in read_day(data, "nationalRailIncidents", day).items():
        if isinstance(r.get("count"), int):
            m["nrInc"][H(s)] = r["count"]
            m["nrUnpl"][H(s)] = r["count"] - (r.get("planned") or 0)
        seen.setdefault("nationalRailIncidents", set()).add(H(s))
    for s, r in read_day(data, "arrivalsReadings", day).items():
        for sid, v in (r.get("stations") or {}).items():
            if isinstance(v, dict) and isinstance(v.get("totalPredictions"), int):
                ar.setdefault(sid, blank())[H(s)] = v["totalPredictions"]
        seen.setdefault("arrivalsReadings", set()).add(H(s))
    for s, r in read_day(data, "arrivalsNetwork", day).items():
        F = r.get("fields") or []
        li = F.index("lineId") if "lineId" in F else 1
        for mode, rows in (r.get("modes") or {}).items():
            rows = rows if isinstance(rows, list) else []
            am.setdefault(mode, blank())[H(s)] = len(rows)
            cnt = {}
            for row in rows:
                if isinstance(row, list) and len(row) > li and row[li]: cnt[row[li]] = cnt.get(row[li], 0) + 1
            for ln, n in cnt.items():
                al.setdefault(ln, blank())[H(s)] = n
    runs = []
    for s, st, r in health_day(data, day):
        for ds, v in st.items():
            if v in ("ok", "partial"):
                seen.setdefault(ds, set()).add(H(s))
        runs.append({"h": H(s), "t": r.get("startedAt"), "d": r.get("durationS"), "q": r.get("requests"), "r": r.get("retries"),
                     "x": r.get("http429"), "k": r.get("keyUsed"), "nr": r.get("nrStatus") or {},
                     "bad": sorted(ds for ds, v in st.items() if v == "failed")})
    for ds, hours in seen.items():
        if ds not in cov:
            continue
        if ds in DAILY_AT_UTC:
            cov[ds] = 1 if hours else 0
        else:
            for h in hours: cov[ds][h] = 1
    size = {}
    for ds in ALL_DATASETS:
        n = sum(os.path.getsize(p) for p in day_files(data, ds, day))
        if n: size[ds] = n
    return {"day": day, "v": VERSION, "src": src_sizes(data, day), "lines": lines, "m": m, "crowd": crowd, "cov": cov,
            "runs": runs, "bytes": size, "nrs": nrs, "nro": nro, "lf": lf, "sd": sd, "ar": ar, "am": am, "al": al}


def update(data):
    """Build missing or stale day rollups. Returns the days written."""
    out_dir = os.path.join(data, "_rollup", "day")
    os.makedirs(out_dir, exist_ok=True)
    built, cache = [], {}

    def look(url):   # KB incident archive, loaded once and only if a day needs it
        if "f" not in cache:
            try:
                from aggregate import nr_incident_lookup
                cache["f"] = nr_incident_lookup(data)
            except Exception:
                cache["f"] = lambda u: None
        return cache["f"](url)
    for day in all_days(data):
        p = os.path.join(out_dir, day + ".json")
        try:
            with open(p, encoding="utf-8") as fh:
                old = json.load(fh)
            if old.get("v") == VERSION and old.get("src") == src_sizes(data, day):
                continue
        except (OSError, ValueError):
            pass
        R = roll_day(data, day, look)
        if day_files(data, "crowdingNetwork", day):
            cdir = os.path.join(data, "_rollup", "cn"); os.makedirs(cdir, exist_ok=True)
            cs = {}
            for sl, r in read_day(data, "crowdingNetwork", day).items():
                for sid, v in (r.get("stations") or {}).items():
                    p_ = v.get("percentageOfBaseline") if isinstance(v, dict) else None
                    if isinstance(v, dict) and v.get("dataAvailable") and isinstance(p_, (int, float)):
                        cs.setdefault(sid, [None] * 24)[H(sl)] = round(100 * p_, 1)
            with open(os.path.join(cdir, day + ".json.tmp"), "w", encoding="utf-8") as fh:
                json.dump({"day": day, "v": VERSION, "s": cs}, fh, separators=(",", ":"))
            os.replace(os.path.join(cdir, day + ".json.tmp"), os.path.join(cdir, day + ".json"))
        bdir = os.path.join(data, "_rollup", "bus"); os.makedirs(bdir, exist_ok=True)
        if day_files(data, "busLineStatus", day):
            with open(os.path.join(bdir, day + ".json.tmp"), "w", encoding="utf-8") as fh:
                json.dump(roll_bus_day(data, day), fh, separators=(",", ":"))
            os.replace(os.path.join(bdir, day + ".json.tmp"), os.path.join(bdir, day + ".json"))
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(R, fh, separators=(",", ":"))
        os.replace(tmp, p)
        built.append(day)
    return built


def index(data):
    d = os.path.join(data, "_rollup", "day")
    days = sorted(f[:10] for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
    first_slot, cov_first, total = None, {}, {}
    for day in days:
        try:
            with open(os.path.join(d, day + ".json"), encoding="utf-8") as fh:
                R = json.load(fh)
        except (OSError, ValueError):
            continue
        hrs = [h for l in R.get("lines", {}).values() for h, c in enumerate(l["c"]) if c is not None]
        if hrs and not first_slot:
            first_slot = f"{day}T{min(hrs):02d}:00:00Z"
        for ds, n in (R.get("bytes") or {}).items():
            total[ds] = total.get(ds, 0) + n
        for ds, v in (R.get("cov") or {}).items():   # first hour each dataset was recorded (gaps are measured from there)
            if ds in cov_first: continue
            h = (0 if v else None) if not isinstance(v, list) else next((i for i, x in enumerate(v) if x), None)
            if h is not None: cov_first[ds] = f"{day}T{h:02d}:00:00Z"
    live = go_live_health(d, days, cov_first)
    bd = os.path.join(data, "_rollup", "bus")
    bus_days = sorted(f[:10] for f in os.listdir(bd) if f.endswith(".json")) if os.path.isdir(bd) else []
    cd = os.path.join(data, "_rollup", "cn")
    cn_days = sorted(f[:10] for f in os.listdir(cd) if f.endswith(".json")) if os.path.isdir(cd) else []
    return {"v": VERSION, "days": days, "first": days[0] if days else None, "last": days[-1] if days else None, "live": live,
            "firstSlot": first_slot, "covFirst": cov_first, "busDays": bus_days, "cnDays": cn_days, "bytes": total, "goLive": GO_LIVE, "incident": INCIDENT}


def go_live_health(d, days, cov_first):
    """Hours recorded since the new collector went live, per hourly dataset, and the current run of
    consecutive hours in which every started hourly dataset was recorded."""
    hourly = [ds for ds in ALL_DATASETS if ds not in DAILY_AT_UTC]
    grid = {}   # slot -> {ds: 0/1}
    for day in days:
        if day < GO_LIVE[:10]:
            continue
        try:
            with open(os.path.join(d, day + ".json"), encoding="utf-8") as fh:
                cov = json.load(fh).get("cov") or {}
        except (OSError, ValueError):
            continue
        for h in range(24):
            slot = f"{day}T{h:02d}:00:00Z"
            if slot >= GO_LIVE:
                grid[slot] = {ds: (cov.get(ds) or [0] * 24)[h] for ds in hourly}
    filled = [sl for sl, v in grid.items() if v.get("lineStatus")]
    if not filled:
        return None
    through = max(filled)
    by = {}
    for sl, v in grid.items():
        if sl > through: continue
        for ds in hourly:
            if ds in cov_first and sl >= max(GO_LIVE, cov_first[ds]):
                b = by.setdefault(ds, [0, 0]); b[1] += 1; b[0] += 1 if v.get(ds) else 0
    streak = 0
    for sl in sorted((x for x in grid if x <= through), reverse=True):
        need = [ds for ds in hourly if ds in cov_first and sl >= cov_first[ds]]
        if need and all(grid[sl].get(ds) for ds in need): streak += 1
        else: break
    return {"through": through, "present": sum(b[0] for b in by.values()), "expected": sum(b[1] for b in by.values()),
            "byDs": by, "streak": streak}


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--data", default="data")
    a = ap.parse_args(argv)
    built = update(a.data)
    print(f"rollup: {len(built)} day(s) built" + (f" ({built[0]} .. {built[-1]})" if built else ""))


if __name__ == "__main__":
    main()
