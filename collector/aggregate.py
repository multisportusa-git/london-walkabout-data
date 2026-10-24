#!/usr/bin/env python3
"""Aggregate data/*.ndjson into docs/summary.json + docs/latest.json (stdlib only, zero tokens).

Usage: python3 collector/aggregate.py --data data --out docs
Tolerates corrupt/empty lines, schema drift (early `readings`/`lineStatuses` variants), dedupes by slot (keeps last).
"""
import argparse, glob, json, os, re, datetime, collections, statistics
try:
    from zoneinfo import ZoneInfo
    LON = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover
    LON = datetime.timezone.utc
UTC = datetime.timezone.utc
DATASETS = ["liftDisruptions", "lineStatus", "stopDisruptions", "crowdingReadings", "arrivalsReadings"]
EXTRA_DATASETS = ["arrivalsDetail", "busLineStatus", "networkDisruptions", "crowdingNetwork", "nationalRailDepartures", "nationalRailIncidents", "nationalRailStations", "stationAccess", "arrivalsNetwork"]  # in the record and the app manifest, not summarised
VERSION = 1
INCIDENT = ("2026-09-22T15:00:00Z", "2026-09-25T15:00:00Z")
FMT = "%Y-%m-%dT%H:%M:%SZ"

def pts(s):
    s = s.strip()
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    d = datetime.datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)
def fts(d): return d.strftime(FMT)
def r1(x, n=1): return None if x is None else round(x, n)

def load(data_dir, ds):
    """slot -> record (last wins); returns (dict, total_lines, dup_slots, bad_lines)."""
    by, total, dup, bad = {}, 0, 0, 0
    files = sorted(glob.glob(os.path.join(data_dir, ds, "*.ndjson")))
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line: continue
                try:
                    r = json.loads(line); s = r["slot"]; pts(s)
                except Exception:
                    bad += 1; continue
                total += 1
                if s in by: dup += 1
                by[s] = r
    return by, total, dup, bad

# ---------- line status normalisation ----------
SEV = {"Special Service": 0, "Closed": 20, "Service Closed": 20, "Suspended": 2, "Part Suspended": 3, "Planned Closure": 4,
       "Part Closure": 5, "Severe Delays": 6, "Reduced Service": 7, "Bus Service": 8, "Minor Delays": 9, "Good Service": 10}
def cat_of(sev):
    # 0 good,1 minor,2 reduced/special,3 part/planned closure,4 severe/suspended,5 service closed
    if sev == 10: return 0
    if sev == 9: return 1
    if sev in (7, 8, 0, 1): return 2
    if sev in (3, 4, 5): return 3
    if sev in (6, 2): return 4
    if sev == 20: return 5
    return 2
CAT_LABEL = ["Good service", "Minor delays", "Reduced / special", "Part closure / planned", "Severe / suspended", "Service closed"]
def line_statuses(x):
    out = []
    if "statuses" in x: raw = x["statuses"]
    elif "lineStatuses" in x:
        raw = [{"description": s.get("statusSeverityDescription"), "severity": s.get("statusSeverity")} for s in x["lineStatuses"]]
    elif "statusSeverityDescription" in x: raw = [{"description": x["statusSeverityDescription"], "severity": x.get("statusSeverity")}]
    else: raw = []
    for s in raw or []:
        d, sv = s.get("description"), s.get("severity")
        if isinstance(sv, str): d, sv = sv, SEV.get(sv)           # long-text description, label in severity
        elif sv is None and d in SEV: sv = SEV[d]
        if d in SEV: sv = SEV[d] if sv is None else sv
        if sv is None: continue
        out.append((d if d in SEV else "Other", sv))
    return out
def line_mode(x): return x.get("mode") or x.get("modeName") or ""
def line_worst(sts):
    # worst category across statuses (a line can carry several); good only if all good
    if not sts: return None, None
    best = max(sts, key=lambda t: (cat_of(t[1]) if cat_of(t[1]) != 5 else 5, -t[1]))
    return cat_of(best[1]), best[0]

