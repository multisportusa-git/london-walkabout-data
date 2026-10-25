#!/usr/bin/env python3
"""Hourly TfL collector (stdlib only, Python >= 3.10).

Polls five TfL datasets and appends one compact-JSON line per dataset to
data/<dataset>/<YYYY-MM-DD>.ndjson, plus one health line per slot to
data/_health/<YYYY-MM-DD>.ndjson.

Timing model ("every hour on the hour"), see plan_slots():
  * The scheduler fires around HH:50 (and a backup around HH:05); an external
    trigger may also dispatch the workflow at HH:00.
  * Current hour H: filled straight away if any dataset is missing and the run
    starts no later than H+45 min (late fill; polledAt records the real time).
  * Next hour H+1: if the run starts at minute >= 30, sleep until H+1:00:00
    and poll. --current-only disables this (used for manual runs).
  * Idempotent: a dataset that already has a record for a slot is skipped.

No closure-window skipping and no station filtering: everything, every hour.
Exit codes: 0 every planned slot complete, 2 a dataset is still missing for a
slot after the retry pass (an unrecoverable gap), 3 complete but TfL rejected the
key (collected anonymously instead), 1 bad arguments.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nrtext import html_text, norm_url  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover - tzdata missing
    LONDON = None

API = "https://api.tfl.gov.uk"
DATASETS = ["liftDisruptions", "lineStatus", "stopDisruptions", "crowdingReadings", "arrivalsReadings", "arrivalsDetail",
            "busLineStatus", "networkDisruptions", "crowdingNetwork", "nationalRailDepartures", "nationalRailIncidents",
            "nationalRailStations", "stationAccess", "arrivalsNetwork"]
# arrivalsNetwork: every live prediction on the whole network, one call per mode (/Mode/{mode}/Arrivals?count=-1):
# tube, Elizabeth line, Overground, DLR, tram, river bus (~7,000 rows an hour). Cable car and Woolwich Ferry publish none.
# nationalRailStations: the whole Knowledgebase stations feed (~2,600 stations, ~55 MB), once a day; each
# day stores a short fingerprint of every station and the full entry only for stations that changed.
DAILY_AT_UTC = {"nationalRailStations": 3, "stationAccess": 3}
# stationAccess: TfL's station accessibility data (every lift at every station, which areas it links,
# platforms, step-free paths, toilets): the ZIP is kept whole in data/_reference/tfl-stationdata/ each
# time its content changes, so a lift outage can be judged against how many lifts the station has.   # collected once per UTC day, at the first run from this hour
# busLineStatus: all ~675 bus routes in one call; good-service routes are counted, the rest stored in full detail.
# networkDisruptions: every stop/station notice network-wide (rail, bus, National Rail) + bus and National Rail line disruptions.
# crowdingNetwork: live crowding at every tube and Elizabeth line station (~315 calls; runs last).
# arrivalsDetail: one compact row per live prediction, so departure boards, headways, platforms and
# destinations can be analysed later (arrivalsReadings keeps the old per-line summary shape).
DETAIL_FIELDS = ["naptanId", "lineId", "platformName", "direction", "destinationNaptanId", "timeToStation", "vehicleId"]
# Large datasets are stored gzip-compressed: each hour appends one gzip member to <day>.ndjson.gz
# (multi-member gzip; `gzip -dc`, Python gzip and zlib all read it as one stream). Keeps the repo lean.
GZIP_DATASETS = {"arrivalsDetail", "busLineStatus", "networkDisruptions", "crowdingNetwork", "nationalRailDepartures",
                 "nationalRailIncidents", "nationalRailStations", "arrivalsNetwork"}
LATE_FILL_MAX = timedelta(minutes=45)
RETRY_PASS_DELAY_S = 45
CHUNK = 20
TIMEOUT_S = 20
ATTEMPTS = 4
BREAKER_AFTER = 6  # after this many failed requests in a row, try each further request once
BREAKER_OPEN = 20  # after this many, skip further requests in this pass (TfL down); the retry pass tries again

KEY_RE = re.compile(r"(app_key|app-key|appkey)=[^&\s'\"]+", re.I)


# ----------------------------------------------------------------- helpers
def redact(text, key=None):
    """Strip API keys from anything that might get logged."""
    s = KEY_RE.sub(r"\1=***", str(text))
    if key:
        s = s.replace(key, "***")
    return s


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_hour(dt):
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def plan_slots(now, current_only=False):
    """Return [(slot_utc, seconds_from_now_until_slot)] in the order to collect.

    current hour: included while now <= H + LATE_FILL_MAX (skipped later if already complete)
    next hour:    included when minute >= 30
    current_only: always just the current hour (manual runs, the HH:00 external trigger)
    """
    now = now.astimezone(timezone.utc)
    cur = floor_hour(now)
    if current_only:  # manual or HH:00-dispatched run: collect the current hour now, whatever the minute
        return [(cur, 0.0)]
    plan = []
    if now - cur <= LATE_FILL_MAX:
        plan.append((cur, 0.0))
    if not current_only and now.minute >= 30:
        nxt = cur + timedelta(hours=1)
        plan.append((nxt, (nxt - now).total_seconds()))
    return plan


def is_closure_window(dt):
    """Metadata only: London 00:00-04:59 or 23:30-23:59."""
    if LONDON is None:
        return False
    l = dt.astimezone(LONDON)
    return (0 <= l.hour < 5) or (l.hour == 23 and l.minute >= 30)


# ---------------------------------------------------------------- registry
def load_registry(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def derive(reg):
    """Derive polling lists from the registry (order preserved)."""
    st = reg["stations"]
    disruption_ids = [s["disruptionId"] for s in st] + [p["naptanId"] for p in reg["piers"]]
    crowding = [[s["slug"], s["crowdingId"]] for s in st if s.get("trackCrowding")]
    # arrivals: every mode-specific id at the station (tube + Elizabeth line + Overground + DLR platforms)
    arrivals = [[s["slug"], s.get("arrivalsIds") or [s["crowdingId"]]] for s in st if s.get("trackArrivals")]
    return {
        "disruptionIds": disruption_ids,
        "crowding": crowding,
        "arrivals": arrivals,
        "stationsPolled": len(st),
        "statusModes": reg.get("statusModes", ["tube,dlr,overground,elizabeth-line", "river-bus"]),
        "crowdingNetworkIds": reg.get("crowdingNetworkIds", []),
        "networkDisruptionModes": reg.get("networkDisruptionModes", {}),
        "nrStations": reg.get("nationalRailStations", []),
        "kbIncidentsUrl": reg.get("nationalRailIncidentsUrl"),
        "kbStationsUrl": reg.get("nationalRailStationsUrl"),
        "arrivalsNetworkModes": reg.get("arrivalsNetworkModes", ["tube", "elizabeth-line", "overground", "dlr", "tram", "river-bus"]),
    }


# -------------------------------------------------------------- HTTP client
def default_fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "london-walkabout-collector/1.1", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, dict(e.headers or {})


# ---------------------------------------------------------- National Rail
# Rail Data Marketplace "Live Departure Board - Staff Version" (LDBSVWS), exactly as the London Commuter
# broker calls it (internal/raildata/client.go, verified live 2026-10-04): GET
# {base}/GetDepBoardWithDetails/{CRS}/{YYYYMMDDTHHMMSS London time}, header x-apikey. Times come back as
# local datetimes without offset. The key travels only in that header, never in a URL, log or record.
NR_DEP_BASE = "https://api1.raildata.org.uk/1010-live-departure-board---staff-version1_0/LDBSVWS/api/20220120"
NR_SERVICE_FIELDS = ["rid", "uid", "operatorCode", "std", "etd", "atd", "sta", "eta", "ata", "departureType",
                     "platform", "platformIsHidden", "isCancelled", "cancelReason", "delayReason",
                     "destination", "origin", "length", "adhocAlerts",
                     # added 25 Oct 2026 (v1 only gains fields): train loading where the operator publishes it
                     "trainid", "category", "loadingPct", "coachLoading"]
# Full hour of departures: GetDepBoardWithDetails returns at most 10 trains whatever numRows says (checked live
# 24 Oct 2026: London Bridge 10 = ~10 minutes); GetDepartureBoardByCRS honours numRows/timeWindow (76 trains,
# the whole hour) and carries the same fields plus formation/loading.
NR_BOARD_PATH = "GetDepartureBoardByCRS/{crs}/{when}?numRows=150&timeWindow=60"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never carry the x-apikey header to another host
        return None


def nr_default_fetch(url, key, timeout, accept="application/json"):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"x-apikey": key, "Accept": accept,
                                               "User-Agent": "london-walkabout-collector/1.1"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""


class NRClient:
    def __init__(self, key, fetch=nr_default_fetch, sleep=time.sleep):
        self.key, self.fetch, self.sleep = key, fetch, sleep
        self.requests = self.failures = 0
        self.status_counts = {}

    def board(self, crs, when_local):
        url = f"{NR_DEP_BASE}/" + NR_BOARD_PATH.format(crs=urllib.parse.quote(crs), when=when_local.strftime('%Y%m%dT%H%M%S'))
        for attempt in range(2):
            if self.requests:
                self.sleep(0.25)
            self.requests += 1
            try:
                status, body = self.fetch(url, self.key, TIMEOUT_S)
            except Exception as e:
                status, body = None, b""
                log(f"  ! nationalRail {crs} -> {type(e).__name__}: {redact(e, self.key)}")
            self.status_counts[str(status)] = self.status_counts.get(str(status), 0) + 1
            if status == 200:
                try:
                    return json.loads(body.decode("utf-8"))
                except Exception:
                    log(f"  ! nationalRail {crs} -> 200 but body not JSON")
            elif status in (401, 403):
                log(f"  ! nationalRail {crs} -> HTTP {status} (key not accepted for this product)")
                break
            else:
                log(f"  ! nationalRail {crs} -> HTTP {status}")
            if attempt == 0:
                self.sleep(2.0)
        self.failures += 1
        return None


NR_KB_INCIDENTS_URL = "https://api1.raildata.org.uk/1010-knowlegebase-incidents-xml-feed1_0/incidents.xml"
NR_KB_STATIONS_URL = ("https://api1.raildata.org.uk/1010-nationalrail-knowledgebase-stations-feed-_json_---production5_0"
                      "/stations")  # from the product's Specification tab; checked 24 Oct 2026: HTTP 200, 2,614 stations


def parse_incidents(xml_bytes):
    """Knowledgebase incidents feed -> {id: (version, element_xml, meta)}. Tags matched by local name
    (namespaces vary). Verified against the live feed 24 Oct 2026: <Incidents><PtIncident> with
    IncidentNumber, Version, Planned, ValidityPeriod/StartTime|EndTime, Summary, IncidentPriority,
    Affects/Operators/AffectedOperator/OperatorRef."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    local = lambda t: t.rsplit("}", 1)[-1]
    def first(el, name):
        for x in el.iter():
            if local(x.tag) == name and (x.text or "").strip():
                return x.text.strip()
        return None
    out = {}
    for el in root:
        if local(el.tag) != "PtIncident":
            continue
        iid = first(el, "IncidentNumber")
        if not iid:
            continue
        ops = sorted({(x.text or "").strip() for x in el.iter() if local(x.tag) == "OperatorRef" and (x.text or "").strip()})
        meta = {"planned": (first(el, "Planned") or "").lower() == "true", "start": first(el, "StartTime"),
                "end": first(el, "EndTime"), "priority": first(el, "IncidentPriority"), "operators": ops,
                "summary": first(el, "Summary"), "cleared": (first(el, "ClearedIncident") or "").lower() == "true",
                "links": [norm_url(x.text) for x in el.iter() if local(x.tag) == "Uri" and (x.text or "").strip()],
                "routes": first(el, "RoutesAffected"), "description": first(el, "Description")}
        out[iid] = (first(el, "Version") or "", ET.tostring(el, encoding="unicode"), meta)
    return out


