#!/usr/bin/env python3
"""Convert an ArtifactData export (export/<collection>/<doc_id>.json) into the repo's NDJSON layout.

Usage: python3 tools/backfill_from_export.py --export EXPORT_DIR --out data [--validate-only]
Layout: data/<dataset>/<YYYY-MM-DD>.ndjson  (one record per line, sorted by polledAt, UTC date of slot)
Record: {"slot","polledAt","legacyDocId", ...all original body fields unchanged}
Legacy/registry collections are copied verbatim to data/_legacy/<collection>/<id>.json.

SAFETY: this tool rebuilds data/<dataset>/ from scratch, so it REFUSES to run when the output
already holds any record written by the live collector (a record without legacyDocId). It is
only for building a fresh repo; never run it on a repo that is already collecting.
"""
import argparse, gzip, json, os, shutil, sys, datetime, collections
DATASETS = ["liftDisruptions", "lineStatus", "stopDisruptions", "crowdingReadings", "arrivalsReadings", "busLineStatus"]
GZIP_DATASETS = {"busLineStatus"}  # stored as .ndjson.gz by the live collector
LEGACY = ["interchanges", "watchedStations", "watchedPiers", "lineOperatingHours"]

def parse_ts(s):
    s = s.strip()
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    d = datetime.datetime.fromisoformat(s)
    if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)

def load_export(export):
    out = {}
    for ds in DATASETS:
        recs = []
        d = os.path.join(export, ds)
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not fn.endswith(".json"): continue
            doc = json.load(open(os.path.join(d, fn), encoding="utf-8"))
            recs.append((fn[:-5], doc))
        out[ds] = recs
    return out

def convert(ds, legacy_id, doc):
    t = parse_ts(doc["polledAt"])
    slot = t.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"slot": slot, "polledAt": doc["polledAt"], "legacyDocId": legacy_id}
    for k, v in doc.items():
        if k not in rec: rec[k] = v
    return t, rec

def live_records_present(out):
    """True if any NDJSON line under out/<dataset>/ lacks legacyDocId (i.e. was written by the collector)."""
    for ds in DATASETS:
        d = os.path.join(out, ds)
        if not os.path.isdir(d): continue
        for fn in os.listdir(d):
            for line in open(os.path.join(d, fn), encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line: continue
                try:
                    if "legacyDocId" not in json.loads(line): return True
                except ValueError:
                    return True
    return False

def append_missing(export, out):
    """Append-only top-up. For each exported reading whose (dataset, slot) is absent, append one line
    to data/<dataset>/<day>.ndjson. Existing lines are never touched. Idempotent (re-running adds nothing)."""
    exp = load_export(export)
    added = 0
    for ds in DATASETS:
        present = set()
        d = os.path.join(out, ds)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                fp = os.path.join(d, fn)
                fh = gzip.open(fp, "rt", encoding="utf-8", errors="replace") if fn.endswith(".gz") else open(fp, encoding="utf-8", errors="replace")
                for line in fh:
                    try: present.add(json.loads(line)["slot"])
                    except Exception: pass
                fh.close()
        items = sorted((convert(ds, lid, doc) for lid, doc in exp[ds]), key=lambda x: x[0])
        n = 0
        for t, rec in items:
            if rec["slot"] in present: continue
            os.makedirs(d, exist_ok=True)
            line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n"
            if ds in GZIP_DATASETS:   # same layout as the live collector: one gzip member appended per record
                with open(os.path.join(d, rec["slot"][:10] + ".ndjson.gz"), "ab") as f:
                    f.write(gzip.compress(line.encode("utf-8"), mtime=0))
            else:
                with open(os.path.join(d, rec["slot"][:10] + ".ndjson"), "a", encoding="utf-8") as f:
                    f.write(line)
            present.add(rec["slot"]); n += 1
        print(f"{ds}: appended {n}"); added += n
    print(f"TOP-UP DONE: {added} records appended, nothing removed")
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", default="data")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--append-missing", action="store_true",
                    help="safe top-up for a live repo: append export records whose hour slot is not already present; never rewrites or removes anything")
    a = ap.parse_args()
    if a.append_missing:
        sys.exit(append_missing(a.export, a.out))
    if not a.validate_only and live_records_present(a.out):
        print(f"REFUSING: {a.out} already contains collector records; rebuilding would delete them.")
        sys.exit(2)
    exp = load_export(a.export)
    if not a.validate_only:
        for ds in DATASETS:
            shutil.rmtree(os.path.join(a.out, ds), ignore_errors=True)
            os.makedirs(os.path.join(a.out, ds), exist_ok=True)
        byday = collections.defaultdict(list)
        for ds in DATASETS:
            items = []
            for lid, doc in exp[ds]:
                t, rec = convert(ds, lid, doc)
                items.append((t, lid, rec))
            items.sort(key=lambda x: (x[0], x[1]))
            for t, lid, rec in items:
                byday[(ds, rec["slot"][:10])].append(rec)
        for (ds, day), recs in byday.items():
            with open(os.path.join(a.out, ds, day + ".ndjson"), "w", encoding="utf-8") as f:
                for r in recs: f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
        for c in LEGACY:
            src = os.path.join(a.export, c)
            if not os.path.isdir(src): continue
            dst = os.path.join(a.out, "_legacy", c)
            shutil.rmtree(dst, ignore_errors=True); os.makedirs(dst)
            for fn in os.listdir(src):
                shutil.copy2(os.path.join(src, fn), os.path.join(dst, fn))
    # ---- validation ----
    ok = True
    for ds in DATASETS:
        n = 0; bad = 0; mism = 0
        orig = {lid: doc for lid, doc in exp[ds]}
        seen = set()
        d = os.path.join(a.out, ds)
        for fn in (sorted(os.listdir(d)) if os.path.isdir(d) else []):
            for line in open(os.path.join(d, fn), encoding="utf-8"):
                if not line.strip(): continue
                try:
                    r = json.loads(line); parse_ts(r["polledAt"])
                except Exception:
                    bad += 1; continue
                n += 1
                body = {k: v for k, v in r.items() if k not in ("slot", "legacyDocId")}
                lid = r["legacyDocId"]; seen.add(lid)
                if body != orig.get(lid): mism += 1
                if r["slot"][:10] != fn[:10]: mism += 1
        cnt_ok = (n == len(exp[ds]) and len(seen) == len(exp[ds]))
        print(f"{ds}: export={len(exp[ds])} records={n} bad_json={bad} roundtrip_mismatch={mism} count_match={cnt_ok}")
        ok &= (bad == 0 and mism == 0 and cnt_ok)
    for c in LEGACY:
        s = os.path.join(a.export, c); dd = os.path.join(a.out, "_legacy", c)
        if os.path.isdir(s):
            same = sorted(os.listdir(s)) == sorted(os.listdir(dd)) and all(open(os.path.join(s, f), "rb").read() == open(os.path.join(dd, f), "rb").read() for f in os.listdir(s))
            print(f"_legacy/{c}: files={len(os.listdir(s))} verbatim={same}"); ok &= same
    print("VALIDATION", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
if __name__ == "__main__": main()