def title(s): return " ".join(w.capitalize() for w in re.split(r"[-\s]+", s))
def cover(by, first, last_global):
    slots = sorted(by);
    start = pts(first); end = pts(last_global)
    n_exp = int((end - start).total_seconds() // 3600) + 1
    have = set(slots); missing = []
    t = start
    while t <= end:
        if fts(t) not in have: missing.append(t)
        t += datetime.timedelta(hours=1)
    gaps = []
    for m in missing:
        if gaps and m - gaps[-1][1] == datetime.timedelta(hours=1): gaps[-1][1] = m
        else: gaps.append([m, m])
    design = sum(1 for m in missing if m.astimezone(LON).hour < 5)
    perday = collections.Counter(s[:10] for s in slots)
    return dict(first=fts(start), last=slots[-1], captured=len(slots), expected=n_exp,
                pct=r1(100 * len(slots) / max(1, n_exp)), missing=len(missing), missingClosureHours=design,
                pctExclClosureHours=r1(100 * len(slots) / max(1, n_exp - design)),
                gaps=[[fts(a), fts(b), int((b - a).total_seconds() // 3600) + 1] for a, b in gaps],
                perDay=dict(sorted(perday.items())))

def latest_record(data_dir, ds):
    """Last record of the newest day file of a dataset (plain or gzip); None if absent or unreadable."""
    import gzip
    fs = sorted(glob.glob(os.path.join(data_dir, ds, "*.ndjson*")))
    if not fs: return None
    try:
        fh = gzip.open(fs[-1], "rt", encoding="utf-8") if fs[-1].endswith(".gz") else open(fs[-1], encoding="utf-8")
        last = None
        for line in fh:
            if line.strip(): last = line
        fh.close()
        return json.loads(last) if last else None
    except Exception:
        return None

def now_and_observations(D, S, last_global, cutoff7, data_dir="data"):
    """Current line board and a short list of computed observations for the dashboard."""
    now_board, obs = {}, []
    LS, LF, SD = D["lineStatus"], D["liftDisruptions"], D["stopDisruptions"]
    if LS:
        s = max(LS); tiles = []
        for x in LS[s].get("lines", []) or []:
            cat, lab = line_worst(line_statuses(x))
            reasons = [st.get("reason") for st in (x.get("statuses") or []) if isinstance(st, dict) and st.get("reason")]
            tiles.append(dict(id=x.get("id"), name=x.get("name"), mode=line_mode(x), cat=cat, status=lab, reason=(reasons[0][:240] if reasons else None)))
        tiles.sort(key=lambda t: (t["mode"] == "national-rail", -(t["cat"] if t["cat"] is not None else -1), t["name"] or ""))
        def obs_group(key, group, label):
            if not group: return
            bad = [t for t in group if t["cat"] not in (0, None)]
            obs.append(dict(key=key, tone="good" if not bad else ("bad" if any(t["cat"] in (3, 4) for t in bad) else "warn"),
                            value=f"{len(group) - len(bad)}/{len(group)}", label=label,
                            text=("All reported good service at the latest reading." if not bad else
                                  "Not good: " + "; ".join(f"{t['name']} ({t['status']})" for t in bad[:6]) + (f" and {len(bad) - 6} more." if len(bad) > 6 else "."))))
        obs_group("network", [t for t in tiles if t["mode"] != "national-rail"], "TfL lines with good service now")
        obs_group("nationalRail", [t for t in tiles if t["mode"] == "national-rail"], "National Rail operators with good service now")
        now_board = dict(slot=s, polledAt=LS[s].get("polledAt"), lines=tiles)
    bus = latest_record(data_dir, "busLineStatus")
    if bus and bus.get("totalLines"):
        n, tot = len(bus.get("disrupted") or []), bus["totalLines"]
        obs.append(dict(key="bus", tone="warn" if n > tot * 0.2 else "neutral", value=f"{tot - n}/{tot}", label="bus routes with good service now",
                        text=f"{n} routes have diversions, special service or delays (latest reading {str(bus.get('polledAt'))[:16]} UTC)."))
    nr = latest_record(data_dir, "nationalRailDepartures")
    if nr and isinstance(nr.get("stations"), dict) and nr.get("fields"):
        F = nr["fields"]; ix = {f: F.index(f) for f in ("std", "etd", "isCancelled") if f in F}
        tot = canc = late = 0
        for b in nr["stations"].values():
            for r in (b or {}).get("services") or []:
                tot += 1
                if ix and r[ix["isCancelled"]]: canc += 1; continue
                try:
                    d = (datetime.datetime.fromisoformat(r[ix["etd"]]) - datetime.datetime.fromisoformat(r[ix["std"]])).total_seconds()
                    late += d >= 300
                except Exception:
                    pass
        if tot:
            ok = tot - canc - late
            obs.append(dict(key="nationalRailBoards", tone="good" if canc + late == 0 else ("bad" if (canc + late) > tot * 0.15 else "warn"),
                            value=f"{ok}/{tot}", label="National Rail departures on time at watched stations",
                            text=f"{canc} cancelled and {late} expected 5+ min late across {len(nr['stations'])} stations (board at {str(nr.get('boardTimeLocal'))[11:16]} UK)."))
    ki = latest_record(data_dir, "nationalRailIncidents")
    if ki and isinstance(ki.get("count"), int):
        bo = ki.get("byOperator") or {}
        unplanned = ki["count"] - (ki.get("planned") or 0)
        obs.append(dict(key="nationalRailIncidents", tone="warn" if unplanned > 0 else "neutral", value=str(ki["count"]),
                        label="National Rail incidents and engineering works listed",
                        text=f"{ki.get('planned') or 0} planned, {unplanned} unplanned; Southeastern {bo.get('SE', 0)}, Thameslink {bo.get('TL', 0)} (feed read {str(ki.get('polledAt'))[:16]} UTC)."))
    L = (S.get("lines") or {}).get("list") or []
    l7 =[l for l in L if l.get("good7dPct") is not None and l.get("slots", 0) >= 24]  # need a day of readings to rank
    if l7:
        w = min(l7, key=lambda l: l["good7dPct"]); b = max(l7, key=lambda l: l["good7dPct"])
        obs.append(dict(key="worstLine", tone="bad" if w["good7dPct"] < 70 else "warn", value=f"{w['good7dPct']}%", label=f"{w['name']}: good service, last 7 days",
                        text=f"{w['name']} had the lowest share of good-service hours this week; {b['name']} the highest at {b['good7dPct']}%."))
    F = S.get("lifts") or {}
    if F.get("current"):
        slots7 = [s for s in LF if s >= cutoff7]
        avg7 = sum(len({e.get("stationUniqueId") for e in (LF[s].get("disruptions") or [])}) for s in slots7) / len(slots7) if slots7 else None
        cur_st = F["current"]["stations"]
        trend = "" if avg7 is None else (f" That is above the 7-day average of {avg7:.1f}." if cur_st > avg7 + 0.5 else
                                         f" That is below the 7-day average of {avg7:.1f}." if cur_st < avg7 - 0.5 else f" In line with the 7-day average of {avg7:.1f}.")
        obs.append(dict(key="lifts", tone="warn" if avg7 is not None and cur_st > avg7 + 0.5 else "neutral", value=str(F["current"]["lifts"]),
                        label="lifts out of service now", text=f"{F['current']['lifts']} lifts out at {cur_st} stations.{trend}"))
        B = F.get("board") or {}
        if B.get("counts"):
            reds = [x["name"] for x in B["stations"] if x["colour"] == "red"]
            obs.append(dict(key="liftStoplight", tone="bad" if reds else ("warn" if B["counts"]["amber"] else "good"),
                            value=f"{B['counts']['red']} red", label="stations with no lift working",
                            text=(("No lift working at " + ", ".join(reds[:6]) + ". ") if reds else "") +
                                 f"{B['counts']['amber']} more stations have some lifts out but others working."))
        ongoing = [x for x in F.get("longest") or [] if x.get("ongoing")]
        if ongoing:
            x = ongoing[0]; days = x["hours"] / 24
            obs.append(dict(key="longestLift", tone="bad" if days >= 7 else "warn", value=(f"{days:.1f} d" if days >= 2 else f"{x['hours']} h"),
                            label="longest lift outage still open", text=f"Lift {x['lift']} at {x['station']} has been out since {x['start'][:10]}."))
    T = S.get("stops") or {}
    if T.get("current"):
        c, avg = T["current"]["disruptions"], T.get("avgActive") or 0
        obs.append(dict(key="notices", tone="warn" if c > avg * 1.25 else "neutral", value=str(c), label="station notices now",
                        text=f"{c} notices across {T['current']['stations']} watched stations and piers (average {avg})."))
    C = S.get("crowding") or {}
    if C.get("top7d"):
        k = C["top7d"][0]; st = C["stations"][k]
        ph = st.get("peakHour")
        obs.append(dict(key="busiest", tone="neutral", value=f"{st['mean7d']}%", label=f"{st['name']}: busiest of the tracked stations",
                        text=f"Highest average crowding this week relative to its normal level" + (f"; it peaks around {ph:02d}:00 UK time." if ph is not None else ".")))
    cov24 = {}
    end = pts(last_global)
    for ds in DATASETS:
        have = set(D[ds]); cov24[ds] = sum(1 for i in range(24) if fts(end - datetime.timedelta(hours=i)) in have)
    worst = min(cov24.values()) if cov24 else 0
    obs.append(dict(key="health", tone="good" if worst == 24 else ("warn" if worst >= 20 else "bad"), value=f"{worst}/24",
                    label="hours recorded, last 24 h (worst dataset)",
                    text="All five datasets recorded every hour in the last day." if worst == 24 else
                    "Missing hours in the last day: " + ", ".join(f"{ds} {24 - n}" for ds, n in cov24.items() if n < 24) + "."))
    return now_board, obs

NO_SFA = re.compile(r"no step[- ]free access|step[- ]free access is not available|not step[- ]free", re.I)


def lift_inventory(data_dir):
    """{station id: {"name":..., "lifts": {lift id: (endpoint set)}}} from the newest stored TfL station data ZIP."""
    import zipfile, io, csv
    rec = latest_record(data_dir, "stationAccess")
    if not rec or not rec.get("file"): return None, None
    path = os.path.join(data_dir, rec["file"])
    try:
        z = zipfile.ZipFile(path)
        inv = collections.defaultdict(lambda: {"name": None, "lifts": {}})
        for r in csv.DictReader(io.TextIOWrapper(z.open("Lifts.csv"), encoding="utf-8-sig")):
            ends = set(x for x in ((r.get("FromAreas") or "") + "|" + (r.get("ToAreas") or "")).split("|") if x)
            inv[r["StationUniqueId"]]["lifts"][r["LiftUniqueId"]] = ends
        for r in csv.DictReader(io.TextIOWrapper(z.open("Stations.csv"), encoding="utf-8-sig")):
            inv[r["UniqueId"]]["name"] = r.get("Name")   # stations with no lifts appear with 0 lifts
        return dict(inv), rec.get("slot")
    except Exception:
        return None, None


def lift_board(data_dir, cur_record, runs, slot_now, names, watched):
    """Stoplight per station, judged against how many lifts the station has (TfL station data):
    red = no lift working; amber = some lifts out, at least one still working; green = all working.
    TfL's own verdict ("no step-free access ...") is kept as a flag with its wording."""
    inv, inv_slot = lift_inventory(data_dir)
    agg = collections.OrderedDict()
    for e in (cur_record or {}).get("disruptions", []) or []:          # TfL may list one station several times
        st = e.get("stationUniqueId")
        if not st: continue
        g = agg.setdefault(st, {"down": set(), "msgs": []})
        g["down"] |= set(e.get("disruptedLiftUniqueIds") or [])
        m = (e.get("message") or "").strip()
        if m and m not in g["msgs"]: g["msgs"].append(m)
    out, names_done = [], set()
    for st, g in agg.items():
        down = g["down"]; known = (inv or {}).get(st, {}).get("lifts", {})
        total = len(set(known) | down) if inv is not None else None
        working = (total - len(down)) if total is not None else None
        msg = " | ".join(g["msgs"])
        lost = bool(NO_SFA.search(msg))
        detail = []
        for m in g["msgs"]:
            parts = re.split(r"\s*-\s+|:\s+", m, maxsplit=2)
            t = parts[-1] if len(parts) > 1 else m
            detail.append(re.sub(r"\s*Call (us on )?0343 222 1234.*$", "", t).strip())
        colour = "red" if working == 0 else "amber"
        since = min((runs[l]["start"] for l in down if l in runs and runs[l]["last"] == slot_now), default=None)
        nm = ((inv or {}).get(st, {}).get("name")) or names(st)
        names_done.add(nm)
        out.append(dict(id=st, name=nm, colour=colour, working=working, total=total, down=len(down),
                        stepFreeLost=lost, since=since, detail=" / ".join(d for d in detail if d)[:240], message=msg[:400]))
    for st in watched:
        if inv is None or st not in inv: continue
        n = len(inv[st]["lifts"]); nm = inv[st]["name"] or names(st)
        if n and nm not in names_done:
            names_done.add(nm)
            out.append(dict(id=st, name=nm, colour="green", working=n, total=n, down=0, stepFreeLost=False, since=None, detail="", message=""))
    order = {"red": 0, "amber": 1, "green": 2}
    out.sort(key=lambda x: (order[x["colour"]], -(x["down"] / x["total"]) if x["total"] else 0, x["name"] or ""))
    return dict(slot=slot_now, inventoryFrom=inv_slot, stations=out, trackedSince="2026-09-30T09:00:00Z",
                counts={c: sum(1 for x in out if x["colour"] == c) for c in ("red", "amber", "green")})


def crowding_network_detail(data_dir, names):
    """All tube/Elizabeth stations from crowdingNetwork: latest %, mean by hour of day (UK), last 48 h."""
    import gzip
    files = sorted(glob.glob(os.path.join(data_dir, "crowdingNetwork", "*.ndjson*")))
    if not files: return None
    acc, last_slot = {}, None
    for f in files:
        try:
            fh = gzip.open(f, "rt", encoding="utf-8") if f.endswith(".gz") else open(f, encoding="utf-8")
            for line in fh:
                if not line.strip(): continue
                try: r = json.loads(line)
                except ValueError: continue
                slot = r.get("slot"); t = pts(slot).astimezone(LON)
                last_slot = max(last_slot or slot, slot)
                for sid, v in (r.get("stations") or {}).items():
                    if not isinstance(v, dict) or not v.get("dataAvailable"): continue
                    p_ = v.get("percentageOfBaseline")
                    if not isinstance(p_, (int, float)): continue
                    a_ = acc.setdefault(sid, {"h": [[0, 0] for _ in range(24)], "series": []})
                    a_["h"][t.hour][0] += p_; a_["h"][t.hour][1] += 1; a_["series"].append([slot, r1(100 * p_)])
            fh.close()
        except (OSError, EOFError):
            continue
    if not acc: return None
    cut = fts(pts(last_slot) - datetime.timedelta(hours=48))
    out = {}
    for sid, a_ in acc.items():
        ser = [x for x in a_["series"] if x[0] > cut]
        out[sid] = dict(name=names(sid), hours=[r1(100 * c[0] / c[1]) if c[1] else None for c in a_["h"]],
                        recent=ser, now=(a_["series"][-1] if a_["series"][-1][0] == last_slot else None))
    return dict(slot=last_slot, unit="percent of TfL baseline (100 = typical)", stations=out)


def arrivals_network_now(data_dir):
    rec = latest_record(data_dir, "arrivalsNetwork")
    if not rec or not isinstance(rec.get("modes"), dict): return None
    F = rec.get("fields") or []; vi = F.index("vehicleId") if "vehicleId" in F else None
    modes = {}
    for m, rows in rec["modes"].items():
        if rows is None: continue
        veh = {r[vi] for r in rows if vi is not None and r[vi]}
        modes[m] = dict(predictions=len(rows), vehicles=len(veh) or None, stops=len({r[0] for r in rows}))
    return dict(slot=rec.get("slot"), modes=modes)


def glance(S, obs, cn, an):
    """Headline numbers for the infographic row at the top of the dashboard."""
    g = {}
    N = S.get("now") or {}
    tfl = [l for l in (N.get("lines") or []) if l.get("mode") != "national-rail"]
    if tfl:
        good = sum(1 for l in tfl if l.get("cat") == 0)
        g["lines"] = dict(good=good, total=len(tfl), notGood=[l["name"] for l in tfl if l.get("cat") not in (0, None)][:8])
    B = (S.get("lifts") or {}).get("board") or {}
    if B.get("counts"): g["lifts"] = B["counts"]
    for o in obs:
        if o.get("key") in ("nationalRailBoards", "bus") and "/" in str(o.get("value")):
            a_, b_ = str(o["value"]).split("/", 1)
            try: g["nr" if o["key"] == "nationalRailBoards" else "bus"] = dict(ok=int(a_), total=int(b_), text=o.get("text"))
            except ValueError: pass
    if cn:
        nowv = sorted(((v["now"][1], v["name"], sid) for sid, v in cn["stations"].items() if v.get("now")), reverse=True)
        g["crowdNow"] = dict(slot=cn["slot"], top=[dict(id=sid, name=n, pct=p_) for p_, n, sid in nowv[:6]], stations=len(nowv))
    if an: g["trainsNow"] = an
    return g


def write_api(out_dir, data_dir, D, S, now):
    """Stable, versioned JSON feed for apps (e.g. London Commuter v3). Additive changes only within v1."""
    api = os.path.join(out_dir, "api", "v1"); os.makedirs(api, exist_ok=True)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner, name = (repo.split("/", 1) + [""])[:2] if repo else ("", "")
    raw = f"https://raw.githubusercontent.com/{repo}/main/data/" if repo else None
    pages = f"https://{owner.lower()}.github.io/{name}/" if repo else None
    latest = {"schemaVersion": 1, "generatedAt": fts(now), "datasets": {}}
    for ds in DATASETS:
        if D[ds]:
            s = max(D[ds]); latest["datasets"][ds] = D[ds][s]
    man = {"schemaVersion": 1, "generatedAt": fts(now), "pagesBase": pages, "rawDataBase": raw,
           "dayFileTemplate": "{rawDataBase}{dataset}/{YYYY-MM-DD}{fileSuffix}  (fileSuffix '.ndjson' unless the dataset says '.ndjson.gz')",
           "recordFormat": "one JSON object per line; 'slot' = UTC hour it represents, 'polledAt' = real poll time; duplicate slots possible, keep the last",
           "datasets": {}}
    for ds in DATASETS:
        days = sorted(os.path.basename(f)[:10] for f in glob.glob(os.path.join(data_dir, ds, "*.ndjson")))
        if D[ds]:
            man["datasets"][ds] = {"firstSlot": min(D[ds]), "lastSlot": max(D[ds]), "slots": len(D[ds]), "days": days}
    # datasets not summarised on the dashboard: list their day files only (cheap, no full parse)
    for ds in EXTRA_DATASETS:
        fs = sorted(glob.glob(os.path.join(data_dir, ds, "*.ndjson*")))
        if fs:
            names = [os.path.basename(f) for f in fs]
            man["datasets"][ds] = {"firstDay": names[0][:10], "lastDay": names[-1][:10], "files": names}
    reg = None
    for p in (os.path.join(os.path.dirname(os.path.abspath(data_dir)), "config", "registry.json"), os.path.join("config", "registry.json")):
        if os.path.exists(p):
            reg = json.load(open(p, encoding="utf-8")); break
    files = [("latest.json", latest), ("manifest.json", man), ("summary.json", S)]
    if reg is not None:
        files.append(("registry.json", reg))
    for fn, obj in files:
        with open(os.path.join(api, fn), "w", encoding="utf-8") as fh: json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--data", default="data"); ap.add_argument("--out", default="docs")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    D, meta_rec = {}, {}
    for ds in DATASETS:
        by, total, dup, bad = load(a.data, ds)
        D[ds] = by; meta_rec[ds] = dict(records=total, uniqueSlots=len(by), duplicateSlots=dup, badLines=bad)
    allslots = [s for ds in D for s in D[ds]]
    if not allslots:
        json.dump({"meta": {"version": VERSION, "generatedAt": fts(datetime.datetime.now(UTC)), "empty": True}}, open(os.path.join(a.out, "summary.json"), "w"))
        return
    last_global = max(allslots)
    now = datetime.datetime.now(UTC)
    S = {"meta": dict(version=VERSION, generatedAt=fts(now), dataThrough=last_global, records=meta_rec)}
    S["incident"] = dict(from_=INCIDENT[0], to=INCIDENT[1])
    S["incident"] = {"from": INCIDENT[0], "to": INCIDENT[1]}
    # ---- coverage ----
    S["coverage"] = {ds: cover(D[ds], min(D[ds]), last_global) for ds in DATASETS if D[ds]}
    for ds, c in S["coverage"].items():
        c["gaps"] = sorted(c["gaps"], key=lambda g: -g[2])[:40]
    cutoff7 = fts(pts(last_global) - datetime.timedelta(days=7))

    # ---- lines ----
    LS = D["lineStatus"]; per = {}; names = {}; modes = {}
    matrix_slots = []
    if LS:
        end = pts(last_global); start168 = end - datetime.timedelta(hours=167)
        matrix_slots = [fts(start168 + datetime.timedelta(hours=i)) for i in range(168)]
    ms_index = {s: i for i, s in enumerate(matrix_slots)}
    rows = collections.defaultdict(lambda: ["-"] * 168)
    for slot in sorted(LS):
        seen = {}
        for x in LS[slot].get("lines", []) or []:
            lid = x.get("id")
            if not lid: continue
            names[lid] = x.get("name") or names.get(lid) or lid; modes[lid] = line_mode(x) or modes.get(lid, "")
            sts = line_statuses(x); cat, lab = line_worst(sts)
            if cat is None: continue
            seen[lid] = (cat, lab, sts)
        for lid, (cat, lab, sts) in seen.items():
            p = per.setdefault(lid, dict(slots=0, good=0, closed=0, good7=0, n7=0, counts=collections.Counter(), day=collections.defaultdict(lambda: [0, 0])))
            p["slots"] += 1
            for d, _ in sts: p["counts"][d] += 1
            if cat == 5: p["closed"] += 1
            else:
                p["day"][slot[:10]][1] += 1
                if cat == 0: p["good"] += 1; p["day"][slot[:10]][0] += 1
                if slot >= cutoff7:
                    p["n7"] += 1; p["good7"] += 1 if cat == 0 else 0
            if slot in ms_index: rows[lid][ms_index[slot]] = str(cat)
    lines = []
    for lid, p in per.items():
        op = p["slots"] - p["closed"]
        worst = None
        for d, (g, n) in p["day"].items():
            if n >= 6:
                v = 100 * g / n
                if worst is None or v < worst[1]: worst = (d, v)
        lines.append(dict(id=lid, name=names[lid], mode=modes[lid], slots=p["slots"], goodPct=r1(100 * p["good"] / op) if op else None,
                          good7dPct=r1(100 * p["good7"] / p["n7"]) if p["n7"] else None, closedPct=r1(100 * p["closed"] / p["slots"]),
                          worstDay=dict(date=worst[0], goodPct=r1(worst[1])) if worst else None, counts=dict(p["counts"].most_common())))
    lines.sort(key=lambda l: (l["goodPct"] if l["goodPct"] is not None else 101))
    S["lines"] = dict(list=lines, matrix=dict(start=matrix_slots[0] if matrix_slots else None, hours=168, rows={k: "".join(v) for k, v in sorted(rows.items())}),
                      legend=CAT_LABEL)

    # ---- station names ----
    stname = {}
    for slot, r in D["stopDisruptions"].items():
        for e in r.get("disruptions", []) or []:
            if e.get("atcoCode") and e.get("commonName"): stname[e["atcoCode"]] = e["commonName"]
    for f in glob.glob(os.path.join(a.data, "_legacy", "watchedStations", "*.json")):
        try:
            w = json.load(open(f, encoding="utf-8")); nm_ = title(w.get("slug") or os.path.basename(f)[:-5])
            for k in ("crowdingId", "disruptionId"):
                if w.get(k): stname.setdefault(w[k], nm_)
        except Exception: pass
    # ---- lifts ----
    LF = D["liftDisruptions"]; lifts = {}
    if LF:
        slots = sorted(LF)
        st_hours = collections.Counter(); runs = {}; done = []
        perday = collections.defaultdict(list); msgname = {}
        prev_t = None; prev_set = set()
        for slot in slots:
            t = pts(slot); cur = {}; sts_now = set()
            for e in LF[slot].get("disruptions", []) or []:
                st = e.get("stationUniqueId"); msg = e.get("message") or ""
                if st: sts_now.add(st)
                m = re.match(r"\s*([^:\n]{2,40}?)(?: STATION| Station| UNDERGROUND STATION)?\s*:", msg)
                if st and m and st not in msgname: msgname[st] = m.group(1).title() if m.group(1).isupper() else m.group(1)
                for lid in e.get("disruptedLiftUniqueIds", []) or []:
                    cur[lid] = st
            for st in sts_now: st_hours[st] += 1
            for lid, st in cur.items():
                if lid in runs and prev_t and (t - prev_t) <= datetime.timedelta(hours=12) and runs[lid]["last"] == fts(prev_t):
                    runs[lid]["last"] = slot; runs[lid]["n"] = int((t - pts(runs[lid]["start"])).total_seconds() // 3600) + 1
                else:
                    if lid in runs: done.append((lid, runs[lid]))
                    runs[lid] = dict(start=slot, last=slot, n=1, st=st)
            perday[slot[:10]].append((len(sts_now), len(cur))); prev_t = t
        cur_last = LF[slots[-1]]; cur_ids = {}
        for e in cur_last.get("disruptions", []) or []:
            for lid in e.get("disruptedLiftUniqueIds", []) or []: cur_ids[lid] = e.get("stationUniqueId")
        allruns = done + list(runs.items())
        longest = sorted(allruns, key=lambda x: -x[1]["n"])[:10]
        nm = lambda st: stname.get(st) or msgname.get(st) or st
        lifts = dict(current=dict(slot=slots[-1], lifts=len(cur_ids), stations=len(set(cur_ids.values()))),
                     perDay=[[d, r1(sum(x[0] for x in v) / len(v)), r1(sum(x[1] for x in v) / len(v)) if any(x[1] for x in v) else None] for d, v in sorted(perday.items())],
                     note="Readings before 30 Sep name only the station; per-lift tracking starts 30 Sep 2026.",
                     topStations=[dict(id=s, name=nm(s), liftHours=n) for s, n in st_hours.most_common(15)],
                     longest=[dict(lift=l, station=nm(x["st"]), start=x["start"], last=x["last"], hours=x["n"], ongoing=(x["last"] == slots[-1])) for l, x in longest])
        watched = []
        for f in glob.glob(os.path.join(a.data, "_legacy", "watchedStations", "*.json")):
            try:
                w = json.load(open(f, encoding="utf-8"))
                watched += [w[k] for k in ("disruptionId", "crowdingId") if w.get(k)]
            except Exception: pass
        try:
            lifts["board"] = lift_board(a.data, cur_last, runs, slots[-1], nm, watched)
        except Exception as ex:
            lifts["board"] = dict(error=type(ex).__name__)
    S["lifts"] = lifts

    # ---- stop disruptions ----
    SD = D["stopDisruptions"]; stops = {}
    if SD:
        slots = sorted(SD); sst = collections.Counter(); app = collections.Counter(); typ = collections.Counter(); mod = collections.Counter(); act = []
        stmode = {}
        for slot in slots:
            ents = SD[slot].get("disruptions", []) or []
            act.append(len(ents)); seen = set()
            for e in ents:
                k = e.get("atcoCode") or e.get("stationAtcoCode") or e.get("commonName")
                app[e.get("appearance") or "?"] += 1; typ[e.get("type") or "?"] += 1; mod[e.get("mode") or "?"] += 1
                if k: seen.add(k); stname.setdefault(k, e.get("commonName") or k); stmode[k] = e.get("mode") or stmode.get(k, "")
            for k in seen: sst[k] += 1
        lastents = SD[slots[-1]].get("disruptions", []) or []
        stops = dict(current=dict(slot=slots[-1], disruptions=len(lastents), stations=len({e.get("atcoCode") for e in lastents})),
                     avgActive=r1(sum(act) / len(act)), snapshots=len(slots),
                     topStations=[dict(id=k, name=stname.get(k, k), slots=n, mode=stmode.get(k, "")) for k, n in sst.most_common(15)],
                     byAppearance=dict(app.most_common()), byType=dict(typ.most_common(8)), byMode=dict(mod.most_common()),
                     activeSeries=[[s[:13], n] for s, n in zip(slots[-336:], act[-336:])])
    S["stops"] = stops

    # ---- crowding ----
    CR = D["crowdingReadings"]; crow = {}
    def station_map(r):
        return r.get("stations") if isinstance(r.get("stations"), dict) else (r.get("readings") if isinstance(r.get("readings"), dict) else {})
    if CR:
        acc = {}
        for slot, r in CR.items():
            t = pts(r.get("polledAt") or slot).astimezone(LON); wk = 1 if t.weekday() >= 5 else 0
            for sl, v in station_map(r).items():
                if not isinstance(v, dict) or v.get("dataAvailable") is False: continue
                p = v.get("percentageOfBaseline")
                if p is None or not isinstance(p, (int, float)): continue
                a_ = acc.setdefault(sl, dict(h=[[[0, 0] for _ in range(24)] for _ in range(2)], n=0, s7=0, n7=0))
                c = a_["h"][wk][t.hour]; c[0] += p; c[1] += 1; a_["n"] += 1
                if slot >= cutoff7: a_["s7"] += p; a_["n7"] += 1
        st = {}
        for sl, a_ in acc.items():
            def mean(w): return [r1(100 * c[0] / c[1], 1) if c[1] else None for c in a_["h"][w]]
            wd, we = mean(0), mean(1)
            allh = [(sum(x for x in (wd[h], we[h]) if x is not None) / max(1, sum(1 for x in (wd[h], we[h]) if x is not None)), h) for h in range(24) if (wd[h] is not None or we[h] is not None)]
            peak = max(allh)[1] if allh else None
            st[sl] = dict(name=title(sl), wd=wd, we=we, peakHour=peak, mean7d=r1(100 * a_["s7"] / a_["n7"]) if a_["n7"] else None, n=a_["n"],
                          recent=[], dow=[None] * 7, now=None)
        # drill-down: last 7 days hour by hour, mean by day of week, latest reading
        dsum = {}
        for slot in sorted(CR):
            r = CR[slot]; t = pts(r.get("polledAt") or slot).astimezone(LON)
            for sl, v in station_map(r).items():
                if sl not in st or not isinstance(v, dict) or v.get("dataAvailable") is False: continue
                p_ = v.get("percentageOfBaseline")
                if not isinstance(p_, (int, float)): continue
                d_ = dsum.setdefault(sl, [[0, 0] for _ in range(7)]); d_[t.weekday()][0] += p_; d_[t.weekday()][1] += 1
                if slot >= cutoff7: st[sl]["recent"].append([slot, r1(100 * p_)])
                st[sl]["now"] = [slot, r1(100 * p_)]
        for sl, d_ in dsum.items():
            st[sl]["dow"] = [r1(100 * a / n) if n else None for a, n in d_]
        latest_cr = max(CR)
        for sl in st:   # "now" only if this station was in the latest reading; otherwise keep it as "last"
            st[sl]["last"] = st[sl]["now"]
            if st[sl]["now"] and st[sl]["now"][0] != latest_cr: st[sl]["now"] = None
        top = sorted([k for k, v in st.items() if v["mean7d"] is not None], key=lambda k: -st[k]["mean7d"])[:10]
        crow = dict(unit="percent of TfL baseline (100 = typical)", stations=st, top7d=top)
    S["crowding"] = crow

    # ---- arrivals ----
    AR = D["arrivalsReadings"]; arr = {}
    if AR:
        acc = {}
        for slot, r in AR.items():
            for sl, v in station_map(r).items():
                if not isinstance(v, dict): continue
                a_ = acc.setdefault(sl, dict(pred=[0, 0], line=collections.defaultdict(lambda: [0, 0]), p7=[0, 0], l7=collections.defaultdict(lambda: [0, 0])))
                tp = v.get("totalPredictions")
                if isinstance(tp, (int, float)):
                    a_["pred"][0] += tp; a_["pred"][1] += 1
                    if slot >= cutoff7: a_["p7"][0] += tp; a_["p7"][1] += 1
                nb = v.get("nextByLine")
                pairs = []
                if isinstance(nb, dict): pairs = [(k, x.get("minSeconds") if isinstance(x, dict) else x) for k, x in nb.items()]
                elif isinstance(v.get("nextByLineSeconds"), dict): pairs = list(v["nextByLineSeconds"].items())
                for ln, sec in pairs:
                    if isinstance(sec, (int, float)):
                        a_["line"][ln][0] += sec; a_["line"][ln][1] += 1
                        if slot >= cutoff7: a_["l7"][ln][0] += sec; a_["l7"][ln][1] += 1
        st = {}
        for sl, a_ in acc.items():
            use_l = a_["l7"] if a_["l7"] else a_["line"]; use_p = a_["p7"] if a_["p7"][1] else a_["pred"]
            per_line = {ln: int(round(c[0] / c[1])) for ln, c in use_l.items() if c[1]}
            vals = list(per_line.values())
            st[sl] = dict(name=title(sl), perLine=dict(sorted(per_line.items(), key=lambda kv: kv[1])), meanNextSec=int(round(sum(vals) / len(vals))) if vals else None,
                          meanPredictions=r1(use_p[0] / use_p[1]) if use_p[1] else None, n=a_["pred"][1])
        have = [k for k, v in st.items() if v["meanPredictions"] is not None]
        busiest = sorted(have, key=lambda k: -st[k]["meanPredictions"])[:10]
        slowest = sorted([k for k in st if st[k]["meanNextSec"] is not None and st[k]["n"] >= 20], key=lambda k: -st[k]["meanNextSec"])[:10]
        arr = dict(window="last 7 days where available", stations=st, busiest=busiest, slowest=slowest)
    S["arrivals"] = arr

    # ---- health ----
    runs = []
    for f in sorted(glob.glob(os.path.join(a.data, "_health", "*.ndjson")))[-3:]:
        for line in open(f, encoding="utf-8", errors="replace"):
            try: runs.append(json.loads(line))
            except Exception: pass
    S["health"] = dict(runs=runs[-72:])

    # ---- now board + observations (plain computed facts, zero tokens) ----
    S["now"], S["observations"] = now_and_observations(D, S, last_global, cutoff7, a.data)
    # drill-down data for every tube/Elizabeth station goes to its own file, loaded only when asked for
    try:
        inv, _ = lift_inventory(a.data)
        try:
            nmap = json.load(open(os.path.join(os.path.dirname(os.path.abspath(a.data)), "config", "stationNames.json"), encoding="utf-8"))["names"]
        except Exception:
            nmap = {}
        def stn(sid):
            base = nmap.get(sid) or ((inv or {}).get(sid, {}).get("name")) or stname.get(sid) or sid
            return base + (" (Elizabeth line)" if sid.startswith("910G") else "")
        cn = crowding_network_detail(a.data, stn)
    except Exception:
        cn = None
    if cn:
        with open(os.path.join(a.out, "crowding.json"), "w", encoding="utf-8") as fh: json.dump(cn, fh, separators=(",", ":"), ensure_ascii=False)
    try: an = arrivals_network_now(a.data)
    except Exception: an = None
    S["glance"] = glance(S, S["observations"], cn, an)

    out = os.path.join(a.out, "summary.json")
    with open(out, "w", encoding="utf-8") as fh: json.dump(S, fh, separators=(",", ":"), ensure_ascii=False)
    write_api(a.out, a.data, D, S, now)
    # same payload as a script so docs/index.html also works when opened from disk (file:// blocks fetch)
    with open(os.path.join(a.out, "summary.js"), "w", encoding="utf-8") as fh:
        fh.write("window.__SUMMARY__=" + json.dumps(S, separators=(",", ":"), ensure_ascii=False) + ";\n")

    # ---- latest.json ----
    L = {"generatedAt": fts(now)}
    for ds in DATASETS:
        if D[ds]:
            s = max(D[ds]); L[ds] = {"slot": s, "polledAt": D[ds][s].get("polledAt")}
    if LF:
        s = max(LF); L["liftDisruptions"]["items"] = [dict(station=(stname.get(e.get("stationUniqueId")) or e.get("stationUniqueId")), lifts=e.get("disruptedLiftUniqueIds"), message=(e.get("message") or "")[:200]) for e in LF[s].get("disruptions", [])][:40]
    if LS:
        s = max(LS); L["lineStatus"]["lines"] = [dict(id=x.get("id"), name=x.get("name"), status=(line_worst(line_statuses(x))[1])) for x in LS[s].get("lines", [])]
    if SD:
        s = max(SD); L["stopDisruptions"]["count"] = len(SD[s].get("disruptions", []) or [])
    if CR:
        s = max(CR); L["crowdingReadings"]["stations"] = {k: r1(100 * v["percentageOfBaseline"]) for k, v in station_map(CR[s]).items() if isinstance(v, dict) and isinstance(v.get("percentageOfBaseline"), (int, float))}
    with open(os.path.join(a.out, "latest.json"), "w", encoding="utf-8") as fh: json.dump(L, fh, separators=(",", ":"), ensure_ascii=False)
    print(f"wrote {out} ({os.path.getsize(out)} bytes), latest.json; dataThrough={last_global}")

if __name__ == "__main__": main()