def known_incident_versions(out):
    """{incident id: last stored version} from this dataset's own history (no separate state file)."""
    seen = {}
    d = Path(out) / "nationalRailIncidents"
    if not d.is_dir():
        return seen
    for f in sorted(d.iterdir()):
        try:
            fh = open_text(f)
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for x in rec.get("newOrChanged") or []:
                    seen[x.get("id")] = x.get("version")
            fh.close()
        except (OSError, EOFError):
            continue
    return seen


def kb_incidents_feed(ctx):
    """(status, xml bytes) of the KB incidents feed, fetched once per collection pass and shared by
    lineStatus (to resolve National Rail messages) and nationalRailIncidents."""
    if "_kb" in ctx:
        return ctx["_kb"]
    key = ctx.get("kbIncidentsKey")
    fetch = ctx.get("nr_fetch") or nr_default_fetch
    status, body = None, b""
    for attempt in range(2):
        try:
            status, body = fetch(ctx.get("kbIncidentsUrl") or NR_KB_INCIDENTS_URL, key, 30, accept="application/xml")
        except Exception as e:
            log(f"  ! nationalRailIncidents -> {type(e).__name__}: {redact(e, key)}")
        if status == 200 or status in (401, 403):
            break
        time.sleep(0 if ctx.get("no_sleep") else 2.0)
    if status == 200 and body and body[:2] == b"\x1f\x8b":   # the gateway gzips this feed regardless of Accept-Encoding
        try:
            body = gzip.decompress(body)
        except Exception:
            status = None
    ctx["_kb"] = (status, body)
    return ctx["_kb"]


def collect_nr_incidents(c, ctx):
    key = ctx.get("kbIncidentsKey")
    if not key:
        return None, "skipped", 0, "no NR_KB_INCIDENTS_KEY"
    status, body = kb_incidents_feed(ctx)
    if status != 200 or not body:
        return None, "failed", 0, f"HTTP {status}"
    import hashlib
    try:
        incidents = parse_incidents(body)
    except Exception as e:
        # Unparseable: keep the whole feed so nothing is lost, and flag it.
        return ({"source": "RDM Knowledgebase Incidents (XML)", "parseError": type(e).__name__,
                 "feedSha256": hashlib.sha256(body).hexdigest(), "xml": body.decode("utf-8", "replace")},
                "partial", 0, f"could not parse ({type(e).__name__}); raw feed stored")
    seen = known_incident_versions(ctx["out"])
    changed = [{"id": i, "version": v, "xml": x} for i, (v, x, m) in incidents.items() if seen.get(i) != v]
    active = [[i, v, m["planned"], m["priority"], m["start"], m["end"], m["operators"], m["cleared"]]
              for i, (v, x, m) in sorted(incidents.items())]
    by_op = {}
    for _, (_, _, m) in incidents.items():
        for o in m["operators"]:
            by_op[o] = by_op.get(o, 0) + 1
    body_rec = {"source": "RDM Knowledgebase Incidents (XML)", "feedSha256": hashlib.sha256(body).hexdigest(),
                "count": len(incidents), "planned": sum(1 for _, (_, _, m) in incidents.items() if m["planned"]),
                "byOperator": dict(sorted(by_op.items())),
                "activeFields": ["id", "version", "planned", "priority", "start", "end", "operators", "cleared"],
                "active": active, "newOrChanged": changed}
    return body_rec, "ok", len(incidents), f"{len(incidents)} incidents, {len(changed)} new or changed"


def last_stations_index(out):
    """{crs: fingerprint} from the newest stored stations record (no separate state file)."""
    d = Path(out) / "nationalRailStations"
    if not d.is_dir():
        return {}
    for f in sorted(d.iterdir(), reverse=True):
        last = None
        try:
            fh = open_text(f)
            for line in fh:
                if line.strip():
                    last = line
            fh.close()
        except (OSError, EOFError):
            continue
        if last:
            try:
                return json.loads(last).get("index") or {}
            except ValueError:
                continue
    return {}


def collect_nr_stations(c, ctx):
    key = ctx.get("kbStationsKey")
    if not key:
        return None, "skipped", 0, "no NR_KB_STATIONS_KEY"
    fetch = ctx.get("nr_fetch") or nr_default_fetch
    status, body = None, b""
    for attempt in range(2):
        try:
            status, body = fetch(ctx.get("kbStationsUrl") or NR_KB_STATIONS_URL, key, 180, accept="application/json")
        except Exception as e:
            log(f"  ! nationalRailStations -> {type(e).__name__}: {redact(e, key)}")
        if status == 200 or status in (401, 403):
            break
        time.sleep(0 if ctx.get("no_sleep") else 5.0)
    if status != 200 or not body:
        return None, "failed", 0, f"HTTP {status}"
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    import hashlib
    try:
        doc = json.loads(body)
        stations = doc["stations"] if isinstance(doc, dict) else doc
        assert isinstance(stations, list)
    except Exception as e:
        return ({"source": "RDM Knowledgebase Stations (JSON)", "parseError": type(e).__name__,
                 "raw": body.decode("utf-8", "replace")}, "partial", 0, f"could not parse ({type(e).__name__}); raw feed stored")
    index, by_crs = {}, {}
    for st in stations:
        if not isinstance(st, dict):
            continue
        crs = st.get("crsCode") or st.get("nationalLocationCode") or st.get("name")
        if not crs:
            continue
        index[crs] = hashlib.sha256(json.dumps(st, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        by_crs[crs] = st
    prev = last_stations_index(ctx["out"])
    changed = [by_crs[k] for k in sorted(index) if prev.get(k) != index[k]]
    removed = sorted(k for k in prev if k not in index)
    rec = {"source": "RDM Knowledgebase Stations (JSON)", "count": len(index), "index": index,
           "changed": changed, "removed": removed}
    return rec, "ok", len(index), f"{len(index)} stations, {len(changed)} new or changed, {len(removed)} removed"


STATIONDATA_PATH = "/stationdata/tfl-stationdata-detailed.zip"


def collect_station_access(c, ctx):
    import hashlib, io, zipfile, csv
    status, body = None, b""
    for attempt in range(3):
        try:
            c.requests += 1
            status, body, _ = c.fetch(c._url(STATIONDATA_PATH), 60)
        except Exception as e:
            log(f"  ! stationAccess -> {type(e).__name__}: {redact(e, c.key)}")
        if status == 200 and body[:2] == b"PK":
            break
        time.sleep(0 if ctx.get("no_sleep") else 3.0)
    if status != 200 or body[:2] != b"PK":
        return None, "failed", 0, f"HTTP {status}"
    sha = hashlib.sha256(body).hexdigest()
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
        files = {i.filename: i.file_size for i in z.infolist()}
        lifts = list(csv.DictReader(io.TextIOWrapper(z.open("Lifts.csv"), encoding="utf-8-sig")))
        stations = list(csv.DictReader(io.TextIOWrapper(z.open("Stations.csv"), encoding="utf-8-sig")))
    except Exception as e:
        return None, "failed", 0, f"not a readable ZIP ({type(e).__name__})"
    ref = Path(ctx["out"]) / "_reference" / "tfl-stationdata"
    fname = f"{sha[:16]}.zip"
    changed = not (ref / fname).exists()
    if changed:  # content-addressed: a new file only when TfL changes the data; nothing is ever replaced
        ref.mkdir(parents=True, exist_ok=True)
        (ref / fname).write_bytes(body)
    rec = {"source": "https://api.tfl.gov.uk" + STATIONDATA_PATH, "sha256": sha, "file": f"_reference/tfl-stationdata/{fname}",
           "changed": changed, "files": files, "lifts": len(lifts), "stations": len(stations)}
    return rec, "ok", len(lifts), f"{len(lifts)} lifts at {len(stations)} stations, {'new version stored' if changed else 'unchanged'}"


def service_loading(sv):
    """(loading % for the whole train or None, [[coach, loading %], ...] or None) from Darwin's formation data."""
    fm = sv.get("formation") if isinstance(sv.get("formation"), dict) else {}
    lp = ((fm.get("serviceLoading") or {}).get("loadingPercentage") or {}) if isinstance(fm.get("serviceLoading"), dict) else {}
    pct = lp.get("Value") if isinstance(lp, dict) else None
    coaches = []
    for c in fm.get("coaches") or []:
        if isinstance(c, dict) and isinstance(c.get("loading"), dict) and c["loading"].get("Value") is not None:
            coaches.append([c.get("number"), c["loading"]["Value"]])
    return pct, (coaches or None)


def compact_service(sv):
    row = []
    pct, coaches = service_loading(sv)
    for f in NR_SERVICE_FIELDS:
        v = sv.get(f)
        if f == "loadingPct":
            v = pct
        elif f == "coachLoading":
            v = coaches
        elif f in ("destination", "origin") and isinstance(v, list):
            v = [x.get("crs") or x.get("locationName") for x in v if isinstance(x, dict)]
        elif f in ("cancelReason", "delayReason") and isinstance(v, dict):
            v = v.get("Value")
        row.append(v)
    return row


class Client:
    def __init__(self, key=None, fetch=default_fetch, sleep=time.sleep, rng=random.random):
        self.key = key or None
        self.fetch = fetch
        self.sleep = sleep
        self.rng = rng
        self.requests = 0
        self.retries = 0
        self.http429 = 0
        self._first = True
        self.consecutive_failures = 0  # circuit breaker: during an outage, stop multiplying retries
        self.key_rejected = False      # TfL answered "Invalid app_key": key dropped, run continues anonymously

    def counters(self):
        return (self.requests, self.retries, self.http429)

    def _url(self, path):
        url = API + path
        if self.key:
            url += ("&" if "?" in url else "?") + "app_key=" + urllib.parse.quote(self.key)
        return url

    def get(self, path):
        """GET JSON with retry/backoff. Returns parsed JSON or None."""
        if self.consecutive_failures >= BREAKER_OPEN:
            return None
        attempts = 1 if self.consecutive_failures >= BREAKER_AFTER else ATTEMPTS
        for attempt in range(attempts):
            url = self._url(path)  # rebuilt each attempt: the key may have been dropped as invalid
            if not self._first:
                self.sleep(0.2 + 0.1 * self.rng())  # politeness delay
            self._first = False
            self.requests += 1
            retry_after = None
            try:
                status, body, headers = self.fetch(url, TIMEOUT_S)
            except Exception as e:  # network error / timeout
                status, body, headers = None, b"", {}
                log(f"  ! {path[:70]} -> {type(e).__name__}: {redact(e, self.key)}")
            if status == 200:
                try:
                    out = json.loads(body.decode("utf-8"))
                    self.consecutive_failures = 0
                    return out
                except Exception:
                    log(f"  ! {path[:70]} -> 200 but body not JSON")
            elif status == 429 and self.key and b"invalid app_key" in (body or b"").lower():
                # A wrong or revoked key makes TfL refuse EVERY call. Never lose the hour over it:
                # drop the key, retry anonymously, flag the run (the key itself is never logged).
                self.key = None
                self.key_rejected = True
                log("::error::TfL rejected TFL_APP_KEY (invalid app_key). Continuing anonymously; replace the repo secret.")
                continue
            elif status == 429:
                self.http429 += 1
                ra = {k.lower(): v for k, v in headers.items()}.get("retry-after")
                try:
                    retry_after = min(float(ra), 60.0) if ra else None
                except ValueError:
                    retry_after = None
                log(f"  ! {path[:70]} -> 429")
            elif status is not None and 400 <= status < 500 and status != 408:
                log(f"  ! {path[:70]} -> HTTP {status} (not retried)")
                return None  # a client error is about this request, not an outage
            elif status is not None:
                log(f"  ! {path[:70]} -> HTTP {status}")
            if attempt < attempts - 1:
                self.retries += 1
                delay = retry_after if retry_after is not None else (1.5 * (2 ** attempt) + self.rng())
                self.sleep(delay)
        self.consecutive_failures += 1
        return None


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ datasets
# Each collector returns (body_or_None, status, items, detail).
# status: "ok" | "partial" | "failed"; body None means nothing to write.

def collect_lift(c, ctx):
    d = c.get("/Disruptions/Lifts/v2/")
    if not isinstance(d, list):
        return None, "failed", 0, "no response"
    body = {"source": API + "/Disruptions/Lifts/v2/", "count": len(d), "disruptions": d}
    return body, "ok", len(d), f"{len(d)} lift disruptions"


def status_entry(s):
    """Compact line status, keeping what an app needs: when it applies and what the disruption covers."""
    e = {"severity": s.get("statusSeverity"), "description": s.get("statusSeverityDescription"), "reason": s.get("reason")}
    vp = [[v.get("fromDate"), v.get("toDate")] for v in (s.get("validityPeriods") or []) if isinstance(v, dict)]
    if vp:
        e["validity"] = vp
    d = s.get("disruption")
    if isinstance(d, dict):
        e["disruption"] = {
            "category": d.get("category"), "categoryDescription": d.get("categoryDescription"),
            "created": d.get("created"), "additionalInfo": d.get("additionalInfo"),
            "affectedStops": [x.get("naptanId") or x.get("id") for x in (d.get("affectedStops") or []) if isinstance(x, dict)],
        }
    return e


def collect_lines(c, ctx):
    lines, ok, modes = [], 0, ctx["statusModes"]
    for m in modes:
        r = c.get(f"/Line/Mode/{m}/Status")
        if isinstance(r, list):
            ok += 1
            for l in r:
                lines.append({
                    "id": l["id"], "name": l["name"], "mode": l.get("modeName"),
                    "statuses": [status_entry(s) for s in l.get("lineStatuses", [])],
                })
    if not lines:
        return None, "failed", 0, f"0/{len(modes)} mode calls"
    resolved = add_nr_messages(lines, ctx)
    body = {"source": API + "/Line/Mode/{" + ",".join(modes) + "}/Status",
            "linesRequested": [l["id"] for l in lines], "lines": lines}
    return body, ("ok" if ok == len(modes) else "partial"), len(lines), f"{ok}/{len(modes)} mode calls" + (
        f", {resolved} National Rail messages" if resolved else "")


def add_nr_messages(lines, ctx):
    """TfL gives National Rail statuses only a nationalrail.co.uk link as the reason. Resolve each link to
    the Knowledgebase incident it names and store that incident's own words with the status, so the
    record says why the service is what it is. TfL's reason is kept unchanged; unmatched links get nothing."""
    want = [st for l in lines for st in l["statuses"]
            if re.match(r"https?://", (st.get("reason") or "").strip())]
    if not want or not ctx.get("kbIncidentsKey"):
        return 0
    status, body = kb_incidents_feed(ctx)
    if status != 200 or not body:
        return 0
    try:
        incidents = parse_incidents(body)
    except Exception:
        return 0
    by_url = {}
    for iid, (v, _, m) in incidents.items():
        for u in m.get("links") or []:
            by_url.setdefault(u, (iid, v, m))
    n = 0
    for st in want:
        hit = by_url.get(norm_url(st["reason"]))
        if not hit:
            continue
        iid, v, m = hit
        st["nrIncident"] = {"id": iid, "version": v, "summary": m.get("summary"),
                            "routes": html_text(m.get("routes"), 600), "message": html_text(m.get("description")),
                            "start": m.get("start"), "end": m.get("end"), "planned": m.get("planned")}
        n += 1
    return n


def collect_stop(c, ctx):
    ids = ctx["disruptionIds"]
    chunks = [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]
    merged, ok = [], 0
    for ch in chunks:
        r = c.get("/StopPoint/" + ",".join(ch) + "/Disruption")
        if isinstance(r, list):
            ok += 1
            merged += r
    if ok == 0:
        return None, "failed", 0, f"0/{len(chunks)} chunks"
    body = {"source": "StopPoint/{chunked}/Disruption", "chunkCount": len(chunks), "chunksSucceeded": ok,
            "closureWindow": ctx["closure"], "closureWindowApplied": False,
            "stationsPolled": ctx["stationsPolled"], "disruptions": merged}
    return body, ("ok" if ok == len(chunks) else "partial"), len(merged), f"{ok}/{len(chunks)} chunks"


def collect_crowding(c, ctx):
    st = {}
    for slug, cid in ctx["crowding"]:
        r = c.get(f"/crowding/{cid}/Live")
        st[slug] = r if isinstance(r, dict) else None
    good = sum(1 for v in st.values() if v)
    if good == 0:
        return None, "failed", 0, f"0/{len(st)} stations"
    body = {"source": "crowding/{id}/Live", "closureWindow": ctx["closure"], "stations": st}
    return body, ("ok" if good == len(st) else "partial"), good, f"{good}/{len(st)} stations"


def summarise_arrivals(preds):
    by = {}
    for p in preds:
        name = p.get("lineName") or p.get("lineId")
        t = p.get("timeToStation")
        if name is None:
            continue
        e = by.setdefault(name, {"count": 0, "minSeconds": None})
        e["count"] += 1
        if t is not None and (e["minSeconds"] is None or t < e["minSeconds"]):
            e["minSeconds"] = t
    return {"totalPredictions": len(preds), "nextByLine": by}


def fetch_arrivals(c, ctx):
    """One pass over every arrivals id; cached in ctx so the summary and detail datasets share the calls.
    Returns {slug: [predictions] or None}; predictions are de-duplicated by TfL prediction id."""
    if "_arrivals" in ctx:
        return ctx["_arrivals"]
    out = {}
    for slug, ids in ctx["arrivals"]:
        preds, seen, any_ok = [], set(), False
        for i in ids:
            r = c.get(f"/StopPoint/{i}/Arrivals")
            if isinstance(r, list):
                any_ok = True
                for p in r:
                    # TfL gives every DLR prediction at a stop the SAME id (seen 24 Oct 2026), so the id alone
                    # would collapse them to one; the same prediction polled via two ids is still a duplicate.
                    k = (tuple(p.get(f) for f in ("id", "naptanId", "platformName", "destinationNaptanId",
                                                  "timeToStation", "vehicleId", "lineId")) if isinstance(p, dict) else None)
                    if k is not None:
                        if k in seen:
                            continue
                        seen.add(k)
                    preds.append(p)
        out[slug] = preds if any_ok else None
    ctx["_arrivals"] = out
    return out


def collect_arrivals(c, ctx):
    raw = fetch_arrivals(c, ctx)
    st = {slug: (summarise_arrivals(p) if p is not None else None) for slug, p in raw.items()}
    good = sum(1 for v in st.values() if v is not None)
    if good == 0:
        return None, "failed", 0, f"0/{len(st)} stations"
    body = {"source": "StopPoint/{id}/Arrivals", "closureWindow": ctx["closure"], "stations": st}
    return body, ("ok" if good == len(st) else "partial"), good, f"{good}/{len(st)} stations"


def collect_arrivals_detail(c, ctx):
    raw = fetch_arrivals(c, ctx)
    st, rows = {}, 0
    for slug, preds in raw.items():
        if preds is None:
            st[slug] = None
            continue
        st[slug] = [[p.get(f) for f in DETAIL_FIELDS] for p in sorted(preds, key=lambda p: (p.get("lineId") or "", p.get("timeToStation") or 0))]
        rows += len(preds)
    good = sum(1 for v in st.values() if v is not None)
    if good == 0:
        return None, "failed", 0, f"0/{len(st)} stations"
    body = {"source": "StopPoint/{id}/Arrivals", "closureWindow": ctx["closure"], "fields": DETAIL_FIELDS, "stations": st}
    return body, ("ok" if good == len(st) else "partial"), rows, f"{good}/{len(st)} stations, {rows} predictions"


def strip_types(x):
    """Drop TfL's '$type' annotations (pure overhead) recursively."""
    if isinstance(x, dict):
        return {k: strip_types(v) for k, v in x.items() if k != "$type"}
    if isinstance(x, list):
        return [strip_types(v) for v in x]
    return x


def collect_bus(c, ctx):
    r = c.get("/Line/Mode/bus/Status")
    if not isinstance(r, list):
        return None, "failed", 0, "no response"
    disrupted, good = [], 0
    for l in r:
        sts = l.get("lineStatuses", [])
        if sts and all(s.get("statusSeverity") == 10 for s in sts):
            good += 1
        else:
            disrupted.append({"id": l.get("id"), "name": l.get("name"), "statuses": [status_entry(s) for s in sts]})
    body = {"source": API + "/Line/Mode/bus/Status", "totalLines": len(r), "goodServiceCount": good, "disrupted": disrupted}
    return body, "ok", len(disrupted), f"{len(r)} routes, {len(disrupted)} not good service"


def collect_network_disruptions(c, ctx):
    modes = ctx["networkDisruptionModes"]
    out, ok = {}, 0
    for key, m in modes.items():
        path = f"/StopPoint/Mode/{m}/Disruption" if key.startswith("stop") else f"/Line/Mode/{m}/Disruption"
        r = c.get(path)
        out[key] = strip_types(r) if isinstance(r, list) else None
        ok += isinstance(r, list)
    if ok == 0:
        return None, "failed", 0, f"0/{len(modes)} feeds"
    items = sum(len(v) for v in out.values() if v)
    body = {"source": "StopPoint/Mode/{modes}/Disruption + Line/Mode/{modes}/Disruption", "modes": modes, "feeds": out}
    return body, ("ok" if ok == len(modes) else "partial"), items, f"{ok}/{len(modes)} feeds, {items} notices"


def collect_crowding_network(c, ctx):
    ids, st = ctx["crowdingNetworkIds"], {}
    for i in ids:
        r = c.get(f"/crowding/{i}/Live")
        st[i] = ({"dataAvailable": r.get("dataAvailable"), "percentageOfBaseline": r.get("percentageOfBaseline"),
                  "timeUtc": r.get("timeUtc")} if isinstance(r, dict) else None)
    got = sum(1 for v in st.values() if v is not None)
    if got == 0:
        return None, "failed", 0, f"0/{len(ids)} stations"
    avail = sum(1 for v in st.values() if v and v.get("dataAvailable"))
    body = {"source": "crowding/{naptanId}/Live", "closureWindow": ctx["closure"], "stations": st}
    return body, ("ok" if got == len(ids) else "partial"), avail, f"{got}/{len(ids)} answered, {avail} with live data"


def collect_arrivals_network(c, ctx):
    modes = ctx.get("arrivalsNetworkModes") or []
    out, ok, rows = {}, 0, 0
    for m in modes:
        r = c.get(f"/Mode/{m}/Arrivals?count=-1")
        if isinstance(r, list):
            ok += 1
            out[m] = [[p.get(f) for f in DETAIL_FIELDS] for p in
                      sorted((p for p in r if isinstance(p, dict)), key=lambda p: (p.get("lineId") or "", p.get("naptanId") or "", p.get("timeToStation") or 0))]
            rows += len(out[m])
        else:
            out[m] = None
    if ok == 0:
        return None, "failed", 0, f"0/{len(modes)} modes"
    body = {"source": "Mode/{mode}/Arrivals?count=-1", "closureWindow": ctx["closure"], "fields": DETAIL_FIELDS, "modes": out}
    return body, ("ok" if ok == len(modes) else "partial"), rows, f"{ok}/{len(modes)} modes, {rows} predictions"


def collect_national_rail(c, ctx):
    nr, stations = ctx.get("nr"), ctx.get("nrStations") or []
    if nr is None:
        return None, "skipped", 0, "no NR_API_KEY"
    when = ctx["now_local"]
    out, svc = {}, 0
    for crs in stations:
        b = nr.board(crs, when)
        if not isinstance(b, dict):
            out[crs] = None
            continue
        rows = [compact_service(x) for x in (b.get("trainServices") or []) if isinstance(x, dict)]
        bus = [compact_service(x) for x in (b.get("busServices") or []) if isinstance(x, dict)]
        msgs = [strip_types(m) for m in (b.get("nrccMessages") or [])]
        out[crs] = {"generatedAt": b.get("generatedAt"), "servicesAreUnavailable": b.get("servicesAreUnavailable"),
                    "platformsAreHidden": b.get("platformsAreHidden"), "services": rows, "busServices": bus,
                    "nrccMessages": msgs}
        svc += len(rows)
    good = sum(1 for v in out.values() if v is not None)
    if good == 0:
        return None, "failed", 0, f"0/{len(stations)} stations"
    body = {"source": "RDM LDBSVWS " + NR_BOARD_PATH.split("/")[0] + " (next 60 min)", "boardTimeLocal": when.strftime("%Y-%m-%dT%H:%M:%S"),
            "fields": NR_SERVICE_FIELDS, "stations": out}
    return body, ("ok" if good == len(stations) else "partial"), svc, f"{good}/{len(stations)} stations, {svc} services"


COLLECTORS = {
    "liftDisruptions": collect_lift,
    "lineStatus": collect_lines,
    "stopDisruptions": collect_stop,
    "crowdingReadings": collect_crowding,
    "arrivalsReadings": collect_arrivals,
    "arrivalsDetail": collect_arrivals_detail,
    "busLineStatus": collect_bus,
    "networkDisruptions": collect_network_disruptions,
    "crowdingNetwork": collect_crowding_network,
    "nationalRailDepartures": collect_national_rail,
    "nationalRailIncidents": collect_nr_incidents,
    "nationalRailStations": collect_nr_stations,
    "stationAccess": collect_station_access,
    "arrivalsNetwork": collect_arrivals_network,
}


# ------------------------------------------------------------------- storage
def slot_str(slot):
    return slot.strftime("%Y-%m-%dT%H:00:00Z")


def ndjson_path(out, dataset, slot):
    ext = ".ndjson.gz" if dataset in GZIP_DATASETS else ".ndjson"
    return Path(out) / dataset / (slot.strftime("%Y-%m-%d") + ext)


def open_text(path):
    p = str(path)
    return gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")


def slot_present(out, dataset, slot):
    """True if the slot's day-file already holds a record for this slot."""
    p = ndjson_path(out, dataset, slot)
    if not p.exists():
        return False
    want = slot_str(slot)
    try:
        fh = open_text(p)
        lines = fh.readlines()
        fh.close()
    except (OSError, EOFError):  # a truncated gzip member: treat the slot as absent so it is re-collected
        return False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("slot") == want:
                return True
        except ValueError:
            continue
    return False


def day_present(out, dataset, slot):
    """True if the slot's day-file already holds any readable record (once-a-day datasets)."""
    p = ndjson_path(out, dataset, slot)
    if not p.exists():
        return False
    try:
        fh = open_text(p)
        ok = any(line.strip() for line in fh)
        fh.close()
        return ok
    except (OSError, EOFError):
        return False


def append_line(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
    if str(path).endswith(".gz"):
        with open(path, "ab") as fh:
            fh.write(gzip.compress(data.encode("utf-8"), mtime=0))
    else:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(data)


def make_record(slot, polled_at, auth_mode, body):
    rec = {"slot": slot_str(slot), "polledAt": iso(polled_at), "authMode": auth_mode}
    src = body.get("source")
    rec["source"] = src
    for k, v in body.items():
        if k != "source":
            rec[k] = v
    return rec


# ---------------------------------------------------------------------- run
def collect_slot(args, client, base_ctx, slot, todo, auth_mode, sleep, clock, started):
    """Collect `todo` datasets for one slot. Returns the list still missing."""
    ctx = dict(base_ctx, closure=is_closure_window(slot),
               now_local=slot.astimezone(LONDON) if LONDON else slot)  # boards are taken as at HH:00 London time
    health, before = {}, client.counters()

    def attempt(names):
        failed = []
        ctx.pop("_arrivals", None)  # each pass polls arrivals once, shared by both arrivals datasets
        ctx.pop("_kb", None)        # and fetches the KB incidents feed at most once, shared by lineStatus
        client.consecutive_failures = 0
        for name in names:
            t0 = time.time()
            body, status, items, detail = COLLECTORS[name](client, ctx)
            if body is None and status == "skipped":
                health[name] = {"status": "skipped", "items": 0, "detail": detail}
                log(f"[{name}] skipped ({detail})")
                continue
            if body is None:
                health[name] = {"status": "failed", "items": 0, "detail": detail}
                failed.append(name)
                log(f"[{name}] FAILED ({detail})")
                continue
            append_line(ndjson_path(args.out, name, slot),
                        make_record(slot, clock(), "app-key" if client.key else "anonymous-no-key", body))
            health[name] = {"status": status, "items": items, "detail": detail}
            log(f"[{name}] {status} {detail} ({time.time() - t0:.1f}s)")
        return failed

    failed = attempt(todo)
    if failed:
        log(f"retry pass for {failed} in {RETRY_PASS_DELAY_S}s")
        if not args.no_sleep:
            sleep(RETRY_PASS_DELAY_S)
        failed = attempt(failed)

    finished = clock()
    req, ret, r429 = (a - b for a, b in zip(client.counters(), before))
    hline = {"slot": slot_str(slot), "startedAt": iso(started), "finishedAt": iso(finished),
             "durationS": round((finished - started).total_seconds(), 1), "keyUsed": bool(client.key),
             "keyRejected": client.key_rejected,
             "nrRequests": ctx["nr"].requests if ctx.get("nr") else 0,
             "nrStatus": dict(ctx["nr"].status_counts) if ctx.get("nr") else {},
             "datasets": health, "requests": req, "retries": ret, "http429": r429}
    append_line(Path(args.out) / "_health" / (slot.strftime("%Y-%m-%d") + ".ndjson"), hline)
    log(f"slot {slot_str(slot)} done: wrote {len(todo) - len(failed)}/{len(todo)}, requests={req} "
        f"retries={ret} 429s={r429}" + (f" STILL MISSING {failed}" if failed else ""))
    return failed


def run(args, fetch=default_fetch, sleep=time.sleep, clock=utcnow, nr_fetch=nr_default_fetch):
    key = (os.environ.get("TFL_APP_KEY") or "").strip() or None
    nr_key = (os.environ.get("NR_API_KEY") or "").strip() or None
    kb_inc_key = (os.environ.get("NR_KB_INCIDENTS_KEY") or "").strip() or None
    kb_st_key = (os.environ.get("NR_KB_STATIONS_KEY") or "").strip() or None
    started = clock()
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else started
    offset = now - started  # lets --now shift the whole timeline for testing
    plan = plan_slots(now, current_only=getattr(args, "current_only", False))
    base_ctx = derive(load_registry(args.registry))
    wanted = args.datasets.split(",") if args.datasets else list(DATASETS)
    if not kb_inc_key and "nationalRailIncidents" in wanted:
        wanted.remove("nationalRailIncidents")
        log("nationalRailIncidents: NR_KB_INCIDENTS_KEY not set, dataset not collected this run")
    if not kb_st_key and "nationalRailStations" in wanted:
        wanted.remove("nationalRailStations")
        log("nationalRailStations: NR_KB_STATIONS_KEY not set, dataset not collected this run")
    if not nr_key and "nationalRailDepartures" in wanted:
        wanted.remove("nationalRailDepartures")  # nothing to do without the key; never counted as a gap
        log("nationalRailDepartures: NR_API_KEY not set, dataset not collected this run")
    bad = [d for d in wanted if d not in COLLECTORS]
    if bad:
        log(f"unknown datasets: {bad}")
        return 1
    if not key:
        log("::warning::TFL_APP_KEY is not set; using anonymous TfL access (shared rate limits). Add the repo secret.")
    log(f"start={iso(now)} plan={[slot_str(s) for s, _ in plan] or 'nothing (minute 46-29 window with no next slot)'} "
        f"key={'yes' if key else 'no'}")

    client = Client(key, fetch=fetch, sleep=(lambda s: None) if args.no_sleep else sleep)
    nr = NRClient(nr_key, fetch=nr_fetch, sleep=(lambda s: None) if args.no_sleep else sleep) if nr_key else None
    base_ctx["nr"] = nr
    base_ctx.update(kbIncidentsKey=kb_inc_key, kbStationsKey=kb_st_key, nr_fetch=nr_fetch, out=args.out, no_sleep=args.no_sleep,
                    kbIncidentsUrl=base_ctx.get("kbIncidentsUrl"))
    auth_mode = "app-key" if key else "anonymous-no-key"
    missing_any = False
    for slot, _ in plan:
        todo = [d for d in wanted if (not day_present(args.out, d, slot) if d in DAILY_AT_UTC else not slot_present(args.out, d, slot))
                and (d not in DAILY_AT_UTC or slot.hour >= DAILY_AT_UTC[d])]
        log(f"slot={slot_str(slot)} closure={is_closure_window(slot)} todo={todo or 'none (complete)'}")
        if args.dry_run:
            calls = {"liftDisruptions": 1, "lineStatus": len(base_ctx["statusModes"]),
                     "stopDisruptions": -(-len(base_ctx["disruptionIds"]) // CHUNK),
                     "crowdingReadings": len(base_ctx["crowding"]),
                     "arrivalsReadings": sum(len(ids) for _, ids in base_ctx["arrivals"]), "arrivalsDetail": 0,
                     "busLineStatus": 1, "networkDisruptions": len(base_ctx["networkDisruptionModes"]),
                     "crowdingNetwork": len(base_ctx["crowdingNetworkIds"]),
                     "nationalRailDepartures": len(base_ctx["nrStations"]) if nr_key else 0,
                     "nationalRailIncidents": 1 if kb_inc_key else 0,
                     "nationalRailStations": 1 if kb_st_key else 0, "stationAccess": 1,
                     "arrivalsNetwork": len(base_ctx.get("arrivalsNetworkModes") or [])}
            if "arrivalsDetail" in todo and "arrivalsReadings" not in todo:
                calls["arrivalsDetail"] = calls["arrivalsReadings"]
            log(f"dry-run: would make ~{sum(calls[d] for d in todo)} requests; nothing fetched or written")
            continue
        if not todo:
            continue
        wait = (slot - (clock() + offset)).total_seconds()
        if wait > 0 and not args.no_sleep:
            log(f"sleeping {wait:.0f}s until {slot_str(slot)}")
            sleep(wait)
        if collect_slot(args, client, base_ctx, slot, todo, auth_mode, sleep, clock, clock()):
            missing_any = True
    if missing_any:
        return 2
    return 3 if client.key_rejected else 0  # 3 = data complete, but the TfL key needs replacing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="data")
    ap.add_argument("--registry", default="config/registry.json")
    ap.add_argument("--now", help="override start time (ISO, UTC) for testing")
    ap.add_argument("--no-sleep", action="store_true", help="skip pre-hour wait and retry delays")
    ap.add_argument("--current-only", action="store_true", help="only the current hour (manual runs)")
    ap.add_argument("--datasets", help="comma-separated subset")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except Exception as e:  # never leak the key in a traceback
        log(f"fatal: {type(e).__name__}: {redact(e, os.environ.get('TFL_APP_KEY'))}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
